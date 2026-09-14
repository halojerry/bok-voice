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


def _make_call(repo, session_id: str) -> dict:
    from bok_voice_core.types import CallMode, SessionManifest

    return repo.create_call(
        SessionManifest(
            session_id=session_id,
            account_id="acc-001",
            object_id="",
            persona_id="",
            mode=CallMode.LIVE,
            direction="outbound",
            language="zh",
            providers={},
        )
    )


def test_dial_result_no_answer_ends_call_with_disposition(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _make_call(repo, "call-dialtest")
    r = client.post(
        "/api/calls/call-dialtest/dial-result",
        json={"status": "no_answer", "detail": "timeout"},
    )
    assert r.status_code == 200
    call = repo.get_call("call-dialtest")
    assert call["status"] == "ended" and call["disposition"] == "no_answer"


def test_dial_result_rejected_and_failed_end_call(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    for status in ("rejected", "failed"):
        _make_call(repo, f"call-{status}")
        r = client.post(f"/api/calls/call-{status}/dial-result", json={"status": status})
        assert r.status_code == 200, status
        call = repo.get_call(f"call-{status}")
        assert call["status"] == "ended", status
        assert call["disposition"] == status, status


def test_dial_result_answered_activates(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _make_call(repo, "call-ans")
    r = client.post("/api/calls/call-ans/dial-result", json={"status": "answered"})
    assert r.status_code == 200
    assert repo.get_call("call-ans")["status"] == "active"


def test_dial_result_missing_call_is_404(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    r = client.post("/api/calls/call-nope/dial-result", json={"status": "answered"})
    assert r.status_code == 404


def test_dial_result_unknown_status_is_noop(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _make_call(repo, "call-unknown")
    r = client.post("/api/calls/call-unknown/dial-result", json={"status": "bogus"})
    assert r.status_code == 200
    assert repo.get_call("call-unknown")["status"] == "ringing"


def test_dial_result_terminal_state_is_idempotent(monkeypatch):
    """终态幂等：已 ended 的通话再报 answered 不得复活成 active（重派/重复上报防抖）。"""
    client, repo = _client_and_repo(monkeypatch)
    _make_call(repo, "call-terminal")
    client.post("/api/calls/call-terminal/dial-result", json={"status": "no_answer"})
    assert repo.get_call("call-terminal")["status"] == "ended"
    r = client.post("/api/calls/call-terminal/dial-result", json={"status": "answered"})
    assert r.status_code == 200
    call = repo.get_call("call-terminal")
    assert call["status"] == "ended" and call["disposition"] == "no_answer"
