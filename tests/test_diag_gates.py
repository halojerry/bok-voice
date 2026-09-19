"""P3-A 诊断路由闸与 per-identity 限速窗收编测试（2026-09-17 全量 debug）。

契约：
- 诊断读面（/api/asr/health、/api/tts/health、/api/tts/speakers、/api/tts/voices、
  /api/tts/filler-preview）挂 settings 页键闸——auth-on 下 user JWT 403，
  双关 auth-off 直通（sidecar 缺席=503，非 403）；
- /api/web_logs 保留 user 可写，但限速窗改 per-identity——一个身份打满 600/min
  不再挤占其他调用方额度。

测试口令走模块常量 PW（测试夹具，非真实凭据）。
"""
from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-diag-gates")
PW = "Passw0rd!x"  # 测试夹具口令(与 test_auth.py 同源),非真实凭据

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

_DIAG_PATHS = (
    "/api/asr/health",
    "/api/tts/health",
    "/api/tts/speakers",
    "/api/tts/voices",
    "/api/tts/filler-preview",
)


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
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---- P3-A: user 403 ×5 端点 ----


def test_diag_routes_user_403(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    _mk_user(repo, "peon")
    tok = _login(client, "peon")
    for path in _DIAG_PATHS:
        r = client.get(path, headers=_auth(tok))
        assert r.status_code == 403, f"{path}: {r.status_code}"


def test_diag_routes_dual_off_not_blocked(monkeypatch):
    """双关 auth-off 直通：诊断面行为零变化（sidecar 缺席=503、资产在=200，均非 403）。"""
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    for path in _DIAG_PATHS:
        r = client.get(path)
        assert r.status_code != 403, f"{path}: {r.status_code}"


def test_diag_routes_admin_allowed(monkeypatch):
    """admin 直通（settings 属管理面默认集）。"""
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    _mk_user(repo, "boss", role="admin")
    tok = _login(client, "boss")
    r = client.get("/api/tts/filler-preview", headers=_auth(tok))
    assert r.status_code == 200, r.text


# ---- P3-A: web_logs per-identity 限速窗 ----


def test_weblog_limiter_per_identity(monkeypatch, tmp_path):
    """限速窗仍护机器通道。2026-09-19 审计 P2-4 起端点挂 settings 闸:任意 user
    恒 403（见 test_cp_wave1）、anon 在 auth-on 下 401——per-identity 面收敛到
    machine 一档,限速语义原样保留(600 行/分钟防刷盘)。"""
    import control_plane.main as cp_main

    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_APP_DATA", str(tmp_path))  # 日志写进临时目录,不碰真实 app-data
    monkeypatch.setattr(cp_main, "_weblog_times", {})  # 隔离进程内已有窗口
    # auth-on:state.machine 只由 identity_gate 置位(CP-token-only 中间件只验不标)
    # ——不置位则限速键落 "anon",种子打不中。
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_JWT_SECRET", "diag-gates-fixture-secret-0123456789abcdef")
    monkeypatch.setenv("BOK_CP_TOKEN", "diag-machine-token")
    machine_h = {"Authorization": "Bearer diag-machine-token"}
    # machine 打满 600/min 窗
    cp_main._weblog_times["machine"] = deque([time.time()] * 600, maxlen=600)
    r = client.post("/api/web_logs", json={"event": "flood"}, headers=machine_h)
    assert r.status_code == 200
    assert r.json() == {"ok": False, "reason": "rate_limited"}
    # 窗口释放后恢复
    cp_main._weblog_times["machine"].clear()
    r3 = client.post("/api/web_logs", json={"event": "after-window"}, headers=machine_h)
    assert r3.json() == {"ok": True}


def test_weblog_empty_event_still_rejected(monkeypatch, tmp_path):
    """限速放行后，空 event 仍按原语义拒（{"ok": False}）。"""
    import control_plane.main as cp_main

    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_APP_DATA", str(tmp_path))
    monkeypatch.setattr(cp_main, "_weblog_times", {})
    r = client.post("/api/web_logs", json={"event": ""})
    assert r.json() == {"ok": False}


# ---- P3-C: webhook 无 secret 时的 fail-open 收窄 ----


def test_webhook_auth_on_without_secret_rejected(monkeypatch):
    """P3-C：auth-on 漏配 LIVEKIT_API_SECRET=配置事故 → 401 拒收（不再 fail-open）。"""
    from control_plane import main as cp_main

    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "")
    # app 是模块级单例：startup 会把 env 快照进 app.state.lk_secret，只改 env
    # 骗不过验签闸——同步钉 state 走「secret 空」档（同 test_debug_sweep 手法）。
    monkeypatch.setattr(cp_main.app.state, "lk_secret", "", raising=False)
    r = client.post("/api/webhook/livekit", json={})
    assert r.status_code == 401
    assert "LIVEKIT_API_SECRET" in r.json()["detail"]


def test_webhook_dual_off_without_secret_fail_open(monkeypatch):
    """双关 auth-off（本地无 LiveKit 联调）保留放行——dev 形态零变化。"""
    from control_plane import main as cp_main

    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    monkeypatch.setenv("LIVEKIT_API_SECRET", "")
    monkeypatch.setattr(cp_main.app.state, "lk_secret", "", raising=False)
    r = client.post("/api/webhook/livekit", json={})
    assert r.status_code != 401


def test_webhook_cp_token_only_without_secret_fail_open(monkeypatch):
    """CP-token-only + 无 secret 保留放行：F2 修复语义（tests/test_debug_sweep
    ::test_webhook_bypasses_cp_token_gate 钉死）优先于 P3-C 的 auth-on 档。"""
    from control_plane import main as cp_main

    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("LIVEKIT_API_SECRET", "")
    monkeypatch.setattr(cp_main.app.state, "lk_secret", "", raising=False)
    r = client.post("/api/webhook/livekit", json={})
    assert r.status_code != 401
