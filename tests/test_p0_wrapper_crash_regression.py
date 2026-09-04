"""P0 崩溃修复回归（f2b2a98 E2E 实测两颗炸，见 .superpowers/.../rca-e2e-budget.md）：

1. LLM 包装层（ContextAwareLLM/ExprAwareLLM/StatelessMTLLM）把默认
   extra_kwargs=None 透传给官方 openai 内芯 → 内芯 is_given(None)=True →
   extra.update(None) TypeError，每轮 chat 必炸（Error in _llm_inference_task）。
   回归口径：内芯只允许收到 NOT_GIVEN 或 dict，None 绝不能透传。
2. _Qwen3ASRLiveStream 把 utils.merge_frames(event.frames) 当可迭代对象 for
   遍历——1.7 的 merge_frames=rtc.combine_audio_frames 返回【单个】
   rtc.AudioFrame（不可迭代）→ 首次 INFERENCE_DONE 即 TypeError，STT 流死。
   回归口径：用真 rtc.AudioFrame 走完整 _run，说话帧必须进 _pending。

不依赖真实模型/服务：内芯用 _Recorder 桩或本地 MlxLlmLLM（不迭代不发包）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from livekit.agents import APIConnectOptions, NOT_GIVEN, llm, stt, utils, vad
from livekit.agents.utils import is_given
from livekit import rtc


def _user_ctx(text: str = "你好") -> llm.ChatContext:
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content=text)
    return ctx


class _Recorder(llm.LLM):
    """记录内芯实际收到的 extra_kwargs；同时记录其余透传参数形态。"""

    provider = "recorder"
    model = "recorder"

    def __init__(self):
        super().__init__()
        self.received: list[object] = []

    def chat(self, *, chat_ctx, tools=None, conn_options=None, parallel_tool_calls=None,
             tool_choice=None, extra_kwargs=NOT_GIVEN):
        self.received.append(extra_kwargs)
        return None


def _drive_chat(wrapper: llm.LLM, *, extra_kwargs=NOT_GIVEN):
    """在事件循环里调一次 wrapper.chat 并立即关闭流（构造 LLMStream 需要运行中的 loop）。"""

    async def _run():
        ctx = _user_ctx()
        if extra_kwargs is NOT_GIVEN:
            stream = wrapper.chat(chat_ctx=ctx)
        else:
            stream = wrapper.chat(chat_ctx=ctx, extra_kwargs=extra_kwargs)
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - 桩内芯返回 None 时收尾可能报错，非断言点
                pass
        return stream

    return asyncio.run(asyncio.wait_for(_run(), timeout=10))


def test_wrappers_never_forward_none_extra_kwargs():
    """三种调用姿势（缺省 / 显式 None / 真 dict）下，内芯收到 NOT_GIVEN 或 dict，绝不收 None。"""
    from agent_runtime.providers.livekit_plugins import (
        ContextAwareLLM,
        ExprAwareLLM,
        StatelessMTLLM,
    )

    wrappers = [
        ("context_aware", ContextAwareLLM(_Recorder())),
        ("expr_aware", ExprAwareLLM(_Recorder())),
        ("stateless_mt", StatelessMTLLM(_Recorder(), "en")),
    ]
    for name, wrapper in wrappers:
        # 1) 框架姿势：不传 extra_kwargs（AgentSession 从不传）。
        _drive_chat(wrapper)
        # 2) 兜底姿势：显式 None 也必须被挡下。
        _drive_chat(wrapper, extra_kwargs=None)
        # 3) 真 dict：原样透传。
        _drive_chat(wrapper, extra_kwargs={"foo": "bar"})

        inner = wrapper._inner
        assert len(inner.received) == 3, (name, inner.received)
        assert not is_given(inner.received[0]), (name, inner.received[0])
        assert inner.received[0] is NOT_GIVEN, (name, inner.received[0])
        assert not is_given(inner.received[1]), (name, inner.received[1])
        assert inner.received[1] is NOT_GIVEN, (name, inner.received[1])
        assert inner.received[2] == {"foo": "bar"}, (name, inner.received[2])
        assert inner.received[2] is not None, (name, inner.received[2])
        # None 绝不出现在任何一次透传里。
        assert all(v is not None for v in inner.received), (name, inner.received)


def test_context_aware_llm_chat_real_openai_inner_no_crash():
    """真 openai 内芯（MlxLlmLLM）复现原炸点：缺省 extra_kwargs 的 chat 必须能建流不 TypeError。

    原崩溃发生在内芯 chat() 同步段（extra.update(None)），建流前即炸——
    本测试不迭代流、不发包，只验证包装层→内芯建流全程无 TypeError。
    """
    from agent_runtime.providers.livekit_plugins import ContextAwareLLM, MlxLlmLLM

    inner = MlxLlmLLM(model="test-model")
    wrapped = ContextAwareLLM(inner)

    async def _run():
        ctx = _user_ctx()
        stream = wrapped.chat(chat_ctx=ctx)  # 修复前：TypeError: 'NoneType' object is not iterable
        assert isinstance(stream, llm.LLMStream)
        await stream.aclose()

    asyncio.run(asyncio.wait_for(_run(), timeout=10))


def _fake_stt_backend():
    """_Qwen3ASRLiveStream 只用到 _stt_ 的 base_url/语言状态/pin 三个属性。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        _base_url="http://127.0.0.1:1",  # 无服务：partial/finish 走异常分支，不断言网络
        _pin_language=False,
        _language_state=SimpleNamespace(lang="cantonese"),
    )


