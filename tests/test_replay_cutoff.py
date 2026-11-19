import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.ARCHIVE_DIR", tmp_path / "replay" / "archive")
    init_db()
    yield


def login(client):
    token = client.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + token}


def official_watermark(client, headers, source):
    items = client.get("/api/watermarks", headers=headers).json()["items"]
    return next(x for x in items if x["source"] == source)["official_watermark"]


def sealed_card(client, headers, source):
    card = client.post("/api/replay/cards", headers=headers, json={"source": source}).json()
    card = client.post(f"/api/replay/cards/{card['id']}/seal", headers=headers).json()
    assert card["status"] == "封板"
    return card


# ---------------------------------------------------------------------------
# 隔离失败测：回放不得抬高正式水位、不得混入正式到达、不得改源目录牌
# 修复前为红：回放直接写正式表，MAX(ingest_clock) 被抬高。
# ---------------------------------------------------------------------------
def test_isolation_replay_must_not_lift_official_watermark():
    client = TestClient(app)
    h = login(client)
    before_wm = official_watermark(client, h, "订单源")
    before_official = len(client.get("/api/arrivals", headers=h).json()["items"])
    before_source_official = next(
        x for x in client.get("/api/reconciliation", headers=h).json()["items"] if x["source"] == "订单源"
    )["official_arrivals"]
    before_mode = next(s for s in client.get("/api/sources", headers=h).json()["items"] if s["name"] == "订单源")["output_mode"]

    card = sealed_card(client, h, "订单源")
    job = client.post("/api/replay/jobs", headers=h, json={"source": "订单源", "card_id": card["id"]})
    assert job.status_code == 200
    job_id = job.json()["id"]
    rows = {
        "rows": [
            {"source_clock": "2026-11-16T10:00:00", "ingest_clock": "2026-11-16T12:00:00"},
            {"source_clock": "2026-11-16T10:30:00", "ingest_clock": "2026-11-16T12:30:00"},
        ]
    }
    assert client.post(f"/api/replay/jobs/{job_id}/rows", headers=h, json=rows).status_code == 200
    commit = client.post(f"/api/replay/jobs/{job_id}/commit", headers=h)
    assert commit.status_code == 200
    assert commit.json()["card"]["status"] == "封档"

    # 正式水位读数保持原值（回放抬高即红）
    assert official_watermark(client, h, "订单源") == before_wm
    # 正式到达卡片不得混入回放行
    official = client.get("/api/arrivals", headers=h).json()["items"]
    assert len(official) == before_official
    assert all(x.get("batch_id") is None for x in official)
    # 源目录牌输出态不被回放改写
    mode = next(s for s in client.get("/api/sources", headers=h).json()["items"] if s["name"] == "订单源")["output_mode"]
    assert mode == before_mode
    # 回放列表独立可见
    replay_rows = client.get("/api/replay/items", headers=h).json()["items"]
    assert len(replay_rows) == 2
    # 对账附页不并轨：正式与回放分列
    rec = next(x for x in client.get("/api/reconciliation", headers=h).json()["items"] if x["source"] == "订单源")
    assert rec["official_watermark"] == before_wm
    assert rec["replay_rows"] == 2
    assert rec["official_arrivals"] == before_source_official
    # 封档留档
    assert (app_module.ARCHIVE_DIR / f"card_{card['id']}.json").exists()


# ---------------------------------------------------------------------------
# 双席抢放失败测：回放席持锁时正式补采席同源抢跑须拒绝并留争用痕
# 修复前为红：无回放锁、无争用留痕，正式席照常成功。
# ---------------------------------------------------------------------------
def test_dual_seat_same_source_requires_replay_lock_and_contention_trace():
    client = TestClient(app)
    h = login(client)
    card = sealed_card(client, h, "订单源")
    job = client.post("/api/replay/jobs", headers=h, json={"source": "订单源", "card_id": card["id"]})
    assert job.status_code == 200

    r = client.post("/api/catchup/订单源", headers=h, params={"seat": "正式补采席"})
    assert r.status_code == 409
    assert "回放双计争用" in r.text

    contentions = client.get("/api/replay/contentions", headers=h).json()["items"]
    assert any(c["source"] == "订单源" and "回放双计争用" in c["note"] for c in contentions)

    # 回放侧撤销释放锁后，正式补采席可正常补齐
    assert client.post(f"/api/replay/jobs/{job.json()['id']}/abort", headers=h).status_code == 200
    assert client.post("/api/catchup/订单源", headers=h, params={"seat": "正式补采席"}).status_code == 200


