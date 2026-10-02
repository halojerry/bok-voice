"""编排审计第二波 FIX 1:a_reply 车道装配期活性探针 + 死车道喂饥荒信号。

实弹基线（2026-10-02 死车道 10 轮通话,PR 证据面）:把 a_reply 端点 kill 掉后,
10/10 轮「有答」全部是罐头（gen script=11 filler=1 llm=0）、首声 p50 1117ms
（罐头车道速度）、哨兵 LLM_FALLBACK_TEXT err=APIConnectionError('Connection
error.') ×16、饥荒激活 0 次、车道级告警 0 行——探针只看「有答」不辨来源,
**死车道上 PASS**。本档钉两条修:

①装配期探活（`_a_reply_endpoint_alive`/`_a_reply_probe_needed` + 装配点接线）:
   端点死=装配面一行 A_REPLY_ENDPOINT_DEAD + 审计 llm.a_reply_dead,不再每轮
   等满 22s 传输超时才落兜底、且零车道级痕迹。判据镜像 B 线
   interpret._mt_endpoint_alive(「有 HTTP 响应即活」,401/404/5xx 都算在场)。
   只探本机档（loopback）;只告警不换路——自动降级是饥荒 overlay 的职责。
②连接失败喂饥荒（`_is_connect_failure`/`_feed_famine_on_connect_failure`）:
   connection refused 是秒级返回,**不走首 token 超时分支**——饥荒 EMA
   (`record_llm_first_token`) 从未吃到样本,EMA 恒平 → overlay/CP 准入闸
   永不介入。修=预热连接失败按「首 token 超时×2,地板 8s」喂深饥荒样本,
   两发即 `llm_famine_active()`。
"""

from __future__ import annotations

import asyncio
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "apps" / "agent"))

import agent_runtime.agent as agent_mod  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    _famine_reset_for_tests,
    llm_famine_active,
)

