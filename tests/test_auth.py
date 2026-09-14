"""三层 RBAC 认证内核（路线 B1）：登录/JWT/门禁/账号管理。

关键契约：
- BOK_AUTH_REQUIRED 未设（默认）→ 全站开放（单机/开发形态零变化，基线测试不破）；
- 置 1 → 除豁免路径（/health、/api/auth/login、/api/nodes/heartbeat、/docs）外
  全部要求用户 JWT 或 BOK_CP_TOKEN 机器 token；
- 角色规则：root 全域；admin 仅本 account 且只能建/管 user；user 无权。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-auth")

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def _mk_user(repo, username, role="user", account="acc-001", password=PW):
    return repo.create_user(
        username=username, password_hash=hash_password(password),
        role=role, org_id="org-t", account_id=account,
    )


def _login(client, username, password=PW):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_me_and_change_password(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "alice")

    body = _login(client, "alice")
    assert body["user"]["role"] == "user"
    assert "password_hash" not in body["user"]  # 凭据材料不出仓
    me = client.get("/api/auth/me", headers=_auth(body["token"])).json()
    assert me["username"] == "alice" and me["role"] == "user" and me["account_id"] == "acc-001"

    # 错密码 401（登录本身开放，不受门禁影响）
    assert client.post("/api/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
    # me 无 token → 401
    assert client.get("/api/auth/me").status_code == 401

    h = _auth(body["token"])
    assert client.post("/api/auth/change-password", headers=h,
                       json={"old_password": "bad", "new_password": "NewPass0rd!"}).status_code == 401
    assert client.post("/api/auth/change-password", headers=h,
                       json={"old_password": PW, "new_password": "short"}).status_code == 400
    assert client.post("/api/auth/change-password", headers=h,
                       json={"old_password": PW, "new_password": "NewPass0rd!"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "alice", "password": PW}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": "NewPass0rd!"}).status_code == 200


def test_users_crud_role_gates(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "rooty", role="root", account="")
    _mk_user(repo, "boss", role="admin", account="acc-001")
    _mk_user(repo, "peon", role="user", account="acc-001")

    rt = _login(client, "rooty")["token"]
    at = _login(client, "boss")["token"]
    pt = _login(client, "peon")["token"]

    # admin 建本账号 user ✓（不传 account 默认落 admin 本账号）；建 admin ✗；user 无权 ✗
    r = client.post("/api/users", headers=_auth(at),
                    json={"username": "new-op", "password": PW, "role": "user"})
    assert r.status_code == 200 and r.json()["account_id"] == "acc-001"
    assert client.post("/api/users", headers=_auth(at),
                       json={"username": "nope", "password": PW, "role": "admin"}).status_code == 403
    assert client.post("/api/users", headers=_auth(pt),
                       json={"username": "nope2", "password": PW, "role": "user"}).status_code == 403
    # root 建 admin（跨账号）✓；重名 409
    assert client.post("/api/users", headers=_auth(rt),
                       json={"username": "boss2", "password": PW, "role": "admin",
                             "account_id": "acc-002"}).status_code == 200
    assert client.post("/api/users", headers=_auth(at),
                       json={"username": "new-op", "password": PW, "role": "user"}).status_code == 409

    # 列表：user 403；admin 强制本账号视角；root 看全部
    assert client.get("/api/users", headers=_auth(pt)).status_code == 403
    lst = client.get("/api/users", headers=_auth(at)).json()["users"]
    assert {u["username"] for u in lst} == {"boss", "peon", "new-op"}
    assert len(client.get("/api/users", headers=_auth(rt)).json()["users"]) == 5

    # PATCH：admin 禁动 root / 禁改 role；admin 可停用本账号 user（停用后登录 401）；root 恢复
    uid = {u["username"]: u["id"] for u in client.get("/api/users", headers=_auth(rt)).json()["users"]}
    assert client.patch(f"/api/users/{uid['rooty']}", headers=_auth(at),
                        json={"status": "disabled"}).status_code == 403
    assert client.patch(f"/api/users/{uid['peon']}", headers=_auth(at),
                        json={"role": "admin"}).status_code == 403
    assert client.patch(f"/api/users/{uid['peon']}", headers=_auth(at),
                        json={"status": "disabled"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "peon", "password": PW}).status_code == 401
    assert client.patch(f"/api/users/{uid['peon']}", headers=_auth(rt),
                        json={"status": "active"}).status_code == 200


def test_gate_auth_required(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    _mk_user(repo, "gate-admin", role="admin", account="acc-001")
    at = _login(client, "gate-admin")["token"]

    # 无 token → 401；带 token → 200；豁免路径开放（/health、登录本身）
    assert client.get("/api/objects").status_code == 401
    assert client.get("/api/objects", headers=_auth(at)).status_code == 200
    assert client.get("/health").status_code == 200
    assert client.post("/api/auth/login", json={"username": "gate-admin", "password": PW}).status_code == 200

    # 机器通道：BOK_CP_TOKEN 同值直通；假 token 仍拒
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    assert client.get("/api/objects", headers={"Authorization": "Bearer machine-token"}).status_code == 200
    assert client.get("/api/objects", headers={"Authorization": "Bearer bogus"}).status_code == 401


def test_auth_off_is_open_by_default(monkeypatch):
    """默认（BOK_AUTH_REQUIRED 未设）全站开放——基线 847 测试与单机形态不破。"""
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    assert client.get("/api/objects").status_code == 200