# ---------------------------------------------------------------------------
# 清双计只撤销本回放标签整段，正式史不得被冲掉
# ---------------------------------------------------------------------------
def test_abort_revokes_only_replay_batch_not_official_history():
    client = TestClient(app)
    h = login(client)
    official_row = client.post("/api/arrivals", headers=h, json={
        "source": "订单源", "source_clock": "2026-11-16T09:00:00", "ingest_clock": "2026-11-16T09:10:00"})
    assert official_row.status_code == 200
    before_official = len(client.get("/api/arrivals", headers=h).json()["items"])

    card = sealed_card(client, h, "订单源")
    job = client.post("/api/replay/jobs", headers=h, json={"source": "订单源", "card_id": card["id"]}).json()
    client.post(f"/api/replay/jobs/{job['id']}/rows", headers=h, json={"rows": [
        {"source_clock": "2026-11-16T08:00:00", "ingest_clock": "2026-11-16T08:10:00"}]})
    assert client.post(f"/api/replay/jobs/{job['id']}/abort", headers=h).status_code == 200

    assert len(client.get("/api/arrivals", headers=h).json()["items"]) == before_official
    assert client.get("/api/replay/items", headers=h).json()["items"] == []


# ---------------------------------------------------------------------------
# 回放切断卡：须先封板再写入；封板后当天不许改口；甲路径偏离须拒绝留痕
# ---------------------------------------------------------------------------
def test_card_must_be_sealed_before_write_and_cannot_be_reworded():
    client = TestClient(app)
    h = login(client)
    card = client.post("/api/replay/cards", headers=h, json={"source": "订单源"}).json()
    assert card["status"] == "草稿"
    denied = client.post("/api/replay/jobs", headers=h, json={"source": "订单源", "card_id": card["id"]})
    assert denied.status_code == 409
    assert client.post(f"/api/replay/cards/{card['id']}/seal", headers=h).json()["status"] == "封板"
    assert client.patch(f"/api/replay/cards/{card['id']}", headers=h, json={"replay_day": "2026-11-17"}).status_code == 409
    audit = client.get("/api/audit", headers=h).json()["items"]
    assert any("双计偏了" in x["detail"] for x in audit)


def test_path_a_deviation_is_rejected_and_audited():
    client = TestClient(app)
    h = login(client)
    r = client.post("/api/replay/cards", headers=h, json={"source": "订单源", "path": "甲"})
    assert r.status_code == 409
    assert "偏离回放切断说明" in r.text
    audit = client.get("/api/audit", headers=h).json()["items"]
    assert any(x["event"] == "回放切断偏离" and "双计偏了" in x["detail"] for x in audit)


# ---------------------------------------------------------------------------
# 标签剔除失败则整段撤销
# ---------------------------------------------------------------------------
def test_label_exclusion_failure_revokes_whole_segment(monkeypatch):
    client = TestClient(app)
    h = login(client)
    card = sealed_card(client, h, "订单源")
    job = client.post("/api/replay/jobs", headers=h, json={"source": "订单源", "card_id": card["id"]}).json()
    client.post(f"/api/replay/jobs/{job['id']}/rows", headers=h, json={"rows": [
        {"source_clock": "2026-11-16T08:00:00", "ingest_clock": "2026-11-16T08:10:00"}]})

    monkeypatch.setattr("app.official_watermark", lambda conn, source: "LEAKED")
    r = client.post(f"/api/replay/jobs/{job['id']}/commit", headers=h)
    assert r.status_code == 409
    assert client.get("/api/replay/items", headers=h).json()["items"] == []
    job_row = next(x for x in client.get("/api/replay/jobs", headers=h).json()["items"] if x["id"] == job["id"])
    assert job_row["status"] == "已撤销"
    assert any("双计偏了" in x["detail"] for x in client.get("/api/audit", headers=h).json()["items"])
