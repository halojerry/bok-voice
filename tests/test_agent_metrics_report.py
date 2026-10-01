"""A 路 worker 指标上报单测（DR 容灾+可观测波契约 §1，feat/dr-observability）。

契约（docs/DR-WAVE-CONTRACT.md §1）::

    POST /api/metrics/agent-report
    {"call_id", "account_id", "worker", "samples":[{"kind","ms","ts"}...]}

覆盖面：
- 批量形状逐字段（契约定死四 kind / ts UTC ISO8601 / worker=a-line）；
- 节流：≤2s tick 攒批（入队即刻零请求）、批满提前发；
- 全吞错：5xx/超时静默丢批（不重试不堆积）、扫尾 close flush、队列溢出丢旧；
- vad 增量差分（首样本基线 / Δcnt≤0 回退当批口径 / 除零跳过）；
- 源级 pin：agent.py `_on_metrics` 四 kind 采样与 `_close` 收尾（pathlib 读源，
  照仓里既有 wiring 测试姿势；本 worktree 基线 agent.py 因缺 room_claim.py 等
  未跟踪文件不可导入，只做文本锚）。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.control_plane import ControlPlaneClient  # noqa: E402
from agent_runtime.metrics_report import (  # noqa: E402
    KINDS,
    MetricsReporter,
    VadInferTracker,
)

_AGENT_SRC = (
    ROOT / "apps" / "agent" / "agent_runtime" / "agent.py"
).read_text(encoding="utf-8")

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_CP = "http://cp.test:8000"


def _capture(status: int = 200):
    """MockTransport 记录全部请求（2xx 应答）——返回 (seen, transport)。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"ok": True})

    return seen, httpx.MockTransport(handler)


def _reporter(seen, transport, **kwargs) -> MetricsReporter:
    return MetricsReporter(
        _CP,
        {"X-Call-ID": "call-1", "X-Bok-Channel": "agent"},
        call_id="call-1",
        account_id="acc-001",
        interval_s=kwargs.pop("interval_s", 0.25),
        transport=transport,
        **kwargs,
    )


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


# ---- 批量形状（契约 §1 逐字段）----


def test_batch_shape_matches_contract():
    seen, transport = _capture()

    async def scenario():
        r = _reporter(seen, transport)
        r.add("llm_ttft", 680.4)
        r.add("asr_transcribe", 412)
        r.add("tts_first_audio", 310)
        r.add("vad_infer", 18)
        await asyncio.sleep(0.35)  # 过首个 tick
        await r.close()

    asyncio.run(scenario())

    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/api/metrics/agent-report"
    assert req.headers["X-Call-ID"] == "call-1"  # 同源请求头透传
    body = _body(req)
    assert set(body) == {"call_id", "account_id", "worker", "samples"}
    assert body["call_id"] == "call-1"
    assert body["account_id"] == "acc-001"
    assert body["worker"] == "a-line"
    assert [s["kind"] for s in body["samples"]] == list(KINDS)
    for sample in body["samples"]:
        assert set(sample) == {"kind", "ms", "ts"}
        assert isinstance(sample["ms"], float) and sample["ms"] >= 0
        assert _TS_RE.match(sample["ts"]), sample["ts"]
        assert sample["kind"] in KINDS
    assert body["samples"][0]["ms"] == 680.4


# ---- 节流 / 批量 ----


def test_throttle_batches_within_window():
    seen, transport = _capture()

    async def scenario():
        r = _reporter(seen, transport, interval_s=0.25)
        r.add("llm_ttft", 100.0)
        r.add("vad_infer", 20.0)
        assert seen == []  # 入队即刻零请求（攒批而非逐条发）
        await asyncio.sleep(0.35)
        assert len(seen) == 1
        assert [s["kind"] for s in _body(seen[0])["samples"]] == ["llm_ttft", "vad_infer"]
        r.add("asr_transcribe", 300.0)
        await asyncio.sleep(0.35)
        assert len(seen) == 2  # 新窗口=新批
        assert [s["kind"] for s in _body(seen[1])["samples"]] == ["asr_transcribe"]
        await r.close()

    asyncio.run(scenario())


def test_full_batch_preempts_tick():
    seen, transport = _capture()

    async def scenario():
        # tick 窗 5s 但批满 3：入队瞬时即发，不等窗口
        r = _reporter(seen, transport, interval_s=5.0, max_batch=3)
        r.add("vad_infer", 10.0)
        r.add("vad_infer", 11.0)
        r.add("vad_infer", 12.0)
        await asyncio.sleep(0.2)
        assert len(seen) == 1
        assert len(_body(seen[0])["samples"]) == 3
        await r.close()

    asyncio.run(scenario())


