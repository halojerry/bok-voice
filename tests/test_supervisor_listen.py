"""主管静默旁听（路线 A3）：token 只订阅不发布 + 审计留痕 + 不动通话状态。"""
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


def _audit_capture(monkeypatch) -> list[dict]:
    """照 test_campaign_audit_events 姿势：换 tap 收集审计事件 dict。"""
    from bok_voice_obs.audit import AuditStore, audit_store

    events: list[dict] = []
    original = audit_store()
    monkeypatch.setattr(
        "bok_voice_obs.audit._STORE",
        AuditStore(original.directory, tap=lambda e: events.append(e.to_dict())),
    )
    return events


def _make_call(repo, *, status: str = "active") -> dict:
    from bok_voice_core.types import CallMode, SessionManifest

    call = repo.create_call(
        SessionManifest(
            session_id="call-listen-1",
            account_id="acc-001",
            object_id="",
            persona_id="",
            mode=CallMode.LIVE,
            direction="inbound",
            language="zh",
            providers={},
        )
    )
    if status != "ringing":
        repo.update_call(call["id"], status=status)
    return call


def _decode(token: str) -> tuple[dict, dict]:
    import jwt

    claims = jwt.decode(token, options={"verify_signature": False})
    return claims, (claims.get("video") or {})


def test_listen_token_is_subscribe_only_and_audited(monkeypatch):
    """旁听 token：can_publish/can_publish_data 全关、无 agent dispatch、留审计。"""
    events = _audit_capture(monkeypatch)
    client, repo = _client_and_repo(monkeypatch)
    call = _make_call(repo, status="paused")

    r = client.post(f"/api/supervisor/{call['id']}/listen")
    assert r.status_code == 200
    claims, video = _decode(r.json()["participantToken"])
    assert claims["sub"] == f"supervisor-{call['id']}"
    assert video["canPublish"] is False
    assert video["canPublishData"] is False
    assert video["canSubscribe"] is True
    # 旁听不挂 RoomConfiguration：主管若先到，不得替房间建房/拉起 agent。
    assert not (claims.get("roomConfig") or claims.get("room_config"))

    # 旁听不得把 paused 悄悄翻回 active（与 operator 进房不同）。
    assert repo.get_call(call["id"])["status"] == "paused"
    assert "supervisor.listen.start" in [e["action"] for e in events]

    # /api/token 的 purpose=listen 直发路径同款 grants（web 也走这条路）。
    direct = client.post(
        "/api/token",
        json={"account_id": "acc-001", "call_id": call["id"], "purpose": "listen"},
    )
    assert direct.status_code == 201
    _claims2, video2 = _decode(direct.json()["participantToken"])
    assert video2["canPublish"] is False and video2["canSubscribe"] is True
    assert repo.get_call(call["id"])["status"] == "paused"


def test_listen_stop_records_seconds(monkeypatch):
    events = _audit_capture(monkeypatch)
    client, repo = _client_and_repo(monkeypatch)
    call = _make_call(repo)
    r = client.post(f"/api/supervisor/{call['id']}/listen/stop", json={"seconds": 42})
    assert r.status_code == 200 and r.json()["recorded"] is True
    stops = [e for e in events if e["action"] == "supervisor.listen.stop"]
    assert stops and stops[0]["detail"]["seconds"] == 42


def test_listen_refuses_ended_call(monkeypatch):
    """终态通话不签旁听 token（409），与幽灵重连守卫同族语义。"""
    client, repo = _client_and_repo(monkeypatch)
    call = _make_call(repo, status="ended")
    assert client.post(f"/api/supervisor/{call['id']}/listen").status_code == 409