_SRC = (_REPO / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_famine_state(monkeypatch):
    """镜像 tests/test_llm_famine.py:饥荒账本与四个常数 env 全清。"""
    monkeypatch.delenv("BOK_LLM_FAMINE", raising=False)
    monkeypatch.delenv("BOK_LLM_FAMINE_TTFT_S", raising=False)
    monkeypatch.delenv("LLM_FIRST_TOKEN_TIMEOUT_S", raising=False)
    _famine_reset_for_tests()
    yield
    _famine_reset_for_tests()


class _FakeResp:
    """urlopen 返回值替身:contextlib.closing 只要求 close()。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _patch_urlopen(monkeypatch, fn):
    monkeypatch.setattr(urllib.request, "urlopen", fn)


# ------------------------------------------------------------------ ① 探活判据


def test_alive_on_any_http_response(monkeypatch):
    """有 HTTP 响应（2xx/3xx）=活;探活 URL 必须是 {base}/models,带超时。"""
    calls: list[tuple[str, float]] = []

    def fake(url, timeout=None):
        calls.append((url, timeout))
        return _FakeResp()

    _patch_urlopen(monkeypatch, fake)
    assert agent_mod._a_reply_endpoint_alive("http://127.0.0.1:1237/v1") is True
    assert calls == [("http://127.0.0.1:1237/v1/models", 1.0)]


def test_alive_on_http_error(monkeypatch):
    """401/404/5xx 等 HTTPError 同判「端点在场」（镜像 interpret._mt_endpoint_alive）。"""

    def fake(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    _patch_urlopen(monkeypatch, fake)
    assert agent_mod._a_reply_endpoint_alive("http://127.0.0.1:1237/v1") is True


def test_dead_on_connection_error(monkeypatch):
    """connection refused（URLError）=死——死车道的实弹形状。"""

    def fake(url, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    _patch_urlopen(monkeypatch, fake)
    assert agent_mod._a_reply_endpoint_alive("http://127.0.0.1:1237/v1") is False


def test_dead_on_timeout(monkeypatch):
    def fake(url, timeout=None):
        raise TimeoutError("timed out")

    _patch_urlopen(monkeypatch, fake)
    assert agent_mod._a_reply_endpoint_alive("http://127.0.0.1:1237/v1") is False


def test_empty_base_dead_without_request(monkeypatch):
    called = []

    def fake(url, timeout=None):
        called.append(url)
        return _FakeResp()

    _patch_urlopen(monkeypatch, fake)
    assert agent_mod._a_reply_endpoint_alive("") is False
    assert called == []


def test_trailing_slash_normalized(monkeypatch):
    seen = []

    def fake(url, timeout=None):
        seen.append(url)
        return _FakeResp()

    _patch_urlopen(monkeypatch, fake)
    assert agent_mod._a_reply_endpoint_alive("http://127.0.0.1:1237/v1/") is True
    assert seen == ["http://127.0.0.1:1237/v1/models"]


def test_never_raises_on_weird_base(monkeypatch):
    """坏 base（非 URL 文本）=死,绝不外抛（装配点经 to_thread 调用）。"""

    def fake(url, timeout=None):
        raise ValueError("unknown url type")

    _patch_urlopen(monkeypatch, fake)
    assert agent_mod._a_reply_endpoint_alive("not-a-url") is False


# ------------------------------------------------------------------ ② 本机判据 / 总闸


@pytest.mark.parametrize(
    "base,expected",
    [
        ("http://127.0.0.1:1237/v1", True),
        ("http://localhost:1235/v1", True),
        ("http://[::1]:1237/v1", True),
        ("http://LOCALHOST:1235/v1", True),
        ("https://api.deepseek.com/v1", False),
        ("http://192.168.1.9:1235/v1", False),
        ("", False),
        ("not-a-url", False),
    ],
)
def test_is_local_base_url(base, expected):
    assert agent_mod._is_local_base_url(base) is expected


def test_probe_gate_default_on_for_loopback():
    assert agent_mod._a_reply_probe_needed("http://127.0.0.1:1237/v1", {}) is True


def test_probe_gate_kill_switch():
    env = {"BOK_A_REPLY_PROBE": "0"}
    assert agent_mod._a_reply_probe_needed("http://127.0.0.1:1237/v1", env) is False
    assert agent_mod._a_reply_probe_needed("http://127.0.0.1:1237/v1", {"BOK_A_REPLY_PROBE": "1"}) is True


def test_probe_gate_non_local_skipped():
    """云端档不打网络（探针只服务「本机 server 没起」这一族死亡）。"""
    assert agent_mod._a_reply_probe_needed("https://api.deepseek.com/v1", {}) is False


# ------------------------------------------------------------------ ③ 死车道喂饥荒


def _api_conn_error():
    import openai

    return openai.APIConnectionError(request=None)


def test_is_connect_failure_recognizes_sdk_and_httpx():
    import httpx

    assert agent_mod._is_connect_failure(_api_conn_error()) is True
    assert agent_mod._is_connect_failure(httpx.ConnectError("refused")) is True
    assert agent_mod._is_connect_failure(httpx.ConnectTimeout("t")) is True
    assert agent_mod._is_connect_failure(ValueError("Model is unloaded")) is False
    assert agent_mod._is_connect_failure(None) is False


def test_connect_failure_penalty_convention():
    """与首 token 超时分支同构（timeout×2）,地板 8s（死≠慢,要更重地声明）。"""
    assert agent_mod._connect_failure_penalty_s() == 8.0  # max(3.0*2, 8.0)
    assert agent_mod._connect_failure_penalty_s() >= 8.0


def test_connect_failure_penalty_scales_with_explicit_timeout(monkeypatch):
    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT_S", "6")
    assert agent_mod._connect_failure_penalty_s() == 12.0


def test_two_connect_failures_activate_famine():
    """两发连接失败 → 饥荒激活（实弹:16 发 APIConnectionError 零饥荒是缺陷）。"""
    exc = _api_conn_error()
    assert agent_mod._feed_famine_on_connect_failure(exc) is True
    assert llm_famine_active() is False, "单样本不判定（防孤例）"
    assert agent_mod._feed_famine_on_connect_failure(exc) is True
    assert llm_famine_active() is True, "车道不可达两发=机器级病态,必须进饥荒"


def test_non_connect_failure_does_not_feed():
    """400「Model is unloaded」类非连接失败不喂（JIT 装载窗不是饥荒）。"""
    assert agent_mod._feed_famine_on_connect_failure(ValueError("Model is unloaded")) is False
    assert llm_famine_active() is False
    assert agent_mod._feed_famine_on_connect_failure(ValueError("Model is unloaded")) is False
    assert llm_famine_active() is False


def test_kill_switch_env_disables_activation(monkeypatch):
    """BOK_LLM_FAMINE=0=喂样本也判不饥荒（饥荒总闸语义零变化）。"""
    monkeypatch.setenv("BOK_LLM_FAMINE", "0")
    exc = _api_conn_error()
    agent_mod._feed_famine_on_connect_failure(exc)
    agent_mod._feed_famine_on_connect_failure(exc)
    assert llm_famine_active() is False


# ------------------------------------------------------------------ ④ 源级 pin


def test_source_pin_probe_sits_between_route_resolve_and_provider():
    """探针必须落在 resolve_route 与首个 provider 构造之间（装配面单点）。"""
    anchor = '_a_reply_route = resolve_route("a_reply"'
    i = _SRC.index(anchor)
    probe = _SRC.index("_a_reply_probe_needed(_a_reply_route.base_url, os.environ)", i)
    mlx = _SRC.index("llm_provider = MlxLlmLLM(", i)
    assert i < probe < mlx, "探针必须在任何 provider 构造之前"


def test_source_pin_probe_threaded_never_blocks_loop():
    i = _SRC.index("_a_reply_probe_needed(_a_reply_route.base_url, os.environ)")
    seg = _SRC[i : i + 1400]
    assert "await asyncio.to_thread(" in seg
    assert "_a_reply_endpoint_alive, _a_reply_route.base_url" in seg
    assert "A_REPLY_ENDPOINT_DEAD base={_a_reply_route.base_url} lane=a_reply" in seg
    assert '"llm.a_reply_dead"' in seg


def test_source_pin_probe_kill_switch_literal():
    i = _SRC.index("def _a_reply_probe_needed")
    seg = _SRC[i : i + 1000]
    assert 'BOK_A_REPLY_PROBE", "1"' in seg, "kill-switch 缺省开=字面量 \"1\""


def test_source_pin_warmup_catch_feeds_connect_failure():
    """预热 catch（死车道唯一 agent.py 侧连接失败落点）必须喂饥荒样本。"""
    i = _SRC.index("async def _prefix_prewarm_task")
    j = _SRC.index("_prefix_prewarm_armed = True", i)
    seg = _SRC[i:j]
    assert "_feed_famine_on_connect_failure(exc, metrics)" in seg


def test_probe_smoke_real_function_does_not_raise():
    """真实 urllib 路径（无 monkeypatch）:对死端口 1.0s 内返回 False,绝不 raise。"""
    assert asyncio.run(
        asyncio.to_thread(agent_mod._a_reply_endpoint_alive, "http://127.0.0.1:1/v1", 0.3)
    ) is False


def test_forward_env_membership_pin():
    """kill-switch 必须进 bok._FORWARD_ENV（prod 封闭面转发靠它；
    agent.py 读注入 env Mapping 故静态扫描不强制——membership 由本测试钉死）。"""
    import tools.bok as bok  # noqa: PLC0415

    assert "BOK_A_REPLY_PROBE" in bok._FORWARD_ENV


# ---------------------------------------------------------------------------
# 验收补刀（2026-10-02）：连接罚样本必须双面落地——本地 EMA 之外还要经当通
# metrics reporter 入队 llm_ttft（ms），否则 CP 侧饥荒状态机（准入 409 + 新单
# overlay 切 4B）结构性收不到死车道信号（实弹：两通死车道后 CP 零转换）。
# ---------------------------------------------------------------------------


class _FakeMetrics:
    def __init__(self):
        self.calls: list[tuple[str, float]] = []

    def add(self, kind: str, ms: float) -> None:
        self.calls.append((kind, float(ms)))


def test_connect_failure_feeds_metrics_reporter():
    import httpx

    fake = _FakeMetrics()
    fed = agent_mod._feed_famine_on_connect_failure(httpx.ConnectError("refused"), fake)
    assert fed is True
    assert fake.calls == [("llm_ttft", agent_mod._connect_failure_penalty_s() * 1000.0)]
    assert agent_mod._connect_failure_penalty_s() >= 8.0


def test_connect_failure_metrics_add_never_raises():
    class _Boom:
        def add(self, *a, **k):
            raise RuntimeError("boom")

    import httpx

    assert agent_mod._feed_famine_on_connect_failure(httpx.ConnectTimeout("t"), _Boom()) is True


def test_non_connect_failure_does_not_touch_metrics():
    fake = _FakeMetrics()
    assert agent_mod._feed_famine_on_connect_failure(ValueError("unloaded"), fake) is False
    assert fake.calls == []


def test_source_pin_prewarm_spawns_pass_reporter():
    """两个 prewarm spawn 点必须把当通 _metrics_reporter 递进任务——漏一个就少一路
    罚样本上报（老 worker 只有本地 EMA,CP 网收不到）。"""
    assert _SRC.count("_prefix_prewarm_task(agent, greeting_text, metrics=_metrics_reporter)") == 1
    assert _SRC.count('_prefix_prewarm_task(agent, "", metrics=_metrics_reporter)') == 1
    assert "_feed_famine_on_connect_failure(exc, metrics)" in _SRC
    assert "metrics=None" in _SRC.split("async def _prefix_prewarm_task")[1][:200]
