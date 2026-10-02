"""本地 LLM 端点自动发现离线单测（2026-09-27，Ethan：本地车道免手打端口/模型名）。

全部离线：`httpx.AsyncClient` 一律换成 `httpx.MockTransport` 工厂（monkeypatch
本模块的 `httpx` 名字，不碰全局 httpx——TestClient 也吃全局 httpx）。零外网、
零真栈。密钥值均为 fixture 假串。

覆盖：
- candidate_base_urls：env 优先/缺省端口/routing lanes+presets 纳入/去重/保序/坏串宽容；
- probe_endpoint：200→models 排序、连接拒绝→ok=False、401→ok=True error="auth"、
  超时→ok=False、空串不炸；绝不 raise；
- detect_local：gather 并发、可达在前、错误全折成数据；
- GET /api/model-routing/detect：权限矩阵（root/机器通道 200、admin/user 403、
  auth-on 匿名 401）、响应永不含 api_key、探活失败绝不 500；
- 源级 pin：detect 端点与 GET /api/model-routing 同一 root 闸。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "")  # 强制内存仓（SQL 路径另行集成测）
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane import model_routing_detect as detect_mod
from control_plane.auth import hash_password

PW = "Passw0rd!x"
MACHINE_TOKEN = "machine-token"
# fixture 假值（中性串，安全扫描友好，勿当真实凭据）
FIXTURE_KEY = "fixture-secret-key"
FIXTURE_BASE = "https://fixture.example/v1"

_SRC = (
    Path(__file__).resolve().parents[1]
    / "apps" / "control-plane" / "control_plane" / "main.py"
).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_routing_state(monkeypatch):
    """每测隔离：路由存储回内存空态 + 路由相关 env 清场（同 test_model_routing）。"""
    from control_plane import deps as cp_deps

    cp_deps._routing_state["memory"] = ""
    cp_deps._routing_state["session_factory"] = None
    monkeypatch.delenv("BOK_MODEL_ROUTING", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("MLX_LLM_BASE_URL", raising=False)


def _install_transport(monkeypatch, handler):
    """把 detect_mod 的 httpx 换成 MockTransport 工厂（handler 即假端点行为）。

    probe_endpoint 只引用本模块的 `httpx.AsyncClient`，故换本模块名字即可；
    全局 httpx 不动（TestClient 走自己的 transport）。
    """
    real_async_client = httpx.AsyncClient

    def factory(**kwargs):  # probe 传 timeout=…，MockTransport 忽略即可
        return real_async_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(detect_mod, "httpx", SimpleNamespace(AsyncClient=factory))


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    return TestClient(cp_main.app), repo


def _mk_user(repo, username, role, account="acc-001"):
    return repo.create_user(
        username=username, password_hash=hash_password(PW), role=role,
        org_id="org-t", account_id=account,
    )


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _machine():
    return {"Authorization": f"Bearer {MACHINE_TOKEN}"}


# ---- candidate_base_urls ----


def test_candidates_env_first_defaults_deduped_order_stable():
    env = {"MLX_LLM_BASE_URL": "http://127.0.0.1:1235/v1"}
    out = detect_mod.candidate_base_urls("", env)
    assert out[:5] == [
        "http://127.0.0.1:1235/v1",  # env 优先
        "http://127.0.0.1:1236/v1",
        "http://127.0.0.1:1237/v1",
        "http://127.0.0.1:18100/v1",
        "http://127.0.0.1:18101/v1",
    ]
    # env 值与缺省 :1235 同一端点 → 只出现一次
    assert out.count("http://127.0.0.1:1235/v1") == 1


def test_candidates_normalizes_missing_version_segment():
    # 裸 host:port（env 未带 /v1）规范成含 /v1 的 API 基址
    out = detect_mod.candidate_base_urls("", {"MLX_LLM_BASE_URL": "http://127.0.0.1:9999"})
    assert out[0] == "http://127.0.0.1:9999/v1"


def test_candidates_include_routing_lanes_and_presets_dedup():
    raw = json.dumps({
        "lanes": {"judge": {"provider": "local", "base_url": "http://127.0.0.1:1237/v1"}},
        "presets": {"p": {"a_reply": {"base_url": "http://127.0.0.1:9999/v1"}}},
    })
    out = detect_mod.candidate_base_urls(raw, {})
    # routing 里与缺省同端口 → 去重
    assert out.count("http://127.0.0.1:1237/v1") == 1
    # presets 端点纳入且排在缺省之后（routing 追加在最后）
    assert "http://127.0.0.1:9999/v1" in out
    assert out.index("http://127.0.0.1:9999/v1") > out.index("http://127.0.0.1:18101/v1")


def test_candidates_bad_raw_is_defaults_only():
    out = detect_mod.candidate_base_urls("{not json", {})
    assert out == [f"http://127.0.0.1:{p}/v1" for p in ("1235", "1236", "1237", "18100", "18101")]


# ---- probe_endpoint ----


def test_probe_ok_models_sorted(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:1235/v1/models"
        return httpx.Response(200, json={"data": [{"id": "m2"}, {"id": "m1"}]})

    _install_transport(monkeypatch, handler)
    out = asyncio.run(detect_mod.probe_endpoint("http://127.0.0.1:1235"))
    assert out == {
        "base_url": "http://127.0.0.1:1235/v1",
        "ok": True,
        "models": ["m1", "m2"],
        "error": "",
    }


def test_probe_connection_refused(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _install_transport(monkeypatch, handler)
    out = asyncio.run(detect_mod.probe_endpoint("http://127.0.0.1:1235/v1"))
    assert out["ok"] is False and out["models"] == []
    assert "ConnectError" in out["error"]


def test_probe_401_is_alive_with_auth_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    _install_transport(monkeypatch, handler)
    out = asyncio.run(detect_mod.probe_endpoint("https://auth.example/v1"))
    assert out["ok"] is True and out["models"] == [] and out["error"] == "auth"


def test_probe_timeout_is_ok_false(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    _install_transport(monkeypatch, handler)
    out = asyncio.run(detect_mod.probe_endpoint("http://10.255.255.1:1235/v1"))
    assert out["ok"] is False and out["models"] == []
    assert "TimeoutException" in out["error"]


def test_probe_empty_base_url_never_raises():
    out = asyncio.run(detect_mod.probe_endpoint("   "))
    assert out == {"base_url": "", "ok": False, "models": [], "error": "empty base_url"}


# ---- detect_local ----


def test_detect_reachable_first_and_error_containment(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if ":1235" in str(request.url):
            return httpx.Response(200, json={"data": [{"id": "solo"}]})
        raise httpx.ConnectError("refused")

    _install_transport(monkeypatch, handler)
    out = asyncio.run(detect_mod.detect_local("", {}))
    eps = out["endpoints"]
    assert eps[0] == {
        "base_url": "http://127.0.0.1:1235/v1",
        "ok": True,
        "models": ["solo"],
        "error": "",
    }
    assert all(e["ok"] is False for e in eps[1:])
    # 错误全部折成数据，绝不外抛；候选全集都在场
    assert len(eps) == 5
    assert all(isinstance(e["error"], str) for e in eps)


# ---- 端点：权限矩阵 + 无密钥 + 绝不 500 ----


def test_detect_endpoint_permission_matrix(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", MACHINE_TOKEN)
    _mk_user(repo, "rooty", "root", account="")
    _mk_user(repo, "boss", "admin")
    _mk_user(repo, "peon", "user")
    rt = _login(client, "rooty")
    at = _login(client, "boss")
    ut = _login(client, "peon")
    _install_transport(monkeypatch, _ok_handler)

    assert client.get("/api/model-routing/detect", headers=rt).status_code == 200
    assert client.get("/api/model-routing/detect", headers=_machine()).status_code == 200
    for h in (at, ut):
        assert client.get("/api/model-routing/detect", headers=h).status_code == 403
    # auth-on 匿名 401（全局门禁先拦）
    assert client.get("/api/model-routing/detect").status_code == 401


def test_detect_response_has_no_api_key(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    from control_plane.deps import write_model_routing_raw

    write_model_routing_raw(json.dumps({"lanes": {"judge": {
        "provider": "openai", "base_url": FIXTURE_BASE, "model": "cloud-model",
        "api_key": FIXTURE_KEY}}}))
    _install_transport(monkeypatch, _ok_handler)

    r = client.get("/api/model-routing/detect")  # auth-off 匿名直通（root 形态）
    assert r.status_code == 200
    body = r.json()
    dumped = json.dumps(body)
    assert FIXTURE_KEY not in dumped
    assert "api_key" not in dumped
    # routing 里的 base_url 作为候选被复探
    assert FIXTURE_BASE in [e["base_url"] for e in body["endpoints"]]


def test_detect_endpoint_never_500_on_probe_failure(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    _install_transport(monkeypatch, boom)
    r = client.get("/api/model-routing/detect")
    assert r.status_code == 200
    body = r.json()
    assert body["endpoints"]
    assert all(e["ok"] is False for e in body["endpoints"])


# ---- 源级 pin：detect 端点与 GET /api/model-routing 同 root 闸 ----


def test_detect_endpoint_gate_pinned():
    idx = _SRC.index('@app.get("/api/model-routing/detect")')
    seg = _SRC[idx:idx + 600]
    assert 'require_role(request, "root")' in seg

    # 与 GET /api/model-routing（非 detect/presets）同一 require_role 闸
    gidx = _SRC.index('@app.get("/api/model-routing")')
    assert 'require_role(request, "root")' in _SRC[gidx:gidx + 400]
