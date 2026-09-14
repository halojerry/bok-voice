from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from bok_voice_business_db.repository import InMemoryBusinessRepository


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def _create_call_via(repo, obj) -> dict:
    """照 test_dial_result_api 同款：直接用 repo.create_call(manifest) 造一通。"""
    from bok_voice_core.types import CallMode, SessionManifest

    return repo.create_call(
        SessionManifest(
            session_id=f"call-{obj['id']}",
            account_id="acc-001",
            object_id=obj["id"],
            persona_id="",
            mode=CallMode.LIVE,
            direction="outbound",
            language="zh",
            providers={},
        )
    )


def _campaign_body(obj, **over) -> dict:
    body = {
        "name": "波次一",
        "object_ids": [obj["id"]],
        "language": "zh",
    }
    body.update(over)
    return body


def test_create_and_detail_defaults(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    r = client.post("/api/campaigns", json=_campaign_body(obj))
    assert r.status_code == 200
    camp = r.json()
    assert camp["status"] == "draft"
    assert camp["id"] and camp["account_id"] == "acc-001"
    assert camp["language"] == "zh" and camp["gap_seconds"] == 5
    assert camp["template_id"] == "" and camp["persona_id"] == ""
    assert camp["name"] == "波次一"


def test_create_requires_object_ids(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    r = client.post("/api/campaigns", json={"name": "空波次", "language": "zh"})
    assert r.status_code == 400


def test_create_filters_scenario_whitelist(monkeypatch):
    """scenarios 值只收 answer/no_answer/reject/hangup_mid，其余（含拼错）丢弃。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    r = client.post(
        "/api/campaigns",
        json=_campaign_body(
            obj,
            scenarios={obj["id"]: "no_answer"},
        ),
    )
    assert r.status_code == 200
    assert repo.list_items(r.json()["id"])[0]["scenario"] == "no_answer"

    obj2 = repo.create_object("acc-001", {"display_name": "B", "phone": "+85222222222"})
    r2 = client.post(
        "/api/campaigns",
        json=_campaign_body(obj2, scenarios={obj2["id"]: "bogus"}),
    )
    assert r2.status_code == 200
    assert repo.list_items(r2.json()["id"])[0]["scenario"] == ""


def test_lifecycle_transitions(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]

    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.status_code == 200 and r.json()["status"] == "running"
    r = client.post(f"/api/campaigns/{cid}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.json()["status"] == "running"
    r = client.post(f"/api/campaigns/{cid}/stop")
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    # stopped 是终态：不可再 start/pause
    assert client.post(f"/api/campaigns/{cid}/start").status_code == 409
    assert client.post(f"/api/campaigns/{cid}/pause").status_code == 409


def test_lifecycle_illegal_transitions_are_409(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    # draft 不能 pause（未开始）
    assert client.post(f"/api/campaigns/{cid}/pause").status_code == 409
    # draft 可以 stop
    assert client.post(f"/api/campaigns/{cid}/stop").status_code == 200


def test_lifecycle_missing_campaign_is_404(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    for verb in ("start", "pause", "stop"):
        assert client.post(f"/api/campaigns/camp-nope/{verb}").status_code == 404


def test_progress_counts_and_answered(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    objs = [
        repo.create_object("acc-001", {"display_name": f"A{i}", "phone": f"+852111111{i}"})
        for i in range(4)
    ]
    cid = client.post(
        "/api/campaigns", json=_campaign_body(objs[0], object_ids=[o["id"] for o in objs])
    ).json()["id"]
    items = repo.list_items(cid)
    repo.update_item(items[0]["id"], status="done")
    repo.update_item(items[1]["id"], status="no_answer")
    repo.update_item(items[2]["id"], status="rejected")
    # items[3] 保持 pending

    detail = client.get(f"/api/campaigns/{cid}").json()
    p = detail["progress"]
    assert p["total"] == 4
    assert (p["pending"], p["done"], p["no_answer"], p["rejected"]) == (1, 1, 1, 1)
    assert p["dialing"] == p["in_call"] == p["failed"] == p["skipped"] == 0
    assert p["answered"] == 3  # done + no_answer + rejected
    assert len(detail["items"]) == 4

    rows = client.get("/api/campaigns").json()
    row = next(c for c in rows if c["id"] == cid)
    assert row["progress"] == p


def test_list_filters_account_and_detail_404(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    assert [c["id"] for c in client.get("/api/campaigns").json()] == [cid]
    assert client.get("/api/campaigns", params={"account_id": "acc-002"}).json() == []
    assert client.get("/api/campaigns/camp-nope").status_code == 404


def test_campaign_crud_and_dial_result(monkeypatch):
    """Brief 主链：建战役 → 启停 → 详情/列表 → dial-result 联动 item 状态。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    r = client.post("/api/campaigns", json=_campaign_body(obj))
    assert r.status_code == 200
    cid = r.json()["id"]
    assert r.json()["status"] == "draft"
    r = client.post(f"/api/campaigns/{cid}/start")
    assert r.json()["status"] == "running"
    r = client.post(f"/api/campaigns/{cid}/pause")
    assert r.json()["status"] == "paused"
    detail = client.get(f"/api/campaigns/{cid}").json()
    assert detail["progress"]["total"] == 1 and detail["items"][0]["status"] == "pending"
    assert client.get("/api/campaigns").json()[0]["id"] == cid

    # dial-result 联动：answered → item in_call
    call = _create_call_via(repo, obj)
    repo.update_item(detail["items"][0]["id"], status="dialing", call_id=call["id"])
    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "answered"})
    assert r.status_code == 200
    assert repo.get_item(detail["items"][0]["id"])["status"] == "in_call"
    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "no_answer"})
    assert r.status_code == 200
    assert repo.get_item(detail["items"][0]["id"])["status"] == "no_answer"


