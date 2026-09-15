"""2026-09-16 深测修复回归：by-ID 页面闸与 DELETE 角色闸（P2-4）+ 杂项闸（P3）。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"


def _make(monkeypatch, permissions=None):
    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    repo.create_user(username="peon", password_hash=hash_password(PW), role="user",
                     org_id="org-t", account_id="acc-001",
                     permissions_json="[]" if permissions == "none" else "")
    return TestClient(cp_main.app), repo


def _peon(client):
    r = client.post("/api/auth/login", json={"username": "peon", "password": PW})
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_permissionless_user_cannot_read_or_delete_calls_by_id(monkeypatch):
    from bok_voice_core.policies import select_session_manifest
    from bok_voice_core.types import CallMode, TurnEvent

    client, repo = _make(monkeypatch, permissions="none")
    repo.create_call(select_session_manifest(
        session_id="call-g1", account_id="acc-001", object_id="",
        persona_id="", mode=CallMode.SIMULATION))
    repo.update_call("call-g1", status="ended")
    repo.create_turn(TurnEvent(trace_id="call-g1", call_id="call-g1", turn_id="t1",
                               role="user", transcript="hi"))
    h = _peon(client)
    assert client.get("/api/calls", headers=h).status_code == 403      # 列表闸既有
    assert client.get("/api/calls/call-g1", headers=h).status_code == 403
    assert client.get("/api/calls/call-g1/turns", headers=h).status_code == 403
    assert client.delete("/api/calls/call-g1", headers=h).status_code == 403