def test_close_flushes_pending_and_is_idempotent():
    seen, transport = _capture()

    async def scenario():
        r = _reporter(seen, transport, interval_s=30.0)  # tick 未到，靠 close 扫尾
        r.add("llm_ttft", 500.0)
        r.add("vad_infer", 15.0)
        await r.close()
        assert len(seen) == 1 and len(_body(seen[0])["samples"]) == 2
        await r.close()  # 幂等：二次 close 零请求零异常
        assert len(seen) == 1
        r.add("llm_ttft", 1.0)  # close 后入队=静默丢弃
        assert r.pending == 0

    asyncio.run(scenario())


def test_success_logs_sent_line(capsys):
    seen, transport = _capture()

    async def scenario():
        r = _reporter(seen, transport)
        r.add("vad_infer", 18.0)
        r.add("vad_infer", 19.0)
        await asyncio.sleep(0.35)
        await r.close()

    asyncio.run(scenario())
    assert "METRICS_REPORT sent=2" in capsys.readouterr().out


# ---- 全吞错 / 不堆积 ----


def test_failures_swallowed_silently(capsys):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(500, json={"detail": "boom"})
        raise httpx.ConnectTimeout("cp unreachable")

    async def scenario():
        r = _reporter(seen, httpx.MockTransport(handler), interval_s=0.05)
        r.add("llm_ttft", 500.0)  # 5xx → 静默丢
        await asyncio.sleep(0.12)
        assert r.pending == 0  # 不重试不堆积
        r.add("vad_infer", 12.0)  # 超时 → 静默丢
        await asyncio.sleep(0.12)
        assert r.pending == 0
        assert len(seen) == 2
        await r.close()

    asyncio.run(scenario())  # 不上抛=吞错成立
    assert "METRICS_REPORT" not in capsys.readouterr().out  # 失败不打，防日志风暴


def test_queue_overflow_drops_oldest():
    seen, transport = _capture()

    async def scenario():
        r = _reporter(seen, transport, interval_s=30.0, queue_max=2)
        for i in range(5):
            r.add("vad_infer", float(i))
        assert r.pending == 2  # 队列有界：不无限涨
        assert r.dropped == 3  # 超限丢最旧
        await r.close()
        assert [s["ms"] for s in _body(seen[0])["samples"]] == [3.0, 4.0]  # 保新

    asyncio.run(scenario())


def test_dirty_samples_dropped_before_queue():
    seen, transport = _capture()

    async def scenario():
        r = _reporter(seen, transport, interval_s=0.05)
        r.add("bogus_kind", 1.0)  # 非契约枚举
        r.add("llm_ttft", -1.0)  # livekit 取消/错误哨兵
        r.add("llm_ttft", float("nan"))
        r.add("asr_transcribe", float("inf"))
        await asyncio.sleep(0.12)
        assert seen == []  # 全脏=零请求
        await r.close()

    asyncio.run(scenario())


# ---- vad 增量差分 ----


def test_vad_first_sample_is_baseline_only():
    tracker = VadInferTracker()
    assert tracker.feed(0.5, 32) is None  # 首样本：只建基线（契约跳过）


def test_vad_cumulative_delta_math():
    tracker = VadInferTracker()
    assert tracker.feed(1.0, 100) is None
    assert tracker.feed(1.6, 160) == pytest.approx(10.0)  # Δ 0.6s / 60 次
    # 真增量口径 ≠ 当批均值（2.1/200=10.5）——钉死差分公式
    assert tracker.feed(2.1, 200) == pytest.approx(12.5)


def test_vad_reset_producer_falls_back_to_batch_mean():
    tracker = VadInferTracker()
    assert tracker.feed(0.50, 32) is None
    # Δcnt=0（livekit 逐批 emit 形态）→ 当批均值，且模式锁死当批口径
    assert tracker.feed(0.55, 32) == pytest.approx(17.2)
    # 计数巧合 +1 不再被差分公式带偏（仍是当批均值 0.6/33）
    assert tracker.feed(0.60, 33) == pytest.approx(18.2)


