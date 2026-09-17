"""LLM 主回复 deadline + 兜底直念(2026-09-17)。

框架恒传 DEFAULT conn_options(timeout=10s×max_retry=3 → 最坏 ~46s 静默后整轮
失败)。MlxLlmLLM 覆写默认档(LLM_REQUEST_TIMEOUT_S/LLM_REQUEST_RETRIES)并在
重试耗尽后发单句三语兜底——治「LLM 卡死客户听死寂直到心跳」。

超时契约(RC4,2026-09-17 修订):首 token 超时出兜底句后**唔弃流**——后台
drain 继续消费同一条内芯流收晚到真答案(mlx 服务端无断连中止,aclose 弃流
只换僵尸解码税+二发排队抬 TTFT);drain 次级截止(LLM_LATE_ANSWER_DEADLINE_S,
默认 8s,0=关回 aclose+factory 重生旧行为)仍无产出才真弃流重生。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, llm

from agent_runtime.agent import _llm_fallback_line  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    ContextAwareLLM,
    ContextState,
    MlxLlmLLM,
    _LlmFallbackStream,
)


class _FakeInnerStream(llm.LLMStream):
    def __init__(self, plugin, chunks: list[str], boom: bool = False):
        super().__init__(
            llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions()
        )
        self._chunks = chunks
        self._boom = boom

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        for i, c in enumerate(self._chunks):
            self._event_ch.send_nowait(
                llm.ChatChunk(id=str(i), delta=llm.ChoiceDelta(content=c, role="assistant"))
            )
        if self._boom:
            raise RuntimeError("simulated llm failure after retries")


class _DummyLLM(llm.LLM):
    """LLMStream 基类构造要真 plugin(取 provider 做 trace),哑壳即可。"""

    def chat(self, *, chat_ctx, **kw):  # pragma: no cover - 测试唔会真调
        raise NotImplementedError


_DUMMY = _DummyLLM()


def _collect(stream_factory) -> str:
    async def _inner() -> str:
        stream = stream_factory()  # LLMStream 构造要喺事件循环内(基类取 running loop)
        parts: list[str] = []
        async for ev in stream:
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)
        return "".join(parts)

    return asyncio.run(_inner())


# ---- 三语兜底文本 ----


def test_llm_fallback_line_three_langs():
    langs = {"cantonese": _llm_fallback_line("cantonese"), "zh": _llm_fallback_line("zh"), "en": _llm_fallback_line("en")}
    assert all(langs.values())
    assert len(set(langs.values())) == 3
    assert len(langs["cantonese"]) <= 30  # ≤22 字级,压 TTS 时长


# ---- conn_options 覆写档 ----


def test_request_conn_options_defaults(monkeypatch):
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT_S", raising=False)
    monkeypatch.delenv("LLM_REQUEST_RETRIES", raising=False)
    opts = MlxLlmLLM._request_conn_options()
    assert opts.timeout == 8.0
    assert opts.max_retry == 0  # 插件级重试归零:恢复交给兜底壳(有声、更快)


def test_request_conn_options_env_override(monkeypatch):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_S", "12")
    monkeypatch.setenv("LLM_REQUEST_RETRIES", "2")
    opts = MlxLlmLLM._request_conn_options()
    assert opts.timeout == 12.0
    assert opts.max_retry == 2


def test_request_conn_options_zero_returns_official_default(monkeypatch):
    # timeout=0 = 逃生口:连覆写都唔要,回官方默认(旧行为)。
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_S", "0")
    assert MlxLlmLLM._request_conn_options() is DEFAULT_API_CONNECT_OPTIONS


def test_request_conn_options_negative_retry_clamped_to_zero(monkeypatch):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_S", "5")
    monkeypatch.setenv("LLM_REQUEST_RETRIES", "-3")
    opts = MlxLlmLLM._request_conn_options()
    assert opts.max_retry == 0
    assert opts.timeout == 5.0


def test_first_token_timeout_default_and_env(monkeypatch):
    monkeypatch.delenv("LLM_FIRST_TOKEN_TIMEOUT_S", raising=False)
    assert MlxLlmLLM._first_token_timeout_s() == 2.0
    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT_S", "1.5")
    assert MlxLlmLLM._first_token_timeout_s() == 1.5
    monkeypatch.setenv("LLM_FIRST_TOKEN_TIMEOUT_S", "0")
    assert MlxLlmLLM._first_token_timeout_s() == 0.0  # 0=关闸


# ---- 超时→原流续读 drain(RC4 契约:唔弃流,晚到答案照收) ----


def test_timeout_drains_same_stream_delivers_late_answer(monkeypatch):
    # 首 token 超时 → 兜底句先出;唔 aclose,后台 drain 消费同一条内芯流,
    # 晚到真答案照交付——factory 二发绝不被调(僵尸解码税回归门)。
    monkeypatch.delenv("LLM_LATE_ANSWER_DEADLINE_S", raising=False)
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    delivered: list[str] = []
    factory_calls = {"n": 0}

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _factory():
        factory_calls["n"] += 1
        return _FakeInnerStream(_DUMMY, ["二发答案"])

    async def _inner():
        stream = _LlmFallbackStream(
            _DUMMY, _SlowFirstChunkStream(_DUMMY, ["您講嘅單號", "我記低咗"], delay_s=0.3),
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            stream_factory=_factory, late_answer_cb=_cb,
        )
        parts = []
        async for ev in stream:
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)
        await asyncio.sleep(0.6)  # 等后台 drain 消费完内芯
        return "".join(parts)

    out = asyncio.run(_inner())
    assert _llm_fallback_line("zh") in out          # 客户先听到兜底句
    assert "您講嘅單號" not in out                  # 晚到答案唔占主回复位
    assert delivered == ["您講嘅單號我記低咗"]       # 原流 drain 照收晚到真答案
    assert factory_calls["n"] == 0                  # factory 二发零调用


def test_regen_env_zero_does_not_block_drain(monkeypatch):
    # BOK_LLM_REGEN=0 只封 factory 二发;原流 drain 照收晚到答案(门控语义不变)。
    monkeypatch.setenv("BOK_LLM_REGEN", "0")
    monkeypatch.delenv("LLM_LATE_ANSWER_DEADLINE_S", raising=False)
    delivered: list[str] = []
    factory_calls = {"n": 0}

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _factory():
        factory_calls["n"] += 1
        return _FakeInnerStream(_DUMMY, ["答案"])

    async def _inner():
        stream = _LlmFallbackStream(
            _DUMMY, _SlowFirstChunkStream(_DUMMY, ["慢"], delay_s=0.3),
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            stream_factory=_factory, late_answer_cb=_cb,
        )
        async for _ in stream:
            pass
        await asyncio.sleep(0.6)

    asyncio.run(_inner())
    assert delivered == ["慢"]
    assert factory_calls["n"] == 0


class _SilentStream(llm.LLMStream):
    """永不产出(模拟服务端死流);记录 aclose——drain 截止/kill-switch 测试用。"""

    def __init__(self, plugin, hold_s: float = 30.0):
        super().__init__(
            llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions()
        )
        self._hold = hold_s
        self.aclose_calls = 0

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        await asyncio.sleep(self._hold)

    async def aclose(self):
        self.aclose_calls += 1
        await super().aclose()


class _TrickleStallStream(llm.LLMStream):
    """首 chunk 慢→出几个 chunk→永久卡住:drain 截止 partial salvage 测试用。"""

    def __init__(self, plugin, chunks: list[str], first_delay_s: float, stall_hold_s: float = 30.0):
        super().__init__(
            llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions()
        )
        self._chunks = chunks
        self._first_delay = first_delay_s
        self._stall_hold = stall_hold_s

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        await asyncio.sleep(self._first_delay)
        for i, c in enumerate(self._chunks):
            self._event_ch.send_nowait(
                llm.ChatChunk(id=str(i), delta=llm.ChoiceDelta(content=c, role="assistant"))
            )
        await asyncio.sleep(self._stall_hold)


class _LateBoomStream(llm.LLMStream):
    """超时后才爆(零产出):drain 收到内芯中途异常 → 落 factory regen。"""

    def __init__(self, plugin, delay_s: float):
        super().__init__(
            llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions()
        )
        self._delay = delay_s

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        await asyncio.sleep(self._delay)
        raise RuntimeError("mid-drain llm failure")


def test_drain_deadline_exceeded_falls_back_to_regen(monkeypatch):
    # drain 次级截止仍无产出 → 真弃流 aclose + factory 二发兜底(最后手段)。
    monkeypatch.setenv("LLM_LATE_ANSWER_DEADLINE_S", "0.2")
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    delivered: list[str] = []

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _factory():
        return _FakeInnerStream(_DUMMY, ["重生答案"])

    async def _inner():
        silent = _SilentStream(_DUMMY)  # LLMStream 构造要喺事件循环内
        stream = _LlmFallbackStream(
            _DUMMY, silent,
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            stream_factory=_factory, late_answer_cb=_cb,
        )
        async for _ in stream:
            pass
        await asyncio.sleep(0.6)
        return silent

    silent = asyncio.run(_inner())
    assert delivered == ["重生答案"]        # factory regen 照兜底
    assert silent.aclose_calls == 1         # 截止到点才真弃流


def test_regen_disabled_blocks_factory_fallback(monkeypatch):
    # BOK_LLM_REGEN=0:drain 截止后 factory 二发被门控拦下,唔重生(交付为空)。
    monkeypatch.setenv("LLM_LATE_ANSWER_DEADLINE_S", "0.2")
    monkeypatch.setenv("BOK_LLM_REGEN", "0")
    delivered: list[str] = []
    factory_calls = {"n": 0}

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _factory():
        factory_calls["n"] += 1
        return _FakeInnerStream(_DUMMY, ["答案"])

    async def _inner():
        stream = _LlmFallbackStream(
            _DUMMY, _SilentStream(_DUMMY),
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            stream_factory=_factory, late_answer_cb=_cb,
        )
        async for _ in stream:
            pass
        await asyncio.sleep(0.5)

    asyncio.run(_inner())
    assert delivered == []
    assert factory_calls["n"] == 0


def test_regen_factory_failure_is_swallowed(monkeypatch):
    # drain 截止后二发也失败:异常吞掉(兜底句已在),唔炸会话。
    monkeypatch.setenv("LLM_LATE_ANSWER_DEADLINE_S", "0.2")
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    delivered: list[str] = []

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _bad_factory():
        raise RuntimeError("server down")

    async def _inner():
        stream = _LlmFallbackStream(
            _DUMMY, _SilentStream(_DUMMY),
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            stream_factory=_bad_factory, late_answer_cb=_cb,
        )
        async for _ in stream:
            pass
        await asyncio.sleep(0.5)

    asyncio.run(_inner())
    assert delivered == []


def test_deadline_zero_immediate_aclose_regen(monkeypatch):
    # LLM_LATE_ANSWER_DEADLINE_S=0 = kill-switch:立即 aclose 弃流+factory 重生
    # (旧行为),drain 唔启动。
    monkeypatch.setenv("LLM_LATE_ANSWER_DEADLINE_S", "0")
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    delivered: list[str] = []
    factory_calls = {"n": 0}

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _factory():
        factory_calls["n"] += 1
        return _FakeInnerStream(_DUMMY, ["重生答案"])

    async def _inner():
        silent = _SilentStream(_DUMMY, hold_s=5.0)  # LLMStream 构造要喺事件循环内
        stream = _LlmFallbackStream(
            _DUMMY, silent,
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            stream_factory=_factory, late_answer_cb=_cb,
        )
        async for _ in stream:
            pass
        await asyncio.sleep(0.5)
        return silent

    silent = asyncio.run(_inner())
    assert delivered == ["重生答案"]        # 只可能来自 factory regen(内芯已被弃)
    assert factory_calls["n"] == 1
    assert silent.aclose_calls == 1         # 立即弃流(唔等 drain)


def test_drain_salvages_partial_on_deadline(monkeypatch):
    # 内芯出了部分真答案后卡死,drain 截止 → 交付已有部分(salvage),
    # 唔再 factory 二发重复问同一条。
    monkeypatch.setenv("LLM_LATE_ANSWER_DEADLINE_S", "0.4")
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    delivered: list[str] = []
    factory_calls = {"n": 0}

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _factory():
        factory_calls["n"] += 1
        return _FakeInnerStream(_DUMMY, ["二发答案"])

    async def _inner():
        stream = _LlmFallbackStream(
            _DUMMY, _TrickleStallStream(_DUMMY, ["部分", "真答案"], first_delay_s=0.2),
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            stream_factory=_factory, late_answer_cb=_cb,
        )
        async for _ in stream:
            pass
        await asyncio.sleep(0.8)

    asyncio.run(_inner())
    assert delivered == ["部分真答案"]
    assert factory_calls["n"] == 0


def test_drain_inner_failure_falls_back_to_regen(monkeypatch):
    # drain 期间内芯中途抛异常(零产出) → aclose + factory regen。
    monkeypatch.setenv("LLM_LATE_ANSWER_DEADLINE_S", "5")
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    delivered: list[str] = []

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _factory():
        return _FakeInnerStream(_DUMMY, ["重生答案"])

    async def _inner():
        stream = _LlmFallbackStream(
            _DUMMY, _LateBoomStream(_DUMMY, delay_s=0.2),
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            stream_factory=_factory, late_answer_cb=_cb,
        )
        async for _ in stream:
            pass
        await asyncio.sleep(0.6)

    asyncio.run(_inner())
    assert delivered == ["重生答案"]


def test_drain_closes_inner_after_success(monkeypatch):
    # drain 正常收完 → aclose 内芯(卫生收尾),交付一次。
    monkeypatch.setenv("LLM_LATE_ANSWER_DEADLINE_S", "3")
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    delivered: list[str] = []

    async def _cb(text: str) -> None:
        delivered.append(text)

    async def _inner():
        slow = _SlowFirstChunkStream(_DUMMY, ["答案"], delay_s=0.2)  # 构造要喺循环内
        stream = _LlmFallbackStream(
            _DUMMY, slow,
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            late_answer_cb=_cb,
        )
        async for _ in stream:
            pass
        await asyncio.sleep(0.6)
        return slow

    slow = asyncio.run(_inner())
    assert delivered == ["答案"]
    assert slow.aclose_calls == 1


def test_late_answer_deadline_default_and_env(monkeypatch):
    from agent_runtime.providers.livekit_plugins import _late_answer_deadline_s

    monkeypatch.delenv("LLM_LATE_ANSWER_DEADLINE_S", raising=False)
    assert _late_answer_deadline_s() == 8.0
    monkeypatch.setenv("LLM_LATE_ANSWER_DEADLINE_S", "12.5")
    assert _late_answer_deadline_s() == 12.5
    monkeypatch.setenv("LLM_LATE_ANSWER_DEADLINE_S", "0")
    assert _late_answer_deadline_s() == 0.0  # 0=kill-switch(回 aclose+regen 旧行为)


# ---- 兜底流:内芯失败 → 单句兜底;内芯正常 → 原样透传 ----


class _SlowFirstChunkStream(llm.LLMStream):
    """首 chunk 慢到（模拟服务端排队/冷 prefill）——首 token 截止测试用。"""

    def __init__(self, plugin, chunks: list[str], delay_s: float):
        super().__init__(
            llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions()
        )
        self._chunks = chunks
        self._delay = delay_s
        self.aclose_calls = 0

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        await asyncio.sleep(self._delay)
        for i, c in enumerate(self._chunks):
            self._event_ch.send_nowait(
                llm.ChatChunk(id=str(i), delta=llm.ChoiceDelta(content=c, role="assistant"))
            )

    async def aclose(self):
        self.aclose_calls += 1
        await super().aclose()


def test_fallback_stream_emits_line_on_failure():
    def _factory():
        return _LlmFallbackStream(_DUMMY, _FakeInnerStream(_DUMMY, ["你说什", "么"], boom=True), _llm_fallback_line("cantonese"))

    out = _collect(_factory)
    assert "你说什么" in out
    assert _llm_fallback_line("cantonese") in out


def test_fallback_stream_passthrough_on_success():
    out = _collect(lambda: _LlmFallbackStream(_DUMMY, _FakeInnerStream(_DUMMY, ["正常回复"]), _llm_fallback_line("zh")))
    assert out == "正常回复"


def test_fallback_stream_survives_empty_failure():
    # 首 token 都未出就死:兜底句照出,客户唔会食死寂。
    out = _collect(lambda: _LlmFallbackStream(_DUMMY, _FakeInnerStream(_DUMMY, [], boom=True), _llm_fallback_line("en")))
    assert out == _llm_fallback_line("en")


def test_first_token_deadline_triggers_fallback():
    # 首 chunk 1s 先到、截止 0.05s → 客户端弃流,只出兜底句(客服口径:唔等)。
    out = _collect(lambda: _LlmFallbackStream(
        _DUMMY, _SlowFirstChunkStream(_DUMMY, ["真答案"], delay_s=1.0),
        _llm_fallback_line("cantonese"), first_token_timeout_s=0.05,
    ))
    assert out == _llm_fallback_line("cantonese")
    assert "真答案" not in out


def test_first_token_in_time_passthrough():
    # 首 chunk 即时到(暖轮 <2.5s 常态) → 真答案照出,闸门零打扰。
    out = _collect(lambda: _LlmFallbackStream(
        _DUMMY, _FakeInnerStream(_DUMMY, ["您讲嘅单号我记低咗"]),
        _llm_fallback_line("zh"), first_token_timeout_s=0.05,
    ))
    assert "您讲嘅单号" in out
    assert _llm_fallback_line("zh") not in out


def test_first_token_deadline_zero_disables():
    # deadline=0:唔计时,慢答案照等(旧行为)。
    out = _collect(lambda: _LlmFallbackStream(
        _DUMMY, _SlowFirstChunkStream(_DUMMY, ["慢但真"], delay_s=0.3),
        _llm_fallback_line("zh"), first_token_timeout_s=0.0,
    ))
    assert out == "慢但真"


# ---- ContextAwareLLM 部分文本 tee(B4 数据源) ----


class _CaptureInner(llm.LLM):
    def __init__(self, chunks: list[str], boom: bool = False):
        super().__init__()
        self._chunks = chunks
        self._boom = boom

    def chat(self, *, chat_ctx, **kw):
        return _FakeInnerStream(self, self._chunks, boom=self._boom)


def _run_captured(chunks: list[str], boom: bool = False) -> tuple[str, dict]:
    async def _inner():
        capture = {"text": ""}
        ctx = ContextState(account_id="t")
        wrapped = ContextAwareLLM(inner=_CaptureInner(chunks, boom=boom), context_state=ctx)
        wrapped.set_partial_capture(capture)
        cc = llm.ChatContext()
        cc.add_message(role="user", content="你好")
        parts: list[str] = []
        err = None
        try:
            async for ev in wrapped.chat(chat_ctx=cc):
                delta = getattr(ev, "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    parts.append(content)
        except RuntimeError as exc:  # 内芯异常透传(取消/失败族)
            err = exc
        return "".join(parts), dict(capture), err

    return asyncio.run(_inner())


def test_partial_capture_normal_completion_clears():
    out, capture, err = _run_captured(["您讲嘅单号", "我记低咗"])
    assert err is None
    assert out == "您讲嘅单号我记低咗"
    assert capture["text"] == ""  # 正常走完清空(item_added 照常上报,零双记)


def test_partial_capture_keeps_text_on_failure():
    # 回复生成到一半链路死(=打断族)→ tee 保留部分文本,agent watcher 拿去补记。
    out, capture, err = _run_captured(["您讲嘅单号", "我记"], boom=True)
    assert err is not None
    assert capture["text"] == "您讲嘅单号我记"


def test_partial_capture_disabled_by_default():
    async def _inner():
        ctx = ContextState(account_id="t")
        wrapped = ContextAwareLLM(inner=_CaptureInner(["x"]), context_state=ctx)
        cc = llm.ChatContext()
        cc.add_message(role="user", content="你好")
        parts = []
        async for ev in wrapped.chat(chat_ctx=cc):
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)
        return "".join(parts)

    assert asyncio.run(_inner()) == "x"  # 未注入 tee:零行为变化


def test_fallback_gate_suppresses_ack_when_filler_covered(monkeypatch):
    # 2026-09-17 call-11132bdd:垫话已盖耳的轮,2s 超时的道歉句係叠床架屋
    # (「垫话+道歉+晚到真答案」三连)——闸合=跳过流内兜底 chunk,drain 照收
    # 晚到真答案,factory 二发零调用。
    monkeypatch.delenv("LLM_LATE_ANSWER_DEADLINE_S", raising=False)
    monkeypatch.delenv("BOK_LLM_REGEN", raising=False)
    delivered: list[str] = []
    factory_calls = {"n": 0}

    async def _cb(text: str) -> None:
        delivered.append(text)

    def _factory():
        factory_calls["n"] += 1
        return _FakeInnerStream(_DUMMY, ["二发答案"])

    async def _inner():
        stream = _LlmFallbackStream(
            _DUMMY, _SlowFirstChunkStream(_DUMMY, ["您講嘅單號", "我記低咗"], delay_s=0.3),
            _llm_fallback_line("zh"), first_token_timeout_s=0.05,
            stream_factory=_factory, late_answer_cb=_cb,
            fallback_gate=lambda: True,  # 垫话已盖耳
        )
        parts = []
        async for ev in stream:
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)
        await asyncio.sleep(0.6)
        return "".join(parts)

    out = asyncio.run(_inner())
    assert _llm_fallback_line("zh") not in out   # 道歉句被抑制
    assert out.strip() == ""                     # 主回复零内容(静默交给晚到)
    assert delivered == ["您講嘅單號我記低咗"]     # 晚到真答案照交付
    assert factory_calls["n"] == 0               # factory 二发零调用
