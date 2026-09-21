"""启动锚与 webhook 拒收收窄测试（2026-09-21 安全收编）。

契约（Item 1 / Item 4）：
- 非回环 bind × 认证双关（BOK_AUTH_REQUIRED / BOK_CP_TOKEN 均未设）= 全 API 裸放行
  对外 → startup 拒绝启动 + CORS 不回落 "*"；回环 bind 行为逐字节不变（本机单用户
  开发形态零变化）；
- LIVEKIT_API_SECRET 未配置时，webhook 只在「回环 bind + 双关 auth-off」保留
  fail-open（F2 契约：本地无 LiveKit 联调依赖 webhook 崩溃补位）；auth-on 或非
  回环 bind 一律 401。

测试口令/密钥走模块常量（测试夹具，非真实凭据）。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-bind-guard")

import pytest

from control_plane import main as cp_main


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(cp_main.app), repo


# ---- Item 1: 回环判定 / 纯判定 / host 解析 / CORS ----


def test_is_loopback_host():
    for host in ("127.0.0.1", "127.0.0.5", "::1", "[::1]", "localhost", "LOCALHOST", ""):
        assert cp_main._is_loopback_host(host), host
    for host in ("0.0.0.0", "192.168.1.10", "10.0.0.1", "cp.example.com", "::"):
        assert not cp_main._is_loopback_host(host), host


def test_unsafe_open_bind_truth_table():
    # 非回环 × 双关 = 危险
    assert cp_main._unsafe_open_bind("0.0.0.0", auth_on=False, cp_token="")
    assert cp_main._unsafe_open_bind("192.168.1.10", auth_on=False, cp_token="")
    # 任一认证 env 在场即放行
    assert not cp_main._unsafe_open_bind("0.0.0.0", auth_on=True, cp_token="")
    assert not cp_main._unsafe_open_bind("0.0.0.0", auth_on=False, cp_token="tok")
    # 纯空白 env 等价未设（全仓一律 .strip() 判定）→ 仍属双关
    assert cp_main._unsafe_open_bind("0.0.0.0", auth_on=False, cp_token="   ")
    # 回环 bind 恒放行（本机单用户形态零变化）
    assert not cp_main._unsafe_open_bind("127.0.0.1", auth_on=False, cp_token="")
    assert not cp_main._unsafe_open_bind("", auth_on=False, cp_token="")


def test_resolved_bind_host_argv_then_env(monkeypatch):
    # argv（uvicorn 实际吃的值）优先
    assert cp_main._resolved_bind_host(["uvicorn", "app", "--host", "0.0.0.0", "--port", "8000"]) == "0.0.0.0"
    assert cp_main._resolved_bind_host(["uvicorn", "app", "--host=0.0.0.0"]) == "0.0.0.0"
    # argv 无 --host → BOK_BIND_HOST（tools/bok.py 单一来源）
    monkeypatch.setenv("BOK_BIND_HOST", "10.0.0.1")
    assert cp_main._resolved_bind_host(["uvicorn", "app"]) == "10.0.0.1"
    # 两者皆无 → uvicorn 默认回环
    monkeypatch.delenv("BOK_BIND_HOST", raising=False)
    assert cp_main._resolved_bind_host(["uvicorn", "app"]) == ""
    assert cp_main._is_loopback_host(cp_main._resolved_bind_host(["uvicorn", "app"]))


def test_cors_allow_origins_gated_by_same_check():
    # 裸放行 × 非回环：不回落 "*"
    assert cp_main._cors_allow_origins([], "0.0.0.0", auth_on=False, cp_token="") == []
    # 回环 / 任一认证 env：维持 "*" 缺省
    assert cp_main._cors_allow_origins([], "127.0.0.1", auth_on=False, cp_token="") == ["*"]
    assert cp_main._cors_allow_origins([], "0.0.0.0", auth_on=True, cp_token="") == ["*"]
    assert cp_main._cors_allow_origins([], "0.0.0.0", auth_on=False, cp_token="tok") == ["*"]
    # 显式配置恒照用
    assert cp_main._cors_allow_origins(["https://x"], "0.0.0.0", auth_on=False, cp_token="") == ["https://x"]


def test_module_wiring_non_loopback_env():
    """真 import（子进程）覆盖 import 期装配：非回环 env 下 CORS 不回落 "*" 且
    startup 拒绝。纯函数测不到这层——env 缺省值曾在装配处把「显式配置」与
    「缺省」混同，令收窄永不生效（装配 bug 只有真 import 能逮）。"""
    import json
    import subprocess
    import sys

    parts = (
        "packages/core",
        "packages/business-db",
        "packages/knowledge",
        "packages/observability",
        "apps/control-plane",
    )
    env = {k: v for k, v in os.environ.items() if k not in ("BOK_AUTH_REQUIRED", "BOK_CP_TOKEN")}
    env.update({
        "DATABASE_URL": "",
        "BOK_BIND_HOST": "0.0.0.0",
        "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in parts),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    code = (
        "import json\n"
        "import control_plane.main as m\n"
        "from starlette.middleware.cors import CORSMiddleware as C\n"
        "cors = [mw for mw in m.app.user_middleware if mw.cls is C]\n"
        "refused = False\n"
        "try:\n"
        "    m._startup()\n"
        "except RuntimeError:\n"
        "    refused = True\n"
        "print(json.dumps({'origins': cors[0].kwargs['allow_origins'] if cors else None,"
        " 'refused': refused}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=str(ROOT),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["refused"] is True
    assert payload["origins"] == []


def test_startup_refuses_non_loopback_without_auth(monkeypatch):
    monkeypatch.setenv("BOK_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    # 拒绝发生在任何 DB 迁移/种子之前（副作用为零）
    with pytest.raises(RuntimeError, match="裸放行"):
        cp_main._startup()


def test_startup_refusal_uses_uvicorn_argv_host(monkeypatch):
    """容器 CMD 形态（Dockerfile --host 0.0.0.0，无 BOK_BIND_HOST）同样被拦。"""
    monkeypatch.delenv("BOK_BIND_HOST", raising=False)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]
    )
    with pytest.raises(RuntimeError, match="0.0.0.0"):
        cp_main._startup()


# ---- Item 4: webhook 无 secret 时的拒收面 ----


def _no_secret(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_SECRET", "")
    # app 是模块级单例：startup 会把 env 快照进 app.state.lk_secret，只改 env
    # 骗不过验签闸——同步钉 state 走「secret 空」档（同 test_diag_gates 手法）。
    monkeypatch.setattr(cp_main.app.state, "lk_secret", "", raising=False)


def test_webhook_non_loopback_without_secret_rejected(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_BIND_HOST", "0.0.0.0")  # 非回环 bind
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    _no_secret(monkeypatch)
    r = client.post("/api/webhook/livekit", json={})
    assert r.status_code == 401
    assert "LIVEKIT_API_SECRET" in r.json()["detail"]


def test_webhook_loopback_dual_off_keeps_carve_out(monkeypatch):
    """唯一 carve-out：回环 bind + 双关 auth-off（本地无 LiveKit 联调，F2 契约）。"""
    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.delenv("BOK_BIND_HOST", raising=False)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    _no_secret(monkeypatch)
    r = client.post("/api/webhook/livekit", json={})
    assert r.status_code != 401
