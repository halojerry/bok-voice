"""模型路由统一 CP 侧（2026-09-25 阶段 0）：端点权限矩阵/掩码/预置/探活/车道消费。

共享契约（packages/core/bok_voice_core/model_routes.py）行为面另有
tests/test_model_routes.py 钉住；本文件只测 CP 集成：
- GET/PUT /api/model-routing + 预置三件 + /test 探活（root+机器通道专属，
  admin/user 403，auth-off 直通——/api/nodes require_role("root") 先例）；
- 掩码回读恒空串+has_api_key（明文仅 ?internal=1，同 settings 先例）；
- PUT 空 api_key=保留旧值（sms secret 先例）；审计零密钥材料；
- settings?internal=1 附 model_routing_json 原始串、掩码面完全不含；
- settle（summarize）/mining（qa_cluster）车道消费：env 档逐字节同旧、
  云端档带 api_key + enable_thinking、kill-switch 字节同旧。

全部 key 值均为 fixture 假值（中性串），不对应任何真实端点/凭据；
探活/消费测试全部 monkeypatch httpx，零外网。
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "")  # 强制内存仓（SQL 路径另行集成测）
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"
MACHINE_TOKEN = "machine-token"
# fixture 假值（中性串，安全扫描友好，勿当真实凭据）
CLOUD_KEY = "fixture-key"
CLOUD_BASE = "https://fixture.example/v1"


@pytest.fixture(autouse=True)
def _isolated_routing_state(monkeypatch):
    """每测隔离：路由存储回内存空态 + 路由相关 env 清场。

    测试不走 lifespan（TestClient 不进 with）→ session_factory 恒 None →
    deps 存储自然落内存分支；显式清一遍防其他测试残留。
    """
    from control_plane import deps as cp_deps

    cp_deps._routing_state["memory"] = ""
    cp_deps._routing_state["session_factory"] = None
    monkeypatch.delenv("BOK_MODEL_ROUTING", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)


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


def _put_judge_cloud(client, *, api_key=CLOUD_KEY, **overrides):
    """PUT judge=云端档（其余车道缺省），回响应。"""
    lane = {"provider": "openai", "base_url": CLOUD_BASE, "model": "judge-model"}
    if api_key is not None:
        lane["api_key"] = api_key
    lane.update(overrides)
    return client.put("/api/model-routing", json={"lanes": {"judge": lane}})


# ---- 权限矩阵（红线：admin 与话务员不允许，计划 §2.4）----


def test_permission_matrix(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", MACHINE_TOKEN)
    _mk_user(repo, "rooty", "root", account="")
    _mk_user(repo, "boss", "admin")
    _mk_user(repo, "peon", "user")
    rt = _login(client, "rooty")
    at = _login(client, "boss")
    ut = _login(client, "peon")

    # root 200（读/写）
    assert client.get("/api/model-routing", headers=rt).status_code == 200
    r = client.put("/api/model-routing", headers=rt, json={"lanes": {"judge": {
        "provider": "openai", "base_url": CLOUD_BASE, "model": "judge-model",
        "api_key": CLOUD_KEY}}})
    assert r.status_code == 200
    # 机器通道 200
    assert client.get("/api/model-routing", headers=_machine()).status_code == 200
    # admin/user 403（读/写/预置/探活全闸）
    for h in (at, ut):
        assert client.get("/api/model-routing", headers=h).status_code == 403
        assert client.get("/api/model-routing?internal=1", headers=h).status_code == 403
        assert client.put("/api/model-routing", headers=h, json={"lanes": {}}).status_code == 403
        assert client.post("/api/model-routing/presets", headers=h, json={"name": "x"}).status_code == 403
        assert client.post("/api/model-routing/test", headers=h, json={"lane": "judge"}).status_code == 403
    # auth-on 匿名 401（全局门禁先拦）
    assert client.get("/api/model-routing").status_code == 401


def test_auth_off_anonymous_passthrough(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    # BOK_AUTH_REQUIRED 未设（autouse 已清）→ 匿名直通（单机形态零变化）
    assert client.get("/api/model-routing").status_code == 200
    assert client.put("/api/model-routing", json={"lanes": {}}).status_code == 200


# ---- 掩码 vs internal 明文 ----


def test_masked_read_vs_internal_plain(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    assert _put_judge_cloud(client).status_code == 200

    masked = client.get("/api/model-routing").json()
    assert masked["lanes"]["judge"]["api_key"] == ""
    assert masked["lanes"]["judge"]["has_api_key"] is True
    assert masked["lanes"]["judge"]["provider"] == "openai"
    assert masked["lanes"]["judge"]["base_url"] == CLOUD_BASE
    # 未配置车道=缺省视图（local/全空/无 key）
    assert masked["lanes"]["mt"]["provider"] == "local"
    assert masked["lanes"]["mt"]["has_api_key"] is False

    internal = client.get("/api/model-routing?internal=1").json()
    assert internal["lanes"]["judge"]["api_key"] == CLOUD_KEY

    # 机器通道同样可读明文（agent 面下发通道）
    assert client.get("/api/model-routing?internal=1", headers=_machine()).json()[
        "lanes"]["judge"]["api_key"] == CLOUD_KEY


def test_put_validation_400_and_unknown_lane_dropped(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    # openai 档缺 base_url/model → 400 {"errors": [...]}
    r = client.put("/api/model-routing", json={"lanes": {"judge": {"provider": "openai"}}})
    assert r.status_code == 400
    body = r.json()
    assert body.get("errors") and len(body["errors"]) == 2

    # 未知车道原样放行但被契约丢弃（宽容面）——不产生错误也不落库
    r = client.put("/api/model-routing", json={"lanes": {"ghost": {"provider": "openai"}}})
    assert r.status_code == 200
    assert "ghost" not in client.get("/api/model-routing").json()["lanes"]


def test_put_empty_key_keeps_old(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    assert _put_judge_cloud(client).status_code == 200
    # 再 PUT：api_key 空缺=保留旧值（sms secret 先例）
    r = client.put("/api/model-routing", json={"lanes": {"judge": {
        "provider": "openai", "base_url": CLOUD_BASE, "model": "judge-model-2"}}})
    assert r.status_code == 200
    internal = client.get("/api/model-routing?internal=1").json()
    assert internal["lanes"]["judge"]["api_key"] == CLOUD_KEY
    assert internal["lanes"]["judge"]["model"] == "judge-model-2"


def test_audit_has_no_key_material(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    from control_plane import main as cp_main

    events: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)))
    assert _put_judge_cloud(client).status_code == 200
    actions = [a for a, _ in events]
    assert "model_routing.update" in actions
    # 逐事件扫：任何 model_routing.* 审计 detail 都不得含密钥材料
    for action, kw in events:
        if str(action).startswith("model_routing."):
            assert CLOUD_KEY not in json.dumps(kw.get("detail") or {})


# ---- 预置三件：存/套/删 roundtrip（套档不清密钥）----


def test_preset_roundtrip_keeps_keys(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    assert _put_judge_cloud(client).status_code == 200

    # 存快照：剥 api_key
    r = client.post("/api/model-routing/presets", json={"name": "演示档"})
    assert r.status_code == 200
    preset = r.json()["presets"]["演示档"]["judge"]
    assert preset["api_key"] == "" and preset["has_api_key"] is False
    assert preset["provider"] == "openai"

    # 把 judge 拨回 local，再套预置 → 回云端档且密钥无损
    assert client.put("/api/model-routing", json={"lanes": {"judge": {
        "provider": "local", "base_url": "", "model": ""}}}).status_code == 200
    r = client.post("/api/model-routing/presets/演示档/apply")
    assert r.status_code == 200
    masked = r.json()["lanes"]["judge"]
    assert masked["provider"] == "openai" and masked["base_url"] == CLOUD_BASE
    assert masked["api_key"] == "" and masked["has_api_key"] is True  # 掩码面
    internal = client.get("/api/model-routing?internal=1").json()
    assert internal["lanes"]["judge"]["api_key"] == CLOUD_KEY  # 套档不清密钥

    # 删：roundtrip 收口；缺失条目 404
    assert client.delete("/api/model-routing/presets/演示档").status_code == 200
    assert client.get("/api/model-routing").json()["presets"] == {}
    assert client.delete("/api/model-routing/presets/演示档").status_code == 404
    assert client.post("/api/model-routing/presets/演示档/apply").status_code == 404


def test_preset_name_sanitized(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    # 路径字符剥除后仍合法 → 收进剥后名
    r = client.post("/api/model-routing/presets", json={"name": "a/b\\c:d"})
    assert r.status_code == 200
    assert "abcd" in r.json()["presets"]
    # 剥完为空 / 超长 → 400
    assert client.post("/api/model-routing/presets", json={"name": "///"}).status_code == 400
    assert client.post("/api/model-routing/presets", json={"name": "x" * 33}).status_code == 400
    assert client.post(
        "/api/model-routing/presets", json={"name": "x" * 32}
    ).status_code == 200


# ---- settings 载荷：internal 附 raw 键，掩码面完全不含 ----


def test_settings_internal_carries_raw_key_only(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    assert _put_judge_cloud(client).status_code == 200
    internal = client.get("/api/settings?internal=1").json()
    assert CLOUD_KEY in str(internal.get("model_routing_json", ""))
    masked = client.get("/api/settings").json()
    assert "model_routing_json" not in masked


# ---- 探活端点（monkeypatch httpx，零外网；失败绝不 500）----


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_probe_ok_and_model_discovery(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    # 云端档带 model：直探 /chat/completions
    assert _put_judge_cloud(client).status_code == 200
    calls: list[tuple] = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls.append(("post", url, headers))
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    r = client.post("/api/model-routing/test", json={"lane": "judge"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["model"] == "judge-model" and body["error"] is None
    assert isinstance(body["latency_ms"], int)
    _, url, headers = calls[0]
    assert url == f"{CLOUD_BASE}/chat/completions"
    assert headers["Authorization"] == f"Bearer {CLOUD_KEY}"  # 云端档带 key

    # local 显式端点 + model 空 → 先 /models 发现取第一个 id
    assert client.put("/api/model-routing", json={"lanes": {"mining": {
        "provider": "local", "base_url": "http://fixture-local:1235/v1"}}}).status_code == 200

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(("get", url, headers))
        return _FakeResp({"data": [{"id": "/models/mini-4b"}, {"id": "other"}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    r = client.post("/api/model-routing/test", json={"lane": "mining"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["model"] == "/models/mini-4b"
    # local 档不带 Authorization（api_key="mlx" 不得进请求头）
    assert calls[-1][2] == {} or calls[-1][2] is None


def test_probe_failure_is_ok_false_never_500(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    assert _put_judge_cloud(client).status_code == 200

    def fake_post(*a, **kw):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    r = client.post("/api/model-routing/test", json={"lane": "judge"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "boom" in str(body["error"])


def test_probe_mt_unconfigured_and_unknown_lane(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    monkeypatch.delenv("MT_LLM_BASE_URL", raising=False)
    # mt 旧回退语义：无显式端点 → 400 人话
    r = client.post("/api/model-routing/test", json={"lane": "mt"})
    assert r.status_code == 400
    assert r.json()["error"] == "该车道未配置显式端点"
    # 未知车道 → 400（ok=false 形状之外的参数错误）
    assert client.post("/api/model-routing/test", json={"lane": "ghost"}).status_code == 400


# ---- CP 侧车道消费：settle（summarize）/ mining（qa_cluster）----


def _fake_llm_response(summary: str) -> _FakeResp:
    return _FakeResp({"choices": [{"message": {"content": json.dumps({
        "summary": summary, "new_topics": [], "insight": None})}}]})


class _Turn:
    def __init__(self, role: str, transcript: str):
        self.role = role
        self.transcript = transcript


def test_summarize_env_path_byte_identical(monkeypatch):
    """settle 车道铁律：路由未命中时 env 链逐字节同旧（无 Authorization 头、
    请求体无 enable_thinking 扩展）。"""
    from control_plane import deps as cp_deps
    from control_plane.summarize import Summarizer

    monkeypatch.setenv("BOK_SETTLE_LLM_BASE_URL", "http://settle-fixture:1237/v1")
    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", "settle-9b")
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured.update({"url": url, "payload": json, "headers": headers})
        return _fake_llm_response("s")

    monkeypatch.setattr(httpx, "post", fake_post)
    out = Summarizer().build([_Turn("user", "你好")], {"object_id": "o"}, {})
    assert out["summary"] == "s"
    assert captured["url"] == "http://settle-fixture:1237/v1/chat/completions"
    assert captured["headers"] is None
    assert "enable_thinking" not in captured["payload"]
    assert cp_deps.read_model_routing_raw() == ""


def test_summarize_cloud_route_with_thinking(monkeypatch):
    from control_plane.deps import write_model_routing_raw
    from control_plane.summarize import Summarizer

    write_model_routing_raw(json.dumps({"lanes": {"settle": {
        "provider": "openai", "base_url": CLOUD_BASE, "model": "settle-cloud",
        "api_key": CLOUD_KEY, "extra": {"enable_thinking": True}}}}))
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured.update({"url": url, "payload": json, "headers": headers})
        return _fake_llm_response("s")

    monkeypatch.setattr(httpx, "post", fake_post)
    out = Summarizer().build([_Turn("user", "你好")], {"object_id": "o"}, {})
    assert out["summary"] == "s"
    assert captured["url"] == f"{CLOUD_BASE}/chat/completions"
    assert captured["payload"]["model"] == "settle-cloud"
    assert captured["payload"]["enable_thinking"] is True
    assert captured["headers"] == {"Authorization": f"Bearer {CLOUD_KEY}"}


def test_summarize_kill_switch_env_byte_identical(monkeypatch):
    """BOK_MODEL_ROUTING=0：路由表全忽略、字节同旧。"""
    from control_plane.deps import write_model_routing_raw
    from control_plane.summarize import Summarizer

    write_model_routing_raw(json.dumps({"lanes": {"settle": {
        "provider": "openai", "base_url": CLOUD_BASE, "model": "settle-cloud",
        "api_key": CLOUD_KEY, "extra": {"enable_thinking": True}}}}))
    monkeypatch.setenv("BOK_MODEL_ROUTING", "0")
    monkeypatch.setenv("BOK_SETTLE_LLM_BASE_URL", "http://settle-fixture:1237/v1")
    monkeypatch.setenv("BOK_SETTLE_LLM_MODEL", "settle-9b")
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured.update({"url": url, "payload": json, "headers": headers})
        return _fake_llm_response("s")

    monkeypatch.setattr(httpx, "post", fake_post)
    Summarizer().build([_Turn("user", "你好")], {"object_id": "o"}, {})
    assert captured["url"].startswith("http://settle-fixture:1237/v1")
    assert captured["headers"] is None
    assert "enable_thinking" not in captured["payload"]


def test_qa_cluster_mining_lane(monkeypatch):
    from control_plane import deps as cp_deps
    from control_plane import qa_cluster as qcm

    # env 档（路由未命中）：MLX 缺省链，无覆盖无 key
    monkeypatch.delenv("MLX_LLM_BASE_URL", raising=False)
    assert qcm._mining_lane() == ("http://127.0.0.1:1235/v1", "", "", False)

    # 云端档：base_url/model/api_key/thinking 全量出
    cp_deps.write_model_routing_raw(json.dumps({"lanes": {"mining": {
        "provider": "openai", "base_url": CLOUD_BASE, "model": "mining-model",
        "api_key": CLOUD_KEY, "extra": {"enable_thinking": True}}}}))
    assert qcm._mining_lane() == (CLOUD_BASE, "mining-model", CLOUD_KEY, True)

    # local 显式改端点：model 空仍走发现，不带 key/thinking
    cp_deps.write_model_routing_raw(json.dumps({"lanes": {"mining": {
        "provider": "local", "base_url": "http://fixture-local:1234/v1"}}}))
    assert qcm._mining_lane() == ("http://fixture-local:1234/v1", "", "", False)

    # kill-switch：字节同旧
    monkeypatch.setenv("BOK_MODEL_ROUTING", "0")
    assert qcm._mining_lane() == ("http://127.0.0.1:1235/v1", "", "", False)


def test_qa_cluster_llm_chat_cloud_shape(monkeypatch):
    from control_plane import qa_cluster as qcm

    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured.update({"url": url, "payload": json, "headers": headers})
        return _FakeResp({"choices": [{"message": {"content": "decisions"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    out = qcm._llm_chat(
        CLOUD_BASE, "mining-model", "sys", "user",
        api_key=CLOUD_KEY, enable_thinking=True,
    )
    assert out == "decisions"
    assert captured["payload"]["enable_thinking"] is True
    assert captured["headers"] == {"Authorization": f"Bearer {CLOUD_KEY}"}
    # env 链形状：无头、无扩展字段（旧姿势逐字节）
    qcm._llm_chat("http://127.0.0.1:1235/v1", "m", "sys", "user")
    assert captured["headers"] is None
    assert "enable_thinking" not in captured["payload"]
