"""刀1 打断弃流（第十七波,2026-10-02,call-4e8d58c1 R4 根修）。

病理：用户开口打断后，框架 speech_handle 给 5s 宽限才硬 cancel；首 token
超时的 drain 接管又压制了 cancel 路径的 abort（``_drain_owns and not force``）
——僵尸 prefill 与下一轮回复在同块 9B 互抢（R4 6110ms 里 96% 白等），且
drain 对已打断轮继续交付晚到答案=重复交付（17:28:08）。

不变量：打断=答案过时=abandon()（force abort 服务端+熔断 drain/regen 交付）；
纯超时（机器慢、答案仍相关）drain 语义零变化。时序门（``_bok_created`` 早于
打断时刻）保下一轮新流永不误杀。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "apps" / "agent"))

from agent_runtime.agent import _llm_fallback_line  # noqa: E402
from agent_runtime.providers import livekit_plugins as _lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    _LlmFallbackStream,
    llm,
)


class _DummyLLM(llm.LLM):
    """LLMStream 基类构造要真 plugin(取 provider 做 trace),哑壳即可。"""

    def chat(self, *, chat_ctx, **kw):  # pragma: no cover - 测试唔会真调
        raise NotImplementedError


_DUMMY = _DummyLLM()


class _FakeInnerStream(llm.LLMStream):
    """按脚本吐 chunk 的假内芯（可带延迟,模拟慢首 token）。"""

    def __init__(self, plugin, chunks, delays=None):
        from livekit.agents import APIConnectOptions

        super().__init__(llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions())
        self._chunks = list(chunks)
        self._delays = list(delays or [])
        self.aclosed = 0

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        for i, text in enumerate(self._chunks):
            if i < len(self._delays) and self._delays[i]:
                await asyncio.sleep(self._delays[i])
            self._event_ch.send_nowait(
                llm.ChatChunk(id=f"fake-{i}", delta=llm.ChoiceDelta(content=text, role="assistant"))
            )

    async def aclose(self) -> None:
        self.aclosed += 1
        await super().aclose()


def _mk_stream(inner, cb, factory=None) -> _LlmFallbackStream:
    return _LlmFallbackStream(
        _DUMMY, inner, _llm_fallback_line("zh"), first_token_timeout_s=0.05,
        stream_factory=factory, late_answer_cb=cb, req_id="test-rid", abort_base="http://127.0.0.1:9999",
    )


# ------------------------------------------------------------------ abandon 本体


def test_abandon_force_abort_bypasses_drain_suppression(monkeypatch):
    # drain 已接管（_drain_owns=True）时普通 cancel 路径被压制——abandon 必须
    # force 穿透（R4 病理正着：cancel 到达时 drain owns=True → abort 被吞）。
    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(_lp, "_fire_mlx_abort", lambda base, rid: fired.append((base, rid)))

    async def _inner():
        stream = _mk_stream(_FakeInnerStream(_DUMMY, ["x"], delays=[0.5]), cb=lambda t: None)
        stream._drain_owns = True
        await stream.abandon()
        return stream

    stream = asyncio.run(_inner())
    assert fired == [("http://127.0.0.1:9999", "test-rid")]
    assert stream._abandoned is True
    assert stream._inner.aclosed >= 1


def test_abandon_idempotent_single_abort(monkeypatch):
    fired: list[tuple[str, str]] = []
    monkeypatch.setattr(_lp, "_fire_mlx_abort", lambda base, rid: fired.append((base, rid)))

    async def _inner():
        stream = _mk_stream(_FakeInnerStream(_DUMMY, ["x"], delays=[0.5]), cb=lambda t: None)
        await stream.abandon()
        await stream.abandon()
        return stream

    stream = asyncio.run(_inner())
    assert len(fired) == 1  # _fire_abort 幂等旗


# ------------------------------------------------------------------ drain/regen 交付熔断


def test_drain_delivery_suppressed_after_abandon(monkeypatch, capsys):
    # 超时→drain 续读；期间用户打断→abandon——晚到文本收齐也**不交付**、
    # 不 regen（被打断轮的补答=重复内容,17:28:08 实证形态）。
    monkeypatch.delenv("LLM_LATE_ANSWER_DEADLINE_S", raising=False)
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    monkeypatch.delenv("LLM_FIRST_TOKEN_TIMEOUT_S", raising=False)
    _lp._famine_reset_for_tests()
    delivered: list[str] = []
    factory_calls = {"n": 0}

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _factory():
        factory_calls["n"] += 1
        return _FakeInnerStream(_DUMMY, ["二发"])

    async def _inner():
        stream = _mk_stream(
            _FakeInnerStream(_DUMMY, ["晚到答案甲", "晚到答案乙"], delays=[0.3, 0.3]),
            cb=_cb, factory=_factory,
        )
        async for _ in stream:
            pass  # 兜底句已出,drain 已起
        await asyncio.sleep(0.1)
        await stream.abandon()  # 用户此刻开口打断
        await asyncio.sleep(0.9)  # 等 drain 收完内芯走到交付点
        return stream

    stream = asyncio.run(_inner())
    assert delivered == []                      # 补答零交付
    assert factory_calls["n"] == 0              # regen 零触发
    assert stream._abandoned is True
    out = capsys.readouterr().out
    assert "LLM_LATE_ANSWER dropped" in out and "(interrupted drain)" in out


def test_drain_delivers_normally_without_abandon(monkeypatch):
    # 对照臂:纯超时（机器慢、无人打断）drain 照收照交付——语义零漂移铁律。
    monkeypatch.delenv("LLM_LATE_ANSWER_DEADLINE_S", raising=False)
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    monkeypatch.delenv("LLM_FIRST_TOKEN_TIMEOUT_S", raising=False)
    _lp._famine_reset_for_tests()
    delivered: list[str] = []

    async def _cb(text: str) -> None:
        delivered.append(text)

    async def _inner():
        stream = _mk_stream(_FakeInnerStream(_DUMMY, ["真答案"], delays=[0.3]), cb=_cb)
        async for _ in stream:
            pass
        await asyncio.sleep(0.7)

    asyncio.run(_inner())
    assert delivered == ["真答案"]


def test_regen_suppressed_after_abandon(monkeypatch):
    # regen 兜底同样熔断:abandon 后 factory 二发绝不发起。
    calls = {"n": 0}

    def _factory():
        calls["n"] += 1
        return _FakeInnerStream(_DUMMY, ["x"])

    async def _inner():
        stream = _mk_stream(_FakeInnerStream(_DUMMY, ["x"], delays=[0.5]), cb=lambda t: None, factory=_factory)
        stream._abandoned = True
        await stream._regen_late_answer()

    asyncio.run(_inner())
    assert calls["n"] == 0


# ------------------------------------------------------------------ 时序门 + 接线 pin


class _Layer:
    def __init__(self, inner=None, abandonable=False, created=0.0):
        self._inner = inner
        if abandonable:
            self.abandon = self._noop
        self._bok_created = created

    async def _noop(self) -> None:
        pass


def test_find_abandonable_timestamp_gate():
    # 下一轮新流（创建晚于打断时刻）绝不返回——单槽竞态防线。
    from agent_runtime.agent import _find_abandonable_stream

    before = time.monotonic()
    stale = _Layer(abandonable=True, created=before - 10)
    fresh = _Layer(abandonable=True, created=before + 10)
    plain = _Layer()
    assert _find_abandonable_stream(_Layer(_Layer(stale)), before) is stale
    assert _find_abandonable_stream(_Layer(_Layer(fresh)), before) is None
    assert _find_abandonable_stream(_Layer(_Layer(plain)), before) is None
    assert _find_abandonable_stream(None, before) is None


def test_interrupt_time_sampled_at_watch_entry_not_after_handle():
    """P0 刀1 取样点回归（2026-10-02 复标）：弃流门必须吃 speech_created 入口时刻。

    原 bug：``now = time.monotonic()`` 在 ``await handle`` 之后取样——框架打断
    有 5s 宽限，宽限 > 下一轮流创建窗（~0.4-1s）时，新流的 ``_bok_created`` 晚于
    真打断时刻但早于收场 now → 被当僵尸错弃。本测试双面钉死：源级（取样点必须
    在 await handle 之前且传给 _find_abandonable_stream）+ 行为面（同一对
    stale/fresh 流，真打断时刻只弃 stale；用收场时刻则误弃 fresh=旧病灶）。
    """
    from agent_runtime.agent import _find_abandonable_stream

    src = (_REPO / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    _start = src.index("async def _watch() -> None:")
    _end = src.index("# 池化(2026-09-17 全量 debug P2-A)", _start)
    watch = src[_start:_end]
    entry = watch.index("_interrupt_at = time.monotonic()")
    await_pos = watch.index("\n            try:\n                await handle")  # 代码行,非注释引用
    assert entry < await_pos, "打断时刻必须在 await handle 之前取样"
    assert "_find_abandonable_stream(_reap_stream, _interrupt_at)" in watch
    assert "_find_abandonable_stream(_reap_stream, now)" not in watch

    interrupt_at = time.monotonic()
    stale = _Layer(abandonable=True, created=interrupt_at - 5.0)    # 本通被打断的僵尸流
    fresh = _Layer(abandonable=True, created=interrupt_at + 0.6)    # 宽限窗内下一轮新流
    completion = interrupt_at + 5.0                                 # 宽限后收场时刻
    assert _find_abandonable_stream(_Layer(stale), interrupt_at) is stale
    assert _find_abandonable_stream(_Layer(fresh), interrupt_at) is None
    # 旧取样点（收场时刻）=新流落进「早于」侧被误弃——病灶形状本身在案。
    assert _find_abandonable_stream(_Layer(fresh), completion) is fresh


def test_source_pins_interrupt_abandon_wiring():
    # 源级 pin:打断分支必须调 abandon（防未来重构静默脱线——wave-13 同文件
    # 并行撞车教训的终态复验形态）。
    agent_src = (_REPO / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    lp_src = (_REPO / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
    assert "_find_abandonable_stream(_reap_stream, _interrupt_at)" in agent_src
    assert "await _fs.abandon()" in agent_src
    assert "async def abandon(self)" in lp_src
    assert "LLM_LATE_ANSWER dropped" in lp_src
