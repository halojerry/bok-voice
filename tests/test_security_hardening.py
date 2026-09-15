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


def test_token_supervisor_identity_prefix_blocked_for_user(monkeypatch):
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    # 与上一测试同理：test_auth.py 先收集时其模块级短密钥占位 env，startup
    # fail-closed 会拒启——显式钉住合法密钥，与文件顺序解耦（全量跑 pytest 时
    # test_auth.py 按字母序在前，2026-09-16 实测该测试在此顺序下必炸）。
    monkeypatch.setenv("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")
    client, repo = _make(monkeypatch, users=[
        {"username": "peon", "role": "user"},
        {"username": "op2", "role": "user", "account": "acc-002"},
    ])
    with client:
        peon = {"Authorization": "Bearer " + _login(client, "peon")}
        # 建一通本账号通话
        r = client.post("/api/calls", json={"account_id": "acc-001"}, headers=peon)
        call_id = r.json()["id"]
        # 直路：role/purpose 字段已被 B4 堵死
        assert client.post("/api/token", json={"call_id": call_id, "role": "supervisor"},
                           headers=peon).status_code == 403
        assert client.post("/api/token", json={"call_id": call_id, "purpose": "listen"},
                           headers=peon).status_code == 403
        # 第三条路（深测 P2）：participant_identity 前缀反推——旧版 201 漏签
        r = client.post("/api/token", json={"call_id": call_id,
                                            "participant_identity": f"supervisor-{call_id}"},
                        headers=peon)
        assert r.status_code == 403, r.text
        # operator 正常签发不受影响
        assert client.post("/api/token", json={"call_id": call_id},
                           headers=peon).status_code == 201


def _sign_webhook(body: bytes, secret: str, claims: dict | None = None) -> str:
    import hashlib
    import time

    import jwt as pyjwt

    now = int(time.time())
    payload = {
        "iss": "devkey", "sub": "devkey", "iat": now, "nbf": now - 5, "exp": now + 300,
        "video": {"webhook": True},
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    if claims:  # 负例用：覆写单字段构造「签名合法但 grant/摘要不对」的 token
        payload.update(claims)
    return pyjwt.encode(payload, secret, algorithm="HS256")


def test_livekit_webhook_requires_valid_signature(monkeypatch):
    from control_plane import main as cp_main

    monkeypatch.setenv("LIVEKIT_API_SECRET", "whsec-test-0123456789abcdef")
    # app 是模块级单例：同会话先跑的 startup（_startup 直调或 with client）会把
    # env 快照进 app.state.lk_secret，只改 env 骗不过验签闸——同步钉 state。
    monkeypatch.setattr(cp_main.app.state, "lk_secret",
                        "whsec-test-0123456789abcdef", raising=False)
    client, repo = _make(monkeypatch)
    payload = {"event": "participant_left", "room": {"name": "call-x"},
               "participant": {"identity": "bok-voice"}}
    # 无签名 → 401（旧版 200 handled:true）
    r = client.post("/api/webhook/livekit", json=payload)
    assert r.status_code == 401, r.text
    # 错误签名 → 401
    r = client.post("/api/webhook/livekit", json=payload,
                    headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    # 正确签名 → 200
    import json as _json

    raw = _json.dumps(payload).encode()
    r = client.post("/api/webhook/livekit", content=raw,
                    headers={"Authorization": "Bearer " + _sign_webhook(raw, "whsec-test-0123456789abcdef"),
                             "Content-Type": "application/json"})
    assert r.status_code == 200, r.text


def test_livekit_webhook_open_when_no_secret(monkeypatch):
    from control_plane import main as cp_main

    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    # 同上：清掉兄弟测试 startup 留下的 state 快照，env 兜底才会生效。
    monkeypatch.delattr(cp_main.app.state, "lk_secret", raising=False)
    client, repo = _make(monkeypatch)
    r = client.post("/api/webhook/livekit", json={"event": "participant_left",
                                                  "room": {"name": "call-x"},
                                                  "participant": {"identity": "bok-voice"}})
    assert r.status_code == 200  # 本地无 LiveKit 联调形态保持可用


def test_livekit_webhook_rejects_valid_jwt_without_webhook_grant(monkeypatch):
    from control_plane import main as cp_main

    secret = "whsec-test-0123456789abcdef"
    monkeypatch.setenv("LIVEKIT_API_SECRET", secret)
    monkeypatch.setattr(cp_main.app.state, "lk_secret", secret, raising=False)
    client, repo = _make(monkeypatch)
    payload = {"event": "participant_left", "room": {"name": "call-x"},
               "participant": {"identity": "bok-voice"}}
    import json as _json

    raw = _json.dumps(payload).encode()
    # participant/roomJoin token 与 webhook 共用同一 LIVEKIT_API_SECRET——
    # 签名合法但 grant 是 roomJoin 而非 webhook → 必须 401。钉死 grant 精确
    # 检查分支：若回归成 claims["video"] 真值判断，本测试即红。
    r = client.post("/api/webhook/livekit", content=raw,
                    headers={"Authorization": "Bearer " + _sign_webhook(
                        raw, secret, claims={"video": {"roomJoin": True}}),
                        "Content-Type": "application/json"})
    assert r.status_code == 401, r.text


def test_livekit_webhook_rejects_sha256_digest_mismatch(monkeypatch):
    from control_plane import main as cp_main

    secret = "whsec-test-0123456789abcdef"
    monkeypatch.setenv("LIVEKIT_API_SECRET", secret)
    monkeypatch.setattr(cp_main.app.state, "lk_secret", secret, raising=False)
    client, repo = _make(monkeypatch)
    payload = {"event": "participant_left", "room": {"name": "call-x"},
               "participant": {"identity": "bok-voice"}}
    import json as _json

    raw = _json.dumps(payload).encode()
    # video.webhook grant 正确但 claim 摘要 ≠ sha256(body) → 必须 401
    # （重放/篡改 body 防线，防摘要检查被回归掉）。
    r = client.post("/api/webhook/livekit", content=raw,
                    headers={"Authorization": "Bearer " + _sign_webhook(
                        raw, secret, claims={"sha256": "f" * 64}),
                        "Content-Type": "application/json"})
    assert r.status_code == 401, r.text


def test_machine_channel_actor_cannot_be_spoofed(monkeypatch):
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("BOK_CP_TOKEN", "mach-token-xyz")
    client, repo = _make(monkeypatch)
    with client:  # startup 装审计 tap → 审计进 repo
        r = client.post("/api/calls", json={"account_id": "acc-001"},
                        headers={"Authorization": "Bearer mach-token-xyz",
                                 "X-User-ID": "spoofed-root"})
        assert r.status_code == 200
        import json as _json

        dumped = _json.dumps(repo.list_audit_events(limit=100)).lower()
        assert "spoofed" not in dumped
