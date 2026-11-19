import hashlib, json, os, secrets, sqlite3
from datetime import date, datetime
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
# 回放专册/归档落在回放专卷（容器内挂 /app/replay），不混入主库卷的日常文件。
ARCHIVE_DIR = Path(os.environ.get("REPLAY_ARCHIVE_DIR", DATA / "replay_archive"))
SESSIONS: dict[str, str] = {}

REPLAY_SEAT = "回放席"
OFFICIAL_SEAT = "正式补采席"
CONTENTION_NOTE = "回放双计争用"
# 回放切断只走「回放抬正式水位切断」专册中的乙路径：同表贴回放标签，
# 查询聚合自动剔除标签行；剔除失败则标签整段撤销。甲路径不在本仓落库。
REPLAY_PATH = "乙"
CUTOFF_METHOD = "回放抬正式水位切断·乙"


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def audit(event: str, detail: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO audit_log(event,detail,created_at) VALUES (?,?,?)", (event, detail, now_iso()))


def deviation(detail: str) -> None:
    """偏离回放切断说明：拒绝并记下双计偏了。"""
    audit("回放切断偏离", f"双计偏了：{detail}")


def record_contention(source: str, seat: str, holder: str, action: str) -> None:
    with connect() as c:
        c.execute("""INSERT INTO replay_contentions(source,seat,holder,action,note,created_at)
                     VALUES (?,?,?,?,?,?)""", (source, seat, holder, action, CONTENTION_NOTE, now_iso()))


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS replay_cards(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          replay_day TEXT NOT NULL,
          path TEXT NOT NULL,
          cutoff_watermark TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT '草稿',
          created_at TEXT NOT NULL,
          sealed_at TEXT,
          archived_at TEXT,
          UNIQUE(source, replay_day)
        );
        CREATE TABLE IF NOT EXISTS replay_jobs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          card_id INTEGER NOT NULL,
          seat TEXT NOT NULL,
          status TEXT NOT NULL,
          opened_at TEXT NOT NULL,
          finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS replay_contentions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          seat TEXT NOT NULL,
          holder TEXT NOT NULL,
          action TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_locks(
          source TEXT PRIMARY KEY,
          holder TEXT NOT NULL,
          acquired_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cutoff_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          card_id INTEGER NOT NULL,
          method TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(arrivals)")}
        if "batch_id" not in cols:
            conn.execute("ALTER TABLE arrivals ADD COLUMN batch_id INTEGER")
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源", "2026-11-16T08:00:00", "2026-11-16T08:35:00", 35, "告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源", "2026-11-16T07:00:00", "2026-11-16T09:10:00", 130, "违约"))


def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"


def mode_of(level):
    return {"正常": "可写", "告警": "降级只读", "违约": "冻结"}[level]


def parse_clock(t: str) -> datetime:
    return datetime.fromisoformat(t)


def official_watermark(conn, source: str) -> str:
    """正式水位只认未贴回放标签的到达行（乙路径：聚合剔除标签行）。"""
    row = conn.execute(
        "SELECT MAX(ingest_clock) m FROM arrivals WHERE source=? AND batch_id IS NULL", (source,)
    ).fetchone()
    return row["m"] or ""


@app.on_event("startup")
def _s(): init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n,
            "db": str(DB_PATH), "replay_path": REPLAY_PATH}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}


class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str


@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        lag = int((parse_clock(body.ingest_clock) - parse_clock(body.source_clock)).total_seconds() // 60)
        if lag < 0: lag = 0
        lv = level_of(lag, src["sla_minutes"])
        mode = mode_of(lv)
        c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,batch_id) VALUES (?,?,?,?,?,NULL)",
                  (body.source, body.source_clock, body.ingest_clock, lag, lv))
        c.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
        return {"lag_minutes": lag, "level": lv, "output_mode": mode}


