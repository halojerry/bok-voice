"""2026-09-16 深度安全测试修复回归：鉴权内核组。

覆盖：JWT 密钥分离（P1-1）、token 生命周期查库（P2-1）、/api/token supervisor
前缀闸（P2-2）、webhook 验签（P2-5）、审计 actor 不可伪造（P2-6）、登录时序（P3）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # in-memory repo
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"


def _make(monkeypatch, users=()):
    """TestClient(带 startup) + InMemory 仓；users=[{username, role, account}]。"""
    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    for u in users:
        repo.create_user(
            username=u["username"], password_hash=hash_password(PW),
            role=u.get("role", "user"), org_id="org-t",
            account_id=u.get("account", "acc-001"),
        )
    return TestClient(cp_main.app), repo


def _login(client, username, password=PW):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_jwt_secret_never_falls_back_to_cp_token(monkeypatch):
    from control_plane import auth

    monkeypatch.delenv("BOK_JWT_SECRET", raising=False)
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    assert auth.jwt_secret() == ""  # 机器通道凭据不再回落作签名密钥
    monkeypatch.setenv("BOK_JWT_SECRET", "real-secret-0123456789abcdef")
    assert auth.jwt_secret() == "real-secret-0123456789abcdef"


def test_startup_rejects_missing_or_shared_or_weak_secret(monkeypatch):
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.delenv("BOK_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="BOK_JWT_SECRET"):
        from control_plane import main as cp_main

        cp_main._startup()
    monkeypatch.setenv("BOK_JWT_SECRET", "same-value-as-cp-token")
    monkeypatch.setenv("BOK_CP_TOKEN", "same-value-as-cp-token")
    with pytest.raises(RuntimeError, match="同值"):
        cp_main._startup()
    monkeypatch.setenv("BOK_JWT_SECRET", "short")
    with pytest.raises(RuntimeError, match="强度不足"):
        cp_main._startup()


def test_disabled_or_demoted_token_dies_immediately(monkeypatch):
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    # startup fail-closed 校验密钥强度：模块级 setdefault 会按 pytest 收集顺序被
    # 先导入的 test_auth.py 的短值占位——这里显式钉住合法密钥，与文件顺序解耦。
    monkeypatch.setenv("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")
    client, repo = _make(monkeypatch, users=[
        {"username": "peon", "role": "user"},
        {"username": "boss", "role": "admin"},
        {"username": "rooty", "role": "root", "account": "acc-002"},
    ])
    with client:  # 触发 startup：注入 app.state.user_lookup
        peon_token = _login(client, "peon")
        boss_token = _login(client, "boss")
        # 禁用 peon → 旧 token 立即 401
        rooty_headers = {"Authorization": "Bearer " + _login(client, "rooty")}
        assert client.post("/api/users", json={"username": "x1", "password": PW,
                                               "role": "user"}, headers=rooty_headers).status_code == 200
        # root 停用 peon（rooty 在 acc-002，跨账号管理被 deny——改由 admin 路径：
        # 直接用 repo 模拟主管禁用）
        repo.update_user(repo.get_user_by_username("peon")["id"], status="disabled")
        assert client.get("/api/objects", headers={"Authorization": f"Bearer {peon_token}"}).status_code == 401
        # 降级 boss：admin→user 后旧 token 打管理面立即 403
        repo.update_user(repo.get_user_by_username("boss")["id"], role="user")
        assert client.get("/api/settings", headers={"Authorization": f"Bearer {boss_token}"}).status_code == 403
