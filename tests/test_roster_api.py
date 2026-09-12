from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from bok_voice_business_db.repository import InMemoryBusinessRepository


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import _repo, app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def test_roster_claim_flow(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    entry = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-x", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    rows = client.get("/api/roster").json()
    assert rows[0]["id"] == entry["id"]
    r = client.post(f"/api/roster/{entry['id']}/claim", json={"claimed_by": "acc-001"})
    assert r.status_code == 200 and r.json()["status"] == "claimed"
    r = client.post(f"/api/roster/{entry['id']}/unclaim")
    assert r.json()["status"] == "unclaimed" and r.json()["claimed_by"] == ""
    r = client.post(f"/api/roster/{entry['id']}/handled", json={"handled": True})
    assert r.json()["status"] == "handled"
    r = client.get("/api/roster", params={"status": "handled"})
    assert len(r.json()) == 1
    assert client.post("/api/roster/roster-nope/handled", json={"handled": True}).status_code == 404


def test_roster_list_filters_channel_and_account(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    repo.upsert_roster_entry(
        account_id="acc-001", call_id="c1", object_id="o1",
        channel="whatsapp", number="11112222",
    )
    repo.upsert_roster_entry(
        account_id="acc-001", call_id="c2", object_id="o2",
        channel="wechat", number="33334444",
    )
    repo.upsert_roster_entry(
        account_id="acc-002", call_id="c3", object_id="o3",
        channel="whatsapp", number="55556666",
    )
    assert len(client.get("/api/roster").json()) == 2
    assert [r["channel"] for r in client.get("/api/roster", params={"channel": "wechat"}).json()] == ["wechat"]
    assert client.get("/api/roster", params={"account_id": "acc-002"}).json()[0]["number"] == "55556666"


def test_roster_claim_404(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    assert client.post("/api/roster/roster-nope/claim", json={"claimed_by": "acc-001"}).status_code == 404
    assert client.post("/api/roster/roster-nope/unclaim").status_code == 404


def test_roster_claim_default_claimed_by(monkeypatch):
    """body 缺省 claimed_by → acc-001（操作台单账号形态）。"""
    client, repo = _client_and_repo(monkeypatch)
    entry = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-x", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    r = client.post(f"/api/roster/{entry['id']}/claim", json={})
    assert r.status_code == 200
    assert r.json()["claimed_by"] == "acc-001"
    assert r.json()["claimed_at"] != ""


def test_roster_handled_syncs_call_whatsapp_status(monkeypatch):
    """handled=true → 来源通话 whatsapp_status=handled；撤销 → 有号码回 captured、无号码回 offered。"""
    client, repo = _client_and_repo(monkeypatch)
    created = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
    ).json()
    call = {"id": created["id"]}
    repo.update_call(call["id"], customer_whatsapp="64320111", whatsapp_status="captured")
    entry = repo.upsert_roster_entry(
        account_id="acc-001", call_id=call["id"], object_id="obj-1",
        channel="whatsapp", number="64320111",
    )

    r = client.post(f"/api/roster/{entry['id']}/handled", json={"handled": True})
    assert r.json()["status"] == "handled"
    assert repo.get_call(call["id"])["whatsapp_status"] == "handled"

    r = client.post(f"/api/roster/{entry['id']}/handled", json={"handled": False})
    assert r.json()["status"] == "unclaimed" and r.json()["claimed_by"] == ""
    assert repo.get_call(call["id"])["whatsapp_status"] == "captured"

    # 无号码的通话（offered）→ 撤销回 offered
    repo.update_call(call["id"], customer_whatsapp="", whatsapp_status="handled")
    r = client.post(f"/api/roster/{entry['id']}/handled", json={"handled": False})
    assert r.status_code == 200
    assert repo.get_call(call["id"])["whatsapp_status"] == "offered"


def test_roster_handled_missing_call_id_is_tolerated(monkeypatch):
    """来源 call 缺失（call_id 为空/通话已删）→ 名册状态照改，不 500。"""
    client, repo = _client_and_repo(monkeypatch)
    entry = repo.upsert_roster_entry(
        account_id="acc-001", call_id="", object_id="obj-1",
        channel="whatsapp", number="99998888",
    )
    assert client.post(f"/api/roster/{entry['id']}/handled", json={"handled": True}).json()["status"] == "handled"
    assert client.post(f"/api/roster/{entry['id']}/handled", json={"handled": False}).json()["status"] == "unclaimed"


def test_roster_claim_unclaim_sql_backend_parity(monkeypatch):
    """SQL 后端同契约：claim 写 datetime → 读侧 ISO；unclaim 空串 → NULL → 读侧 ""。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from bok_voice_business_db import models
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository
    from fastapi.testclient import TestClient

    from control_plane.main import app

    # TestClient 请求跑在 anyio 工作线程：sqlite:// 默认 SingletonThreadPool 每线程
    # 各持一条独立连接（各自空内存库）→ 建表线程与请求线程看到的库不是同一个。
    # StaticPool 令全线程共享同一连接（内存库唯一）。
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.create_all(engine)
    repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine, expire_on_commit=False, future=True)())
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app)

    entry = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-x", object_id="obj-1",
        channel="whatsapp", number="64320111", display_name="张三", summary="已沟通",
    )
    r = client.post(f"/api/roster/{entry['id']}/claim", json={"claimed_by": "acc-001"})
    assert r.status_code == 200
    assert r.json()["status"] == "claimed" and r.json()["claimed_by"] == "acc-001"
    assert r.json()["claimed_at"] != ""  # datetime → ISO

    r = client.post(f"/api/roster/{entry['id']}/unclaim")
    assert r.status_code == 200
    assert r.json()["status"] == "unclaimed"
    assert r.json()["claimed_by"] == "" and r.json()["claimed_at"] == ""
    # 确认落库为 NULL 而非字符串 ""
    assert repo.get_roster_entry(entry["id"])["claimed_at"] == ""