@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    """正式到达卡片：只含正式行，回放标签行并入即判并轨。"""
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM arrivals WHERE batch_id IS NULL ORDER BY id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@app.post("/api/catchup/{source}")
def catchup(source: str, seat: str = OFFICIAL_SEAT, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        lock = c.execute("SELECT * FROM source_locks WHERE source=?", (source,)).fetchone()
    if lock and lock["holder"].startswith(REPLAY_SEAT):
        # 后交只记回放双计争用，不得落正式补采。
        record_contention(source, seat, lock["holder"], "补齐恢复")
        raise HTTPException(409, "回放双计争用：回放席持锁中，正式补采席后交")
    now = now_iso()
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO source_locks(source,holder,acquired_at) VALUES (?,?,?)",
                  (source, OFFICIAL_SEAT, now))
        c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
        c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                  (source, "成功", "可写", "补齐后恢复可写", now))
        c.execute("DELETE FROM source_locks WHERE source=? AND holder=?", (source, OFFICIAL_SEAT))
        return {"ok": True, "output_mode": "可写"}


@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}


# ---------------------------------------------------------------------------
# 补采重放（回放切断）
# ---------------------------------------------------------------------------
class CardIn(BaseModel):
    source: str
    replay_day: str | None = None
    path: str = REPLAY_PATH


class CardPatch(BaseModel):
    path: str | None = None
    replay_day: str | None = None


class JobIn(BaseModel):
    source: str
    card_id: int


class ReplayRow(BaseModel):
    source_clock: str
    ingest_clock: str


class RowsIn(BaseModel):
    rows: list[ReplayRow]


@app.get("/api/watermarks")
def watermarks(authorization: str | None = Header(default=None)):
    """水位读数：正式水位剔除回放标签行，保持原值。"""
    require_user(authorization)
    with connect() as c:
        items = []
        for s in c.execute("SELECT name FROM sources ORDER BY id").fetchall():
            items.append({"source": s["name"], "official_watermark": official_watermark(c, s["name"])})
        return {"items": items}


