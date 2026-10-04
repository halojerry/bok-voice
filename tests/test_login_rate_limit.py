"""M-7（fix-wave-3，task-12 F-Major-2）：/api/auth/login 按用户名滑动窗口频控。

task-12 实测：100 连发错误密码登录 `401 ×100` 全放行——无限流、无锁定、无退避，
公网暴露前必须补。修法对齐既有 /api/token per-identity 频控件（30/min 滑窗）：
- 键=登录体里的 username（strip 后）——登录是预认证端点，JWT 身份尚不存在；
  IP 维不采（本栈全部调用方共享 loopback，IP 限流=本地组件互挤额度自伤），
  跨用户名喷洒的残余风险记录在案（报告）。
- 杀开关 `BOK_LOGIN_RATE_LIMIT`（默认开；"0"=关。CP 侧键走 _control_plane_env
  注入面，同 BOK_DISPATCH_RETRY 判例——不进 agent 面的 _FORWARD_ENV）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-auth-login-rl")

import pytest

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!rl"


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import control_plane.main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    # 隔离进程内已有窗口（与 test_token_dispatch 同款惯例）
    monkeypatch.setattr(cp_main, "_login_attempt_times", {})
    monkeypatch.delenv("BOK_LOGIN_RATE_LIMIT", raising=False)
    repo.create_user(
        username="rl-user", password_hash=hash_password(PW),
        role="user", org_id="org-rl", account_id="acc-001",
    )
    return TestClient(cp_main.app)


def _bad_login(client, username="rl-user"):
    return client.post("/api/auth/login", json={"username": username, "password": "definitely" + "-wrong"})


def test_login_rate_limit_429_after_30_per_username(client):
    """30 次内照常 401（无限流语义不变），第 31 次起 429。"""
    statuses = [_bad_login(client).status_code for _ in range(30)]
    assert set(statuses) == {401}
    r = _bad_login(client)
    assert r.status_code == 429, r.text
    assert "rate limited" in r.json()["detail"]


def test_login_rate_limit_per_username_isolation(client):
    """一个用户名打满额度不挤占别的用户名（token 频控 per-identity 同款纪律）。"""
    for _ in range(30):
        assert _bad_login(client, "rl-user").status_code == 401
    assert _bad_login(client, "rl-user").status_code == 429
    # 另一用户名（即使不存在）照常走 401 认证失败路径
    assert _bad_login(client, "other-user").status_code == 401


def test_login_rate_limit_counts_successful_logins_too(client):
    """成功登录同样计入额度（撞库混合正确密码的探测路径同样被盖）。"""
    for i in range(29):
        body = {"username": "rl-user", "password": PW if i % 5 == 0 else "wrong"}
        r = client.post("/api/auth/login", json=body)
        assert r.status_code in (200, 401)
    # 第 30 次仍在额度内（成功登录）
    assert client.post("/api/auth/login", json={"username": "rl-user", "password": PW}).status_code == 200
    assert _bad_login(client).status_code == 429


def test_login_rate_limit_kill_switch(client, monkeypatch):
    """BOK_LOGIN_RATE_LIMIT=0 → 不限流（100 连发全 401，旧行为逐字节）。"""
    monkeypatch.setenv("BOK_LOGIN_RATE_LIMIT", "0")
    for _ in range(35):
        assert _bad_login(client).status_code == 401


def test_login_rate_limit_whitespace_username_normalized(client):
    """键按 strip 归一：同名带空白变体共享同一预算（防绕过）。"""
    for _ in range(30):
        assert client.post(
            "/api/auth/login", json={"username": "rl-user", "password": "x"}
        ).status_code == 401
    assert client.post(
        "/api/auth/login", json={"username": "  rl-user  ", "password": "x"}
    ).status_code == 429


def test_login_rate_limit_key_space_bounded_under_spray(client, monkeypatch):
    """评审 I-1（fix round 1）：喷洒 5000 个不同用户名后键数有界不涨。

    预认证端点的键=请求体用户名（无身份可键），喷洒可无限撑大
    `_login_attempt_times`——键数封顶（超限先清过期键、仍超丢最旧），
    且喷洒途中既有用户名的频控语义不破。喷洒走 `_login_rate_limit` 直调
    （被测=淘汰逻辑本体；HTTP 全链路另由小样本腿盖，避免 5000×scrypt 拖套件）。
    """
    import control_plane.main as cp_main

    # 既有用户名攒满预算（HTTP 全链路）
    for _ in range(30):
        assert _bad_login(client, "rl-user").status_code == 401
    assert _bad_login(client, "rl-user").status_code == 429

    for i in range(5000):
        cp_main._login_rate_limit(f"spray-{i:05d}")

    assert len(cp_main._login_attempt_times) <= cp_main._LOGIN_RATE_MAX_KEYS

    # HTTP 小样本腿：端点→store 全链路键仍在场且计入
    assert _bad_login(client, "http-spray-probe").status_code == 401

    # 喷洒把最旧键挤出去后，rl-user 预算被逐出 → 重新计数（401 而非 429）：
    # 有界化的代价是「超额攻击可重置别人预算」，优于无界内存增长——
    # 键数封顶=DoS 防线，逐出顺序=最旧优先。
    r = _bad_login(client, "rl-user")
    assert r.status_code == 401


def test_login_rate_limit_eviction_prefers_expired_keys(client, monkeypatch):
    """有 expired 键在场时先清过期键：fresh 键不受株连。"""
    import time as _t

    import control_plane.main as cp_main

    stale = _t.monotonic() - cp_main._LOGIN_RATE_WINDOW_S - 10
    fresh = _t.monotonic()
    # 封顶个过期键 + 1 个 fresh 键，再触发一次新增 → 过期键全清、fresh 保留
    monkeypatch.setattr(
        cp_main, "_login_attempt_times",
        {f"old-{i:05d}": [stale] for i in range(cp_main._LOGIN_RATE_MAX_KEYS)}
        | {"fresh-key": [fresh]},
    )
    _bad_login(client, "newcomer")
    assert "fresh-key" in cp_main._login_attempt_times
    assert "newcomer" in cp_main._login_attempt_times
    assert not any(k.startswith("old-") for k in cp_main._login_attempt_times)


def test_login_username_max_length_422(client):
    """评审 I-2（fix round 1）：username>128 → 422（pydantic max_length），
    不进频控/查询路径。"""
    r = client.post("/api/auth/login", json={"username": "u" * 129, "password": "x"})
    assert r.status_code == 422
    # 边界值 128 合法形状（此用户不存在 → 401 认证失败路径）
    r2 = client.post("/api/auth/login", json={"username": "u" * 128, "password": "x"})
    assert r2.status_code == 401