def test_vad_zero_and_garbage_skipped():
    tracker = VadInferTracker()
    assert tracker.feed(0.0, 0) is None  # 空批/除零：不进基线
    assert tracker.feed(-1.0, 5) is None  # 负值
    assert tracker.feed("x", 1) is None  # 坏值
    assert tracker.feed(float("nan"), 1) is None
    assert tracker.feed(1.0, 10) is None  # 前面全被滤除 → 这只是首样本（基线）
    assert tracker.feed(1.5, 20) == pytest.approx(50.0)  # Δ 0.5s / 10 次


# ---- CP 请求头同源缝 ----


def test_cp_request_headers_snapshot(monkeypatch):
    # httpx 迭代头名统一小写（HTTP 头大小写不敏感）——断言前归一小写。
    def lower(headers: dict) -> dict:
        return {str(k).lower(): v for k, v in headers.items()}

    async def scenario_with_token():
        cp = ControlPlaneClient(_CP, call_id="call-9")
        try:
            headers = lower(cp.request_headers)
            assert headers["x-call-id"] == "call-9"
            assert headers["x-bok-channel"] == "agent"
            assert "authorization" not in headers
        finally:
            await cp.aclose()

    asyncio.run(scenario_with_token())

    monkeypatch.setenv("BOK_CP_TOKEN", "tok-123")

    async def scenario_auth_on():
        seen, transport = _capture()
        cp = ControlPlaneClient(_CP, call_id="call-9")
        try:
            r = MetricsReporter(cp.base_url, cp.request_headers, call_id="call-9", transport=transport)
            r.add("vad_infer", 9.0)
            await r.close()
        finally:
            await cp.aclose()
        assert len(seen) == 1
        assert seen[0].headers["Authorization"] == "Bearer tok-123"  # 机器凭据同源透传
        assert seen[0].headers["X-Call-ID"] == "call-9"

    asyncio.run(scenario_auth_on())


# ---- 契约常量 ----

def test_contract_defaults_pinned():
    from agent_runtime import metrics_report as mr

    assert mr._ENDPOINT == "/api/metrics/agent-report"
    assert mr.KINDS == ("llm_ttft", "asr_transcribe", "tts_first_audio", "vad_infer")
    assert mr._DEFAULT_INTERVAL_S == 2.0  # 契约 §1：≤2s 批量窗
    assert mr._HTTP_TIMEOUT_S == 2.0  # 契约 §1：上报 timeout 2s
    assert mr._DEFAULT_QUEUE_MAX == 200  # 有界队列（不堆积）


# ---- 源级 pin（agent.py 接线，文本锚）----


def test_import_and_construction_pinned():
    assert "from .metrics_report import MetricsReporter, VadInferTracker" in _AGENT_SRC
    start = _AGENT_SRC.index("_metrics_reporter = MetricsReporter(")
    block = _AGENT_SRC[start : start + 400]
    assert "cp.base_url" in block  # CP 地址复用既有 client（同源）
    assert "cp.request_headers" in block  # 请求头复用（X-Call-ID/X-Bok-Channel/机器凭据）
    assert "call_id=call_id" in block
    assert 'account_id=str((call or {}).get("account_id") or "acc-001")' in block
    # 会话装配时构造：先于 AgentSession（任何 metrics 事件前必已就位）
    assert start < _AGENT_SRC.index("session = AgentSession(")


def test_on_metrics_feeds_four_kinds():
    start = _AGENT_SRC.index("def _on_metrics(ev):")
    end = _AGENT_SRC.index('session.on("metrics_collected", _on_metrics)', start)
    seg = _AGENT_SRC[start:end]
    assert seg.count('_metrics_reporter.add("llm_ttft"') == 1
    assert seg.count('_metrics_reporter.add("tts_first_audio"') == 1
    assert seg.count('_metrics_reporter.add("asr_transcribe"') == 1
    assert seg.count('_metrics_reporter.add("vad_infer"') == 1
    # 口径钉死：tts=官方 ttfb、asr=eou transcription_delay、llm=ttft
    assert 'add("tts_first_audio", m.ttfb * 1000)' in seg
    assert 'add("asr_transcribe", m.transcription_delay * 1000)' in seg
    assert 'add("llm_ttft", m.ttft * 1000)' in seg
    # vad=增量差分器（官方批次总量喂 tracker）
    assert "_vad_tracker.feed(" in seg
    assert "inference_duration_total" in seg and "inference_count" in seg


def test_close_flushes_reporter():
    start = _AGENT_SRC.index("async def _close():")
    end = _AGENT_SRC.index("def _close_done(", start)
    seg = _AGENT_SRC[start:end]
    assert "_metrics_reporter.close()" in seg