@app.get("/api/replay/cards")
def list_cards(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM replay_cards ORDER BY id DESC").fetchall()]}


@app.post("/api/replay/cards")
def create_card(body: CardIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    if body.path != REPLAY_PATH:
        deviation(f"回放切断卡要求走 {body.path} 路径，本仓落库为乙路径，甲乙不得混用")
        raise HTTPException(409, "偏离回放切断说明：仅允许乙路径，甲乙互斥不得混用")
    day = body.replay_day or date.today().isoformat()
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        existing = c.execute("SELECT * FROM replay_cards WHERE source=? AND replay_day=?", (body.source, day)).fetchone()
        if existing:
            return dict(existing)
        cur = c.execute("""INSERT INTO replay_cards(source,replay_day,path,cutoff_watermark,status,created_at)
                           VALUES (?,?,?,?,?,?)""", (body.source, day, REPLAY_PATH, "", "草稿", now_iso()))
        return dict(c.execute("SELECT * FROM replay_cards WHERE id=?", (cur.lastrowid,)).fetchone())


@app.patch("/api/replay/cards/{card_id}")
def patch_card(card_id: int, body: CardPatch, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        card = c.execute("SELECT * FROM replay_cards WHERE id=?", (card_id,)).fetchone()
        if not card: raise HTTPException(404, "回放切断卡不存在")
        if card["status"] != "草稿":
            deviation(f"回放切断卡 {card_id} 已{card['status']}，仍尝试改口")
            raise HTTPException(409, "回放当天不许改口回放切断卡")
        if body.path is not None and body.path != REPLAY_PATH:
            deviation(f"回放切断卡 {card_id} 要求改走 {body.path}，甲乙不得混用")
            raise HTTPException(409, "偏离回放切断说明：仅允许乙路径，甲乙互斥不得混用")
        fields, values = [], []
        if body.path is not None:
            fields.append("path=?"); values.append(REPLAY_PATH)
        if body.replay_day is not None:
            fields.append("replay_day=?"); values.append(body.replay_day)
        if fields:
            values.append(card_id)
            c.execute("UPDATE replay_cards SET " + ", ".join(fields) + " WHERE id=?", values)
        return dict(c.execute("SELECT * FROM replay_cards WHERE id=?", (card_id,)).fetchone())


@app.post("/api/replay/cards/{card_id}/seal")
def seal_card(card_id: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        card = c.execute("SELECT * FROM replay_cards WHERE id=?", (card_id,)).fetchone()
        if not card: raise HTTPException(404, "回放切断卡不存在")
        if card["status"] == "封档":
            raise HTTPException(409, "回放切断卡已封档")
        if card["status"] == "封板":
            return dict(card)
        wm = official_watermark(c, card["source"])
        c.execute("UPDATE replay_cards SET status='封板', cutoff_watermark=?, sealed_at=? WHERE id=?",
                  (wm, now_iso(), card_id))
        c.execute("INSERT INTO cutoff_ledger(card_id,method,detail,created_at) VALUES (?,?,?,?)",
                  (card_id, CUTOFF_METHOD, f"封板基准正式水位={wm or '空'}", now_iso()))
        return dict(c.execute("SELECT * FROM replay_cards WHERE id=?", (card_id,)).fetchone())


@app.get("/api/replay/jobs")
def list_jobs(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM replay_jobs ORDER BY id DESC").fetchall()]}


@app.post("/api/replay/jobs")
def open_job(body: JobIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        card = c.execute("SELECT * FROM replay_cards WHERE id=?", (body.card_id,)).fetchone()
        if not card: raise HTTPException(404, "回放切断卡不存在")
        if card["source"] != body.source:
            deviation(f"回放作业源 {body.source} 与切断卡源 {card['source']} 不一致")
            raise HTTPException(409, "偏离回放切断说明：源与切断卡不一致")
        if card["status"] != "封板":
            raise HTTPException(409, "回放切断卡未封板：须先封板再写入")
        lock = c.execute("SELECT * FROM source_locks WHERE source=?", (body.source,)).fetchone()
    if lock:
        record_contention(body.source, REPLAY_SEAT, lock["holder"], "回放作业")
        raise HTTPException(409, "回放双计争用：同源已有席位持锁，后交不落库")
    with connect() as c:
        cur = c.execute("""INSERT INTO replay_jobs(source,card_id,seat,status,opened_at)
                           VALUES (?,?,?,?,?)""", (body.source, body.card_id, REPLAY_SEAT, "持锁", now_iso()))
        job_id = cur.lastrowid
        c.execute("INSERT OR REPLACE INTO source_locks(source,holder,acquired_at) VALUES (?,?,?)",
                  (body.source, f"{REPLAY_SEAT}#{job_id}", now_iso()))
        return dict(c.execute("SELECT * FROM replay_jobs WHERE id=?", (job_id,)).fetchone())


def _held_job(conn, job_id: int):
    job = conn.execute("SELECT * FROM replay_jobs WHERE id=?", (job_id,)).fetchone()
    if not job: raise HTTPException(404, "回放作业不存在")
    if job["status"] != "持锁": raise HTTPException(409, "回放作业不在持锁态")
    card = conn.execute("SELECT * FROM replay_cards WHERE id=?", (job["card_id"],)).fetchone()
    if not card or card["status"] != "封板":
        raise HTTPException(409, "回放切断卡未封板：须先封板再写入")
    return job, card


def _release(conn, source: str, job_id: int):
    conn.execute("DELETE FROM source_locks WHERE source=? AND holder=?", (source, f"{REPLAY_SEAT}#{job_id}"))


@app.post("/api/replay/jobs/{job_id}/rows")
def add_replay_rows(job_id: int, body: RowsIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        job, card = _held_job(c, job_id)
        src = c.execute("SELECT * FROM sources WHERE name=?", (job["source"],)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        inserted = 0
        for r in body.rows:
            try:
                lag = int((parse_clock(r.ingest_clock) - parse_clock(r.source_clock)).total_seconds() // 60)
            except ValueError:
                raise HTTPException(400, "时钟格式不合法")
            if lag < 0: raise HTTPException(400, "摄入时钟早于源时钟")
            lv = level_of(lag, src["sla_minutes"])
            # 乙路径：写入同表但贴回放标签，正式聚合一律剔除；不改源目录牌。
            c.execute("""INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,batch_id)
                         VALUES (?,?,?,?,?,?)""",
                      (job["source"], r.source_clock, r.ingest_clock, lag, lv, job_id))
            inserted += 1
        return {"ok": True, "inserted": inserted}


@app.post("/api/replay/jobs/{job_id}/commit")
def commit_job(job_id: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        job, card = _held_job(conn, job_id)
        n = conn.execute("SELECT COUNT(*) c FROM arrivals WHERE batch_id=?", (job_id,)).fetchone()["c"]
        if n == 0: raise HTTPException(409, "回放作业无标签行")
        # 乙路径自检：标签剔除后正式水位必须仍等于封板基准；否则整段撤销。
        now_wm = official_watermark(conn, job["source"])
        if now_wm != card["cutoff_watermark"]:
            conn.execute("DELETE FROM arrivals WHERE batch_id=?", (job_id,))
            conn.execute("UPDATE replay_jobs SET status='已撤销', finished_at=? WHERE id=?", (now_iso(), job_id))
            _release(conn, job["source"], job_id)
            conn.execute("INSERT INTO audit_log(event,detail,created_at) VALUES (?,?,?)",
                         ("回放切断偏离", f"双计偏了：标签剔除失败 正式水位 {now_wm!r} != 封板基准 {card['cutoff_watermark']!r}，标签整段撤销", now_iso()))
            conn.commit()
            raise HTTPException(409, "回放标签剔除失败：标签整段撤销")
        archived_at = now_iso()
        conn.execute("UPDATE replay_cards SET status='封档', archived_at=? WHERE id=?", (archived_at, card["id"]))
        conn.execute("UPDATE replay_jobs SET status='已封档', finished_at=? WHERE id=?", (archived_at, job_id))
        _release(conn, job["source"], job_id)
        conn.commit()
        card = dict(conn.execute("SELECT * FROM replay_cards WHERE id=?", (card["id"],)).fetchone())
    finally:
        conn.close()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (ARCHIVE_DIR / f"card_{card['id']}.json").write_text(
        json.dumps({"card": card, "job_id": job_id, "method": CUTOFF_METHOD}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return {"ok": True, "card": card, "rows": n}


@app.post("/api/replay/jobs/{job_id}/abort")
def abort_job(job_id: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        job = c.execute("SELECT * FROM replay_jobs WHERE id=?", (job_id,)).fetchone()
        if not job: raise HTTPException(404, "回放作业不存在")
        if job["status"] != "持锁": raise HTTPException(409, "回放作业不在持锁态")
        # 清双计只撤销本回放标签整段，正式史原样保留。
        c.execute("DELETE FROM arrivals WHERE batch_id=?", (job_id,))
        c.execute("UPDATE replay_jobs SET status='已撤销', finished_at=? WHERE id=?", (now_iso(), job_id))
        _release(c, job["source"], job_id)
        return {"ok": True}


@app.get("/api/replay/items")
def replay_items(authorization: str | None = Header(default=None)):
    """回放列表：仅回放标签行，与正式到达卡片分轨。"""
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM arrivals WHERE batch_id IS NOT NULL ORDER BY id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@app.get("/api/replay/contentions")
def replay_contentions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM replay_contentions ORDER BY id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@app.get("/api/audit")
def audit_log(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@app.get("/api/reconciliation")
def reconciliation(authorization: str | None = Header(default=None)):
    """对账附页：正式与回放分列，禁止并轨。"""
    require_user(authorization)
    with connect() as c:
        items = []
        for s in c.execute("SELECT * FROM sources ORDER BY id").fetchall():
            official = c.execute("SELECT COUNT(*) c FROM arrivals WHERE source=? AND batch_id IS NULL", (s["name"],)).fetchone()
            replay = c.execute("SELECT COUNT(*) c, MAX(ingest_clock) m FROM arrivals WHERE source=? AND batch_id IS NOT NULL", (s["name"],)).fetchone()
            items.append({
                "source": s["name"],
                "official_watermark": official_watermark(c, s["name"]),
                "official_arrivals": official["c"],
                "replay_rows": replay["c"],
                "replay_watermark": replay["m"] or "",
                "output_mode": s["output_mode"],
            })
        return {"items": items}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
