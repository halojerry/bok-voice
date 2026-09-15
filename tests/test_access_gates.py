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


def test_personas_account_scoped_for_admin(monkeypatch):
    client, repo = _make(monkeypatch)
    foreign = repo.create_persona({"account_id": "acc-002", "name": "p2"})
    repo.create_user(username="adm", password_hash=hash_password(PW), role="admin",
                     org_id="org-t", account_id="acc-001")
    h = _peon(client)  # peon 仍 403（管理面不变）
    assert client.get(f"/api/personas/{foreign['id']}", headers=h).status_code == 403
    # admin 登录后跨账号读 → 404（旧版 200）；删除 → 404
    r = client.post("/api/auth/login", json={"username": "adm", "password": PW})
    ah = {"Authorization": "Bearer " + r.json()["token"]}
    assert client.get(f"/api/personas/{foreign['id']}", headers=ah).status_code == 404
    assert client.delete(f"/api/personas/{foreign['id']}", headers=ah).status_code == 404
    # 建人设强制本账号（旧版可建进任意账号）
    r = client.post("/api/personas", json={"account_id": "acc-002", "name": "evil"}, headers=ah)
    assert r.status_code == 200 and r.json()["account_id"] == "acc-001"


def test_misc_gates(monkeypatch):
    client, repo = _make(monkeypatch, permissions="none")
    h = _peon(client)
    # fillers hit 曾完全无闸（匿名/任意身份可刷他账号计数）
    entry = repo.create_filler_entry({"account_id": "acc-001", "lang": "zh", "text": "稍等"})
    r = client.post(f"/api/fillers/{entry['id']}/hit", headers=h)
    assert r.status_code == 403  # permissions=[] → calls/qa 均无,垫话计数归 qa 键
    # insights 曾仅 reports 页面键且无账号维度 → 管理面 only
    assert client.get("/api/insights", headers=h).status_code == 403
    # setup 曾无角色闸 → admin/root only
    assert client.get("/api/setup", headers=h).status_code == 403
    assert client.post("/api/setup/download", headers=h).status_code == 403
    # roster 认领保护：u2 不能释放/抢走 u1 的认领
    repo.create_user(username="op1", password_hash=hash_password(PW), role="user",
                     org_id="org-t", account_id="acc-001")
    entry2 = repo.upsert_roster_entry(account_id="acc-001", call_id="call-r1",
                                      object_id="", channel="whatsapp", number="138",
                                      display_name="", summary="")
    repo.update_roster_entry(entry2["id"], status="claimed", claimed_by="op1")
    r = client.post(f"/api/roster/{entry2['id']}/unclaim", headers=h)
    assert r.status_code == 403  # peon ≠ 认领人 op1
