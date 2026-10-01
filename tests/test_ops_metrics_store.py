"""DR 容灾+可观测波 B 路（feat/dr-observability）· CP 指标管道/饥荒状态机/overlay/准入闸。

契约=docs/DR-WAVE-CONTRACT.md §1/§2/§3（冻结）。覆盖面：
- 滚动窗（300s 每 kind deque maxlen 500）/ 分位纯函数 / call_id 过滤；
- EMA α=0.4 状态机全转换 healthy→famine→downgraded→healthy + hold/release 迟滞；
- 手动覆盖优先（force_downgrade 钉住 / force_healthy 证据清零交还自动 /
  pause_dialing 独立闸）；
- overlay 优先级（读 packages/core model_routes.resolve_route 核实：
  kill-switch > overlay > 用户配置 > env 链）；
- 端点：agent-report（形状不合 422 绝不 500）/ metrics-providers / disaster-status /
  disaster-override / settings overlay 只作用 agent 通道且不落库；
- 准入闸 409 矩阵（live 拦、simulation/interpret 放、停拨同拦、解除放行）+ 审计落库。

全部 key/URL 均为 fixture 假值；不碰端口、不起服务（TestClient + 内存仓）。
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "")  # 强制内存仓（SQL 路径另行集成测）
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from control_plane import main as cp_main  # noqa: E402
from control_plane import ops_metrics  # noqa: E402
from control_plane.auth import hash_password  # noqa: E402

PW = "Passw0rd!x"
MACHINE_TOKEN = "machine-token"
# fixture 假值（中性串，安全扫描友好，勿当真实凭据）
CLOUD_KEY = "fixture-key"
CLOUD_BASE = "https://fixture.example/v1"
FOUR_B = "/models/Qwen3.5-4B-test"


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    """每测隔离：饥荒状态机/滚窗复位；审计钩子回挂 main._audit 动态代理。"""
    for key in (
        "BOK_LLM_FAMINE_TTFT_S",
        "BOK_LLM_FAMINE_HOLD_S",
        "BOK_LLM_FAMINE_RELEASE_S",
        "BOK_SWAP_THRESHOLD_GB",
        "BOK_CP_TOKEN",
        "BOK_AUTH_REQUIRED",
        "BOK_REQUIRE_TEMPLATE",
        "BOK_MAX_ACTIVE_CALLS",
        "MLX_LLM_MODEL",
        "MLX_LLM_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    from control_plane import deps as cp_deps

    cp_deps._routing_state["memory"] = ""
    cp_deps._routing_state["session_factory"] = None
    ops_metrics.reset_state()
    yield
    ops_metrics.reset_state()
    # 钩子回挂 canonical 绑定（lambda 体动态查 cp_main._audit，monkeypatch 可拦截）。
    ops_metrics.store().set_audit_hook(
        lambda action, detail: cp_main._audit(action, detail=detail)
    )


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    return TestClient(cp_main.app), repo


def _audit_sink(monkeypatch) -> list[tuple[str, dict]]:
    """拦 CP 审计（既不写真 JSONL，也不依赖全局 audit store 生命周期）。"""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: events.append((action, kw)))
    return events


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


# ---- §2 分位纯函数 ----


def test_percentile_ms_pure():
    assert ops_metrics.percentile_ms([], 0.5) == 0
    assert ops_metrics.percentile_ms([412.0], 0.95) == 412
    # 线性插值（numpy 缺省口径）：两端点中位 = 中点
    assert ops_metrics.percentile_ms([400.0, 500.0], 0.5) == 450
    assert ops_metrics.percentile_ms([18.0, 20.0, 22.0], 0.95) == 22
    assert ops_metrics.percentile_ms([10.0, 20.0, 30.0, 40.0], 0.5) == 25


# ---- 滚动窗 / maxlen / call_id 过滤 / 脏样本 ----


def test_rolling_window_maxlen_and_call_filter():
    s = ops_metrics.MetricsStore(window_s=300.0, maxlen=500)
    for _ in range(600):
        s.record("vad_infer", 10.0, call_id="call-a", now=0.0)
    prov = s.providers(now=1.0)
    assert prov["vad"]["n"] == 500  # deque(maxlen=500) 帽
    assert prov["asr"] == {"last_ms": None, "p50": None, "p95": None, "n": 0}
    # 窗口外（>300s）样本不计
    s.record("vad_infer", 999.0, call_id="call-a", now=1000.0)
    prov = s.providers(now=1000.0)
    assert prov["vad"]["n"] == 1 and prov["vad"]["last_ms"] == 999
    # call_id 过滤（每 kind 同一 deque 内过滤）
    s.record("tts_first_audio", 300.0, call_id="call-b", now=1000.0)
    assert s.providers(call_id="call-b", now=1000.0)["tts"]["n"] == 1
    assert s.providers(call_id="call-c", now=1000.0)["tts"]["n"] == 0
    # 脏样本静默滤除（负数/NaN/未知 kind）
    assert s.record("vad_infer", -1.0, now=1000.0) is False
    assert s.record("vad_infer", float("nan"), now=1000.0) is False
    assert s.record("bogus_kind", 1.0, now=1000.0) is False
    assert s.record("vad_infer", 12.0, now=1000.0) is True


def test_providers_view_shape_matches_contract():
    s = ops_metrics.MetricsStore()
    s.record("asr_transcribe", 412.0, now=0.0)
    s.record("asr_transcribe", 388.0, now=0.0)
    s.record("llm_ttft", 680.0, now=0.0)
    view = s.providers_view(now=1.0)
    assert view["window_s"] == 300
    assert list(view["providers"]) == ["asr", "llm", "tts", "vad"]  # 恒四键（契约行序）
    assert view["providers"]["asr"] == {"last_ms": 388, "p50": 400, "p95": 411, "n": 2}
    assert view["providers"]["tts"]["n"] == 0
    # §2 famine=五键（无 manual_override；该键只在 §4 六键面）
    assert set(view["famine"]) == {
        "level", "ema_s", "since", "downgraded", "dialing_paused",
    }


# ---- §3 状态机：全转换 / 迟滞 / 审计 ----


def test_state_machine_full_cycle_and_audit():
    seen: list[tuple[str, dict]] = []
    s = ops_metrics.store()
    s.set_audit_hook(lambda action, detail: seen.append((action, detail)))
    assert s.famine_view()["level"] == "healthy"
    # ≥2 样本才有效（与 worker 端第十五波同款）
    s.record("llm_ttft", 5000.0, now=0.0)
    assert s.famine_view(now=0.5)["level"] == "healthy"
    s.record("llm_ttft", 5000.0, now=1.0)
    v = s.famine_view(now=1.0)
    assert v["level"] == "famine" and v["downgraded"] is False and v["ema_s"] == 5.0
    # hold 10s（high 钟自 t=1 起）
    assert s.famine_view(now=10.5)["level"] == "famine"
    assert s.famine_view(now=11.5)["level"] == "downgraded"
    assert s.blocking_state(now=11.5)["reason"] == "downgraded"
    # EMA 回落 < 阈值 → release 需 30s（自首个低样本起）
    s.record("llm_ttft", 100.0, now=12.0)  # ema=3040ms < 4000
    assert s.famine_view(now=41.5)["level"] == "downgraded"  # 29.5s
    v = s.famine_view(now=42.5)
    assert v["level"] == "healthy" and v["since"] is None
    # 审计：from/to/ema_ms（ms 口径）
    assert [t["to"] for _, t in seen] == ["famine", "downgraded", "healthy"]
    assert seen[0][1] == {"from": "healthy", "to": "famine", "ema_ms": 5000.0}
    assert seen[-1][1]["from"] == "downgraded"
    assert all(action == "ops.famine" for action, _ in seen)


def test_hysteresis_release_clock_resets_on_high_blip():
    s = ops_metrics.MetricsStore()
    s.record("llm_ttft", 6000.0, now=0.0)
    s.record("llm_ttft", 6000.0, now=1.0)
    assert s.famine_view(now=1.0)["level"] == "famine"
    s.record("llm_ttft", 100.0, now=2.0)  # ema=3560 < 阈值；low 钟=2
    assert s.famine_view(now=31.5)["level"] == "famine"
    # 高尖峰把 EMA 拉回阈值上 → low 钟重置（release 不累计）
    s.record("llm_ttft", 9000.0, now=32.0)  # ema=5736 ≥ 阈值
    s.record("llm_ttft", 100.0, now=33.0)  # ema=3481.6 < 阈值；low 钟=33
    assert s.famine_view(now=62.5)["level"] == "famine"  # 29.5s < 30
    assert s.famine_view(now=63.5)["level"] == "healthy"  # 30.5s ≥ 30


def test_hysteresis_hold_clock_resets_on_low_blip():
    s = ops_metrics.MetricsStore()
    s.record("llm_ttft", 6000.0, now=0.0)
    s.record("llm_ttft", 6000.0, now=1.0)
    assert s.famine_view(now=1.0)["level"] == "famine"  # high 钟=1
    s.record("llm_ttft", 100.0, now=5.0)  # ema=3560 < 阈值 → high 钟清
    s.record("llm_ttft", 9000.0, now=6.0)  # ema=5736 → high 钟重启=6
    assert s.famine_view(now=14.5)["level"] == "famine"  # 8.5s < 10
    assert s.famine_view(now=16.5)["level"] == "downgraded"  # 10.5s ≥ 10


def test_manual_override_beats_auto_and_hands_back():
    s = ops_metrics.MetricsStore()
    s.record("llm_ttft", 5000.0, now=0.0)
    s.record("llm_ttft", 5000.0, now=1.0)
    s.evaluate(now=11.5)
    assert s.famine_view()["level"] == "downgraded"
    # force_healthy：解除覆盖 + 自动证据清零（陈旧 EMA 不立即反扑）
    s.override("force_healthy")
    v = s.famine_view()
    assert v["level"] == "healthy" and v["manual_override"] is None and v["ema_s"] == 0.0
    # force_downgrade 钉住：低 EMA 持续 180s 也不自动 release
    s.override("force_downgrade")
    assert s.famine_view()["manual_override"] == "force_downgrade"
    s.record("llm_ttft", 100.0, now=20.0)
    s.evaluate(now=200.0)
    assert s.famine_view(now=200.0)["downgraded"] is True
    assert s.blocking_state(now=200.0)["blocked"] is True
    # 解除后自动接管：1 样本不够，2 样本+hold 才再降
    s.override("force_healthy")
    s.record("llm_ttft", 5000.0, now=210.0)
    assert s.famine_view(now=300.0)["level"] == "healthy"
    s.record("llm_ttft", 5000.0, now=211.0)
    assert s.famine_view(now=211.0)["level"] == "famine"
    s.evaluate(now=221.5)
    assert s.famine_view(now=221.5)["level"] == "downgraded"


def test_dialing_pause_is_independent_gate():
    s = ops_metrics.MetricsStore()
    s.override("pause_dialing")
    v = s.famine_view()
    assert v["dialing_paused"] is True and v["level"] == "healthy"
    assert v["manual_override"] is None
    blocked = s.blocking_state()
    assert blocked["blocked"] is True and blocked["reason"] == "dialing_paused"
    s.override("resume_dialing")
    assert s.blocking_state() == {
        "blocked": False,
        "reason": "",
        "famine": s.famine_view(),
    }


def test_env_tuning_and_bad_values(monkeypatch):
    monkeypatch.setenv("BOK_LLM_FAMINE_TTFT_S", "2")
    monkeypatch.setenv("BOK_LLM_FAMINE_HOLD_S", "1")
    monkeypatch.setenv("BOK_LLM_FAMINE_RELEASE_S", "2")
    assert ops_metrics.famine_threshold_ms() == 2000.0
    assert ops_metrics.famine_hold_s() == 1.0
    assert ops_metrics.famine_release_s() == 2.0
    s = ops_metrics.MetricsStore()
    s.record("llm_ttft", 2500.0, now=0.0)
    s.record("llm_ttft", 2500.0, now=0.5)
    assert s.famine_view(now=0.5)["level"] == "famine"
    assert s.famine_view(now=2.0)["level"] == "downgraded"  # hold 1s
    s.record("llm_ttft", 100.0, now=3.0)  # ema=1540 < 2000
    assert s.famine_view(now=5.5)["level"] == "healthy"  # release 2s
    # 误配防呆：0/负值回落默认（0 若按字面消费=恒饥荒）
    monkeypatch.setenv("BOK_LLM_FAMINE_TTFT_S", "0")
    monkeypatch.setenv("BOK_LLM_FAMINE_HOLD_S", "-3")
    assert ops_metrics.famine_threshold_ms() == 4000.0
    assert ops_metrics.famine_hold_s() == 10.0


# ---- §3 overlay：优先级与用户原文隔离 ----


def test_overlay_priority_via_shared_resolve_route():
    from bok_voice_core.model_routes import resolve_route

    user_raw = json.dumps({
        "lanes": {
            "a_reply": {"provider": "openai", "base_url": CLOUD_BASE,
                        "model": "cloud-9b", "api_key": CLOUD_KEY},
            "judge": {"provider": "openai", "base_url": CLOUD_BASE,
                      "model": "judge-model", "api_key": CLOUD_KEY},
        },
        "presets": {},
    })
    overlaid = ops_metrics.overlay_a_reply(
        user_raw, ops_metrics.famine_overlay_lane(model=FOUR_B, base_url="")
    )
    env = {"MLX_LLM_BASE_URL": "http://127.0.0.1:1235/v1"}
    # overlay > 用户配置（a_reply 被替换成 4B 本地档；其余车道不动）
    route = resolve_route("a_reply", env, overlaid)
    assert route.provider == "local"
    assert route.base_url == "http://127.0.0.1:1235/v1"
    assert route.model == FOUR_B and route.source == "routing"
    assert resolve_route("judge", env, overlaid).model == "judge-model"
    # kill-switch > overlay：BOK_MODEL_ROUTING=0 时整表被忽略（overlay 一并失效）
    route = resolve_route("a_reply", {**env, "BOK_MODEL_ROUTING": "0"}, overlaid)
    assert route.source == "env" and route.model == ""
    assert route.base_url == "http://127.0.0.1:1235/v1"
    # 用户原文零改写（overlay 是响应体行为，不是落库）
    assert json.loads(user_raw)["lanes"]["a_reply"]["model"] == "cloud-9b"
    assert json.loads(user_raw)["lanes"]["a_reply"]["api_key"] == CLOUD_KEY
    # 空表 overlay：只补 a_reply，其余车道回落 env 链
    doc = json.loads(ops_metrics.overlay_a_reply(
        "", ops_metrics.famine_overlay_lane(model=FOUR_B)
    ))
    assert list(doc["lanes"]) == ["a_reply"]
    assert resolve_route("settle", env, json.dumps(doc)).source == "env"
    # 坏 JSON 宽容（永不抛）
    assert json.loads(ops_metrics.overlay_a_reply("{not json", ops_metrics.famine_overlay_lane()))["lanes"]["a_reply"]["provider"] == "local"


# ---- §1/§2 端点 ----


def test_agent_report_and_providers_endpoint(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    body = {
        "call_id": "call-aabbccdd", "account_id": "acc-001", "worker": "a-line",
        "samples": [
            {"kind": "llm_ttft", "ms": 680.0, "ts": "2026-10-01T15:00:00Z"},
            {"kind": "asr_transcribe", "ms": 412, "ts": "2026-10-01T15:00:00Z"},
            {"kind": "tts_first_audio", "ms": 310, "ts": "2026-10-01T15:00:00Z"},
            {"kind": "vad_infer", "ms": 18, "ts": "2026-10-01T15:00:00Z"},
        ],
    }
    r = client.post("/api/metrics/agent-report", json=body)
    assert r.status_code == 200 and r.json() == {"ok": True, "accepted": 4}
    rep = client.get("/api/metrics/providers").json()
    assert rep["window_s"] == 300
    assert rep["providers"]["asr"] == {"last_ms": 412, "p50": 412, "p95": 412, "n": 1}
    assert rep["providers"]["llm"]["last_ms"] == 680
    assert rep["providers"]["tts"]["n"] == 1 and rep["providers"]["vad"]["n"] == 1
    assert rep["famine"]["level"] == "healthy"
    # call_id 过滤：别的通话=无样本（null + n 0）
    other = client.get("/api/metrics/providers", params={"call_id": "call-other"}).json()
    assert other["providers"]["llm"] == {"last_ms": None, "p50": None, "p95": None, "n": 0}
    mine = client.get("/api/metrics/providers", params={"call_id": "call-aabbccdd"}).json()
    assert mine["providers"]["llm"]["n"] == 1


def test_agent_report_shape_422_never_500(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    for bad in (
        {"samples": [{"kind": "bogus_kind", "ms": 1}]},  # 未知 kind
        {"samples": [{"kind": "llm_ttft"}]},  # 缺 ms
        {"samples": "nope"},  # samples 非数组
        {"samples": [{"kind": "llm_ttft", "ms": "abc"}]},  # ms 非数字
    ):
        assert client.post("/api/metrics/agent-report", json=bad).status_code == 422, bad
    # 超样本帽（5000）→ 422 而非 500
    big = {"samples": [{"kind": "vad_infer", "ms": 1}] * 5001}
    assert client.post("/api/metrics/agent-report", json=big).status_code == 422
    # 脏 ms（负值/NaN）静默滤除，端点仍 200（fire-and-forget 语义）
    r = client.post("/api/metrics/agent-report", json={"samples": [
        {"kind": "llm_ttft", "ms": -5}, {"kind": "vad_infer", "ms": 1},
    ]})
    assert r.status_code == 200 and r.json()["accepted"] == 1


def test_metrics_endpoints_permission_matrix(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _mk_user(repo, "rooty", "root", account="")
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", MACHINE_TOKEN)
    # auth-on 无凭据 → 401（identity 门）；机器通道直通；root 直通
    assert client.post("/api/metrics/agent-report", json={"samples": []}).status_code == 401
    assert client.get("/api/metrics/providers").status_code == 401
    assert client.post("/api/metrics/agent-report", headers=_machine(),
                       json={"samples": []}).status_code == 200
    assert client.get("/api/metrics/providers", headers=_machine()).status_code == 200
    assert client.get("/api/metrics/providers", headers=_login(client, "rooty")).status_code == 200


def test_disaster_status_shape_and_override_audit(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    events = _audit_sink(monkeypatch)
    monkeypatch.setattr(
        ops_metrics, "probe_servers",
        lambda *a, **k: [{"name": "llm-9b", "port": 1237, "up": True}],
    )
    monkeypatch.setattr(ops_metrics, "swap_used_gb", lambda: 24.7)
    _mk_user(repo, "rooty", "root", account="")
    _mk_user(repo, "peon", "user")
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_CP_TOKEN", MACHINE_TOKEN)
    assert client.get("/api/ops/disaster-status").status_code == 401
    assert client.get("/api/ops/disaster-status", headers=_login(client, "peon")).status_code == 403
    rt = _login(client, "rooty")
    body = client.get("/api/ops/disaster-status", headers=rt).json()
    assert set(body) == {
        "famine", "providers", "memory", "servers", "active_calls", "recent_events",
    }
    assert set(body["famine"]) == {
        "level", "ema_s", "since", "downgraded", "dialing_paused", "manual_override",
    }
    assert body["memory"] == {"swap_used_gb": 24.7, "threshold_gb": 8.0}
    assert body["servers"] == [{"name": "llm-9b", "port": 1237, "up": True}]
    assert body["active_calls"] == 0 and body["recent_events"] == []
    # 机器通道直通（root 专属面先例）
    assert client.get("/api/ops/disaster-status", headers=_machine()).status_code == 200
    # 四动作 + 未知 422 + 非 root 403
    r = client.post("/api/ops/disaster-override", headers=rt, json={"action": "force_downgrade"})
    assert r.status_code == 200
    assert r.json()["downgraded"] is True and r.json()["manual_override"] == "force_downgrade"
    assert client.post("/api/ops/disaster-override", headers=_login(client, "peon"),
                       json={"action": "force_healthy"}).status_code == 403
    assert client.post("/api/ops/disaster-override", headers=rt,
                       json={"action": "bogus"}).status_code == 422
    r = client.post("/api/ops/disaster-override", headers=rt, json={"action": "pause_dialing"})
    assert r.status_code == 200 and r.json()["dialing_paused"] is True
    r = client.post("/api/ops/disaster-override", headers=rt, json={"action": "resume_dialing"})
    assert r.json()["dialing_paused"] is False
    # 审计：每个成功动作 ops.famine_override（403/422 不入账）；level 转换另发 ops.famine
    actions = [a for a, _ in events]
    assert actions.count("ops.famine_override") == 3
    assert "ops.famine" in actions
    over = next(kw for a, kw in events if a == "ops.famine_override")
    assert over["detail"]["action"] == "force_downgrade"
    assert over["detail"]["level"] == "downgraded"
    assert CLOUD_KEY not in json.dumps(over["detail"])  # 零密钥材料（红线）


def test_override_and_famine_audit_land_in_repo(monkeypatch, tmp_path):
    """审计落库（非 monkeypatch）：钩子经真 _audit → AuditStore tap → 仓储。"""
    from bok_voice_obs.audit import AuditStore

    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setattr(
        cp_main, "audit_store",
        lambda: AuditStore(directory=tmp_path, tap=lambda event: repo.append_audit(event.to_dict())),
    )
    assert client.post("/api/ops/disaster-override",
                       json={"action": "force_downgrade"}).status_code == 200
    rows = repo.list_audit_events(limit=50)
    actions = [r["action"] for r in rows]
    assert "ops.famine_override" in actions and "ops.famine" in actions
    famine = next(r for r in rows if r["action"] == "ops.famine")
    assert famine["detail"]["from"] == "healthy"
    assert famine["detail"]["to"] == "downgraded"
    assert famine["detail"]["ema_ms"] == 0.0  # 无样本手动钉档：EMA=0（如实）
    # 面板 recent_events 读同一账本（时间正序）
    body = client.get("/api/ops/disaster-status").json()
    events = [e["event"] for e in body["recent_events"]]
    assert events == ["ops.famine", "ops.famine_override"]


def test_settings_overlay_agent_channel_only_and_never_persisted(monkeypatch):
    client, _repo = _client_and_repo(monkeypatch)
    _audit_sink(monkeypatch)
    from control_plane import deps as cp_deps

    cp_deps._routing_state["memory"] = json.dumps({
        "lanes": {
            "a_reply": {"provider": "openai", "base_url": CLOUD_BASE,
                        "model": "cloud-9b", "api_key": CLOUD_KEY},
        },
        "presets": {},
    })
    monkeypatch.setenv("MLX_LLM_MODEL", FOUR_B)
    # 非降档：agent 通道原样
    s = client.get("/api/settings", params={"internal": 1},
                   headers={"X-Bok-Channel": "agent"}).json()
    assert json.loads(s["model_routing_json"])["lanes"]["a_reply"]["provider"] == "openai"
    # 降档 → agent 通道吃 overlay
    client.post("/api/ops/disaster-override", json={"action": "force_downgrade"})
    s = client.get("/api/settings", params={"internal": 1},
                   headers={"X-Bok-Channel": "agent"}).json()
    lane = json.loads(s["model_routing_json"])["lanes"]["a_reply"]
    assert lane == {
        "provider": "local", "base_url": "http://127.0.0.1:1235/v1",
        "model": FOUR_B, "api_key": "", "extra": {"enable_thinking": False},
    }
    # 人类通道（root/本机匿名无 X-Bok-Channel 头）不吃 overlay
    human = client.get("/api/settings", params={"internal": 1}).json()
    assert json.loads(human["model_routing_json"])["lanes"]["a_reply"]["provider"] == "openai"
    # 掩码面完全不含该键；落库原文零改动
    assert "model_routing_json" not in client.get("/api/settings").json()
    stored = json.loads(cp_main.read_model_routing_raw())
    assert stored["lanes"]["a_reply"]["model"] == "cloud-9b"
    assert stored["lanes"]["a_reply"]["api_key"] == CLOUD_KEY


# ---- §3 准入闸 409 矩阵 ----


def test_admission_gate_matrix(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    events = _audit_sink(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")  # 隔离模板闸，只测饥荒闸
    monkeypatch.setenv("BOK_MAX_ACTIVE_CALLS", "0")  # 不限并发，避免闸间干扰
    base = {"account_id": "acc-001", "mode": "live"}
    # healthy：live 放行
    assert client.post("/api/calls", json={**base, "object_id": "obj-ok"}).status_code == 200
    # downgraded：live 409 + 审计 call.reject_famine + 契约文案
    ops_metrics.store().override("force_downgrade")
    r = client.post("/api/calls", json={**base, "object_id": "obj-famine"})
    assert r.status_code == 409
    assert r.json()["detail"] == "节点饥荒降档中，暂停新建单"
    assert "call.reject_famine" in [a for a, _ in events]
    # simulation / realtime_demo 档不拦
    assert client.post("/api/calls", json={"account_id": "acc-001",
                                           "mode": "simulation"}).status_code == 200
    assert client.post("/api/calls", json={"account_id": "acc-001",
                                           "mode": "realtime_demo"}).status_code == 200
    # interpret（B 线同传）live 不拦
    assert client.post("/api/calls", json={**base, "kind": "interpret",
                                           "object_id": "obj-interp"}).status_code == 200
    # 手动停拨同拦；解除后放行
    ops_metrics.store().override("force_healthy")
    ops_metrics.store().override("pause_dialing")
    r = client.post("/api/calls", json={**base, "object_id": "obj-paused"})
    assert r.status_code == 409
    ops_metrics.store().override("resume_dialing")
    assert client.post("/api/calls", json={**base, "object_id": "obj-live2"}).status_code == 200


def test_famine_does_not_touch_existing_calls(monkeypatch):
    """在途通话不受影响（契约 §3）：降档只拦新建，不改任何既有行状态。"""
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")
    created = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
    assert created.status_code == 200
    call_id = created.json()["id"]
    ops_metrics.store().override("force_downgrade")
    row = repo.get_call(call_id)
    assert row and row["status"] == "ringing"