class _FakeVADStream:
    """鸭型 VAD 流：按脚本吐事件，记录 push_frame（官方 StreamAdapter 同款接口）。"""

    def __init__(self, events):
        self._events = iter(events)
        self.pushed: list[rtc.AudioFrame] = []

    def push_frame(self, frame):
        self.pushed.append(frame)

    def flush(self):
        pass

    def end_input(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeVAD:
    def __init__(self, events):
        self._events = events
        self.last_stream: _FakeVADStream | None = None

    def stream(self):
        self.last_stream = _FakeVADStream(self._events)
        return self.last_stream


def test_asr_live_stream_speech_frames_reach_pending():
    """INFERENCE_DONE 说话帧必须进 _pending：merge_frames 返回单 Frame，直接用返回值。

    修复前 `for f in utils.merge_frames(event.frames)` 首次 INFERENCE_DONE 即
    TypeError（AudioFrame 不可迭代），_run 整体炸掉 → STT 流死、agent 哑火。
    """
    from agent_runtime.providers.livekit_plugins import _Qwen3ASRLiveStream

    # 版本契约钉死：merge_frames 收 list 返回【单个】不可迭代 Frame。
    fa = rtc.AudioFrame(data=b"\x01\x00" * 8, sample_rate=16000, num_channels=1, samples_per_channel=8)
    fb = rtc.AudioFrame(data=b"\x02\x00" * 8, sample_rate=16000, num_channels=1, samples_per_channel=8)
    merged = utils.merge_frames([fa, fb])
    assert isinstance(merged, rtc.AudioFrame)
    try:
        iter(merged)  # noqa: B015 - 故意探测：单 Frame 不可迭代
        raise AssertionError("merge_frames 返回值不应可迭代(版本行为变了,需重审本修复)")
    except TypeError:
        pass
    assert bytes(merged.data) == b"\x01\x00" * 8 + b"\x02\x00" * 8

    events = [
        vad.VADEvent(
            type=vad.VADEventType.START_OF_SPEECH,
            samples_index=0,
            timestamp=0.0,
            speech_duration=0.0,
            silence_duration=0.0,
            frames=[],
        ),
        vad.VADEvent(
            type=vad.VADEventType.INFERENCE_DONE,
            samples_index=8,
            timestamp=0.0,
            speech_duration=1.0,
            silence_duration=0.0,
            frames=[fa, fb],
        ),
    ]
    fake_vad = _FakeVAD(events)

    async def _run():
        stream = _Qwen3ASRLiveStream(
            _fake_stt_backend(),
            vad=fake_vad,
            conn_options=APIConnectOptions(),
        )
        # 推一帧并收口:_forward_input 转发完后退出,输入通道关闭 → gather 收敛。
        stream.push_frame(fa)
        stream.end_input()
        got = []
        async for ev in stream:
            got.append(ev.type)
            if len(got) > 4:  # noqa: PLR2004 - 防御性上限,正常只有 START_OF_SPEECH
                break
        return stream, got

    stream, got = asyncio.run(asyncio.wait_for(_run(), timeout=15))
    assert stt.SpeechEventType.START_OF_SPEECH in got
    # 修复前走不到这里：_run 在 INFERENCE_DONE 处 TypeError,流异常终止。
    assert bytes(stream._pending) == b"\x01\x00" * 8 + b"\x02\x00" * 8, bytes(stream._pending)[:32]
    assert fake_vad.last_stream.pushed  # 输入帧确实经 forward 进了 VAD