def test_dial_result_links_all_failure_states(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    item_id = repo.list_items(cid)[0]["id"]

    for status in ("no_answer", "rejected", "failed"):
        call = _create_call_via(repo, obj)
        repo.update_call(call["id"], status="ringing")
        repo.update_item(item_id, status="dialing", call_id=call["id"], last_error="")
        r = client.post(
            f"/api/calls/{call['id']}/dial-result",
            json={"status": status, "detail": f"edge {status}"},
        )
        assert r.status_code == 200, status
        item = repo.get_item(item_id)
        assert item["status"] == status, status
        assert "edge" in item["last_error"], status


def test_dial_result_ignores_items_outside_dialing(monkeypatch):
    """不在 dialing/in_call 的 item 不被 dial-result 覆写（pending 不得跳到终态）。"""
    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    item_id = repo.list_items(cid)[0]["id"]
    call = _create_call_via(repo, obj)
    repo.update_item(item_id, call_id=call["id"])  # 仍 pending

    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "no_answer"})
    assert r.status_code == 200
    assert repo.get_item(item_id)["status"] == "pending"


def test_dial_result_without_campaign_item_is_noop(monkeypatch):
    """普通通话（无任何 item 指向它）上报 dial-result 照常改通话，不报错。"""
    from bok_voice_core.types import CallMode, SessionManifest

    client, repo = _client_and_repo(monkeypatch)
    call = repo.create_call(
        SessionManifest(
            session_id="call-plain", account_id="acc-001", object_id="",
            persona_id="", mode=CallMode.LIVE, direction="outbound",
            language="zh", providers={},
        )
    )
    r = client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "no_answer"})
    assert r.status_code == 200
    assert repo.get_call(call["id"])["disposition"] == "no_answer"


def test_campaign_audit_events(monkeypatch):
    """全链审计：create/start/pause/stop 各自落一条 audit 事件。"""
    from bok_voice_obs.audit import AuditStore, audit_store

    events: list[str] = []
    original = audit_store()
    monkeypatch.setattr(
        "bok_voice_obs.audit._STORE",
        AuditStore(original.directory, tap=lambda e: events.append(e.action)),
    )

    client, repo = _client_and_repo(monkeypatch)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    cid = client.post("/api/campaigns", json=_campaign_body(obj)).json()["id"]
    client.post(f"/api/campaigns/{cid}/start")
    client.post(f"/api/campaigns/{cid}/pause")
    client.post(f"/api/campaigns/{cid}/stop")

    for expected in ("campaign.create", "campaign.running", "campaign.paused",
                     "campaign.stopped"):
        assert expected in events, expected


def test_campaign_sql_backend_parity(monkeypatch):
    """SQL 后端同契约：create/list/detail/transition 全链在 SQL 仓可用。"""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from bok_voice_business_db import models
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

    from control_plane.main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.create_all(engine)
    repo = SqlAlchemyBusinessRepository(
        sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    )
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app)

    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    r = client.post("/api/campaigns", json=_campaign_body(obj))
    assert r.status_code == 200
    cid = r.json()["id"]
    assert r.json()["status"] == "draft"
    assert client.post(f"/api/campaigns/{cid}/start").json()["status"] == "running"
    detail = client.get(f"/api/campaigns/{cid}").json()
    assert detail["progress"]["total"] == 1
    assert detail["items"][0]["status"] == "pending"
    assert client.get("/api/campaigns").json()[0]["id"] == cid

    call = _create_call_via(repo, obj)
    repo.update_item(detail["items"][0]["id"], status="dialing", call_id=call["id"])
    client.post(f"/api/calls/{call['id']}/dial-result", json={"status": "answered"})
    assert repo.get_item(detail["items"][0]["id"])["status"] == "in_call"
