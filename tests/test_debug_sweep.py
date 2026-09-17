"""2026-09-17 全量 debug 收编测试(systematic-debugging 四阶段产物)。

每个测试对应一个已核实根因的 findings:
- B-F1 机器通道经 POST/PATCH /api/users 提权(auth-on 与 CP-token-only 双模式);
- B-F2 /api/webhook/livekit 在 optional_bearer_auth 豁免表缺失(CP-token-only 401);
- B-F7 scoped_account 对空账号 user 塌缩成 "" 通配符 → 跨账号列表;
- A-F1/F2 classic MiniMax 重连动作序列(抽出的 _minimax_classic_reconnect);
- A-F6 ControlPlaneClient.report_dial_result 重复定义(后者静默遮蔽前者);
- A-F5 ASR sidecar 会话表无 reaper(finish 永不到达时 PCM 驻留泄漏)。

测试口令全部走模块常量 PW(测试夹具,非真实凭据)。
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-auth-sweep")
PW = "Passw0rd!x"  # 测试夹具口令(与 test_auth.py 同源),非真实凭据


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    sys.path.insert(0, str(ROOT / "packages" / "business-db"))
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


# ---- B-F1: 机器通道不得触账号管理面 ----


def test_machine_token_cannot_mint_root(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    r = client.post(
        "/api/users",
        json={"username": "escalated", "password": PW, "role": "root"},
        headers={"Authorization": "Bearer machine-token"},
    )
    assert r.status_code == 403
    assert repo.get_user_by_username("escalated") is None


def test_machine_token_cannot_reset_root_password(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    root = repo.create_user(
        username="rootx", password_hash="x" * 8, role="root", org_id="o", account_id="acc-001"
    )
    r = client.patch(
        f"/api/users/{root['id']}",
        json={"password": PW},
        headers={"Authorization": "Bearer machine-token"},
    )
    assert r.status_code == 403


def test_machine_token_cannot_list_users(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    r = client.get("/api/users", headers={"Authorization": "Bearer machine-token"})
    assert r.status_code == 403


def test_cp_token_only_mode_user_admin_denied(monkeypatch):
    """BOK_AUTH_REQUIRED 未设、仅 BOK_CP_TOKEN:identity_gate 早退不打 machine 标,
    判据必须落在「加固模式」而非 flag(_hardened_auth 第二析支)。"""
    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    r = client.post(
        "/api/users",
        json={"username": "escalated2", "password": PW, "role": "root"},
        headers={"Authorization": "Bearer machine-token"},
    )
    assert r.status_code == 403


def test_dual_off_user_admin_unchanged(monkeypatch):
    """真 auth-off(双关):本机单用户形态零变化。"""
    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    r = client.post(
        "/api/users",
        json={"username": "localop", "password": PW, "role": "user"},
    )
    assert r.status_code == 200


# ---- B-F2: webhook 豁免对齐 ----


def test_webhook_bypasses_cp_token_gate(monkeypatch):
    from control_plane import main as cp_main

    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("LIVEKIT_API_SECRET", "")
    # app 是模块级单例:startUp 把 env 快照进 app.state.lk_secret,只改 env 骗不过
    # 验签闸(同 test_security_hardening.py:149 注记)——同步钉 state 走 fail-open 档。
    monkeypatch.setattr(cp_main.app.state, "lk_secret", "", raising=False)
    r = client.post("/api/webhook/livekit", json={})
    assert r.status_code != 401  # 中间件层不得拦(端点内另有签名校验语义)


# ---- B-F7: 空账号 user 不得拿通配符视图 ----


def test_empty_account_user_scoped_out(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    from control_plane.auth import hash_password

    repo.create_user(
        username="ghost-acct", password_hash=hash_password(PW),
        role="user", org_id="o", account_id="",
    )
    r = client.post("/api/auth/login", json={"username": "ghost-acct", "password": PW})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    r2 = client.get("/api/calls", headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 403  # fail-closed: "" 通配符视图不得出现


# ---- A-F6: dup 方法定义 ----


def test_report_dial_result_defined_once():
    sys.path.insert(0, str(ROOT / "apps" / "agent"))
    from agent_runtime.control_plane import ControlPlaneClient

    src = inspect.getsource(ControlPlaneClient)
    assert src.count("async def report_dial_result") == 1


# ---- A-F1/F2: classic 重连动作序列 ----


class _FakeWS:
    def __init__(self, tag: str):
        self.tag = tag
        self.sent: list[str] = []
        self.closed = False

    async def close(self):
        self.closed = True

    async def send(self, payload: str):
        self.sent.append(payload)


class _StubTTS:
    def _endpoint_ws(self) -> str:
        return "ws://minimax.fake"


def test_classic_reconnect_resends_sent_texts(monkeypatch):
    sys.path.insert(0, str(ROOT / "apps" / "agent"))
    import websockets

    from agent_runtime.providers import livekit_plugins as lp

    old, new = _FakeWS("old"), _FakeWS("new")
    handshake_targets: list[str] = []

    async def fake_connect(endpoint, **kwargs):
        return new

    async def fake_handshake(ws):
        handshake_targets.append(ws.tag)

    # helper 是局部 import websockets——patch 同一模块对象属性即生效。
    monkeypatch.setattr(websockets, "connect", fake_connect)
    ws = asyncio.run(lp._minimax_classic_reconnect(
        _StubTTS(), "k", old, fake_handshake, ["句一。", "句二。"]))
    assert old.closed and ws is new
    assert handshake_targets == ["new"]
    assert [json.loads(t)["text"] for t in new.sent] == ["句一。", "句二。"]


def test_classic_reconnect_failure_propagates(monkeypatch):
    sys.path.insert(0, str(ROOT / "apps" / "agent"))
    import websockets

    async def fake_connect(endpoint, **kwargs):
        raise RuntimeError("cloud wedged")

    monkeypatch.setattr(websockets, "connect", fake_connect)

    async def fake_handshake(ws):  # pragma: no cover - 不可达
        raise AssertionError("handshake must not run after failed connect")

    from agent_runtime.providers import livekit_plugins as lp

    with pytest.raises(RuntimeError):
        asyncio.run(lp._minimax_classic_reconnect(
            _StubTTS(), "k", _FakeWS("old"), fake_handshake, ["a。"]))


# ---- A-F5: sidecar 会话 reaper ----


def _load_sidecar_app():
    os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_sweep", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_asr_sidecar_sweeps_stale_sessions():
    mod = _load_sidecar_app()
    svc = mod.ASRService()
    s1 = svc.start()
    # 回拨 created_at 到 TTL 之外
    svc._sessions[s1]["created_at"] -= (svc.SESSION_TTL_S + 10)
    s2 = svc.start()  # start 自带懒清扫
    assert s1 not in svc._sessions
    assert s2 in svc._sessions


def test_asr_sidecar_caps_session_count():
    mod = _load_sidecar_app()
    svc = mod.ASRService()
    svc.SESSION_MAX = 5
    ids = [svc.start() for _ in range(7)]
    assert len(svc._sessions) <= 5
    # 清的是最旧,留最新
    assert ids[-1] in svc._sessions
    assert ids[0] not in svc._sessions
