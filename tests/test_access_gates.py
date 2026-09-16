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


def test_audit_limit_clamped(monkeypatch):
    client, repo = _make(monkeypatch)
    repo.create_user(username="rooty", password_hash=hash_password(PW), role="root",
                     org_id="", account_id="")
    r = client.post("/api/auth/login", json={"username": "rooty", "password": PW})
    h = {"Authorization": "Bearer " + r.json()["token"]}
    # 巨值 limit 不再透传（SQL LIMIT 巨值=全表进内存）；钳制后正常返回
    assert client.get("/api/audit", params={"limit": 999999999}, headers=h).status_code == 200


def test_demoted_admin_old_token_hits_demotion_immediately(monkeypatch):
    """Task 3 残余收口：create_user/list_users/update_user/change-password/auth_me
    曾直调 identity_from_request（信 token 内 8h claim）绕过门禁按库刷新的
    state.identity——admin 被降权后旧 token 在这些端点仍按 admin 放行。改走
    current_identity（优先门禁刷新身份）后降权即时生效。"""
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    # 显式钉住合法密钥：pytest 收集顺序下 test_auth.py 的短占位可能已占 env，
    # startup fail-closed 会拒启（与 test_security_hardening 同款处理）。
    monkeypatch.setenv("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")
    client, repo = _make(monkeypatch)
    repo.create_user(username="boss", password_hash=hash_password(PW), role="admin",
                     org_id="org-t", account_id="acc-001")
    with client:  # 触发 startup：注入 app.state.user_lookup
        r = client.post("/api/auth/login", json={"username": "boss", "password": PW})
        assert r.status_code == 200, r.text
        h = {"Authorization": "Bearer " + r.json()["token"]}
        boss_id = repo.get_user_by_username("boss")["id"]
        # 降权前：admin 管理面操作正常
        assert client.get("/api/users", headers=h).status_code == 200
        r = client.patch(f"/api/users/{boss_id}", json={"display_name": "Boss"}, headers=h)
        assert r.status_code == 200, r.text
        # 降权：admin → user（repo 直改，模拟另一主管操作；身份未禁用，token 仍有效）
        repo.update_user(boss_id, role="user")
        # 降权即时生效：管理面 PATCH 自己（含列表）403；/api/auth/me 报库角色
        assert client.get("/api/users", headers=h).status_code == 403
        r = client.patch(f"/api/users/{boss_id}", json={"display_name": "Boss2"}, headers=h)
        assert r.status_code == 403, r.text
        assert client.get("/api/auth/me", headers=h).json()["role"] == "user"
