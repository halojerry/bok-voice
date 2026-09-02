"""MiniMaxTTS 真流式(streaming=True + SynthesizeStream)契约与增量语义。

核心防回归点：
1. capabilities.streaming 必须为 True —— livekit 才会走 stream()(SynthesizeStream)，
   而不是被 StreamAdapter+SentenceTokenizer 包(那会等整句/全文,首包 8-18s)。
2. stream() 返回 SynthesizeStream 实例(而非 NotImplementedError)。
3. task_continue 的 text 是「累积全文」而非纯增量(实测纯增量丢 ~40% 音频)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import asyncio  # noqa: E402

import pytest  # noqa: E402

from agent_runtime.providers.livekit_plugins import MiniMaxTTS  # noqa: E402


def _make_tts():
    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse", "yue": "Cantonese_Male_news_anchor_vv2"},
        sample_rate=24000,
        api_key="test-key",
    )


def test_minimax_capabilities_streaming_true():
    # 真流式:streaming=True 让 voice 管线走 stream(),不包 StreamAdapter 句子切分。
    tts = _make_tts()
    assert tts.capabilities.streaming is True


def test_minimax_stream_returns_synthesize_stream():
    from livekit.agents import tts as lk_tts

    tts = _make_tts()

    async def _make():
        return tts.stream()

    stream = asyncio.run(_make())
    # SynthesizeStream 实例:livekit 会 async with stream / push_text / end_input / async for
    assert isinstance(stream, lk_tts.SynthesizeStream)


def test_minimax_stream_is_not_chunked_stream():
    # ChunkedStream(synthesize 返回)不代表流式;stream() 必须返回 SynthesizeStream。
    from livekit.agents import tts as lk_tts

    tts = _make_tts()

    async def _make():
        synth = tts.synthesize("你好")
        stream = tts.stream()
        return synth, stream

    synth, stream = asyncio.run(_make())
    assert isinstance(synth, lk_tts.ChunkedStream)
    assert not isinstance(synth, lk_tts.SynthesizeStream)
    assert isinstance(stream, lk_tts.SynthesizeStream)
    assert not isinstance(stream, lk_tts.ChunkedStream)


def test_minimax_accumulates_text_for_task_continue(monkeypatch):
    """增量语义:每次 task_continue 发「累积全文」而非纯增量。

    MiniMax WS 的 task_continue.text 是到目前为止的全部文本(实测纯增量丢音频);
    本测试用假 WS 捕获发送的 task_continue,断言 text 单调累积。
    """
    import json

    from agent_runtime.providers.livekit_plugins import _MiniMaxSynthesizeStream

    sent: list[dict] = []
    recv_count = {"n": 0}

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                return json.dumps({"event": "connected_success"})
            if recv_count["n"] == 2:
                return json.dumps({"event": "task_started"})
            await asyncio.sleep(3600)  # 之后不发音频,让测试专注发送侧

        async def send(self, payload):
            sent.append(json.loads(payload))

        async def close(self):
            pass

    calls: dict[str, int] = {"n": 0}

    async def fake_connect(*a, **kw):
        return FakeWS()

    monkeypatch.setattr("websockets.connect", fake_connect)

    tts = _make_tts()

    async def run():
        # 正常生命周期:构造即起 _main_task 跑 _run(连 FakeWS→task_start→读 input_ch)
        from livekit.agents import APIConnectOptions

        from agent_runtime.providers.livekit_plugins import _MiniMaxSynthesizeStream

        s = _MiniMaxSynthesizeStream(tts, APIConnectOptions())
        s.push_text("你好，")
        s.push_text("我係林先生，")
        s.push_text("想問下包裹幾時到？")
        s.end_input()
        # 等 _run 消费 input_ch 并发完 3 次 task_continue
        for _ in range(100):
            if len([m for m in sent if m.get("event") == "task_continue"]) >= 3:
                break
            await asyncio.sleep(0.05)
        s._task.cancel()  # 测试结束,取消后台任务(FakeWS.recv 挂起不拖慢)

    asyncio.run(run())

    continues = [m for m in sent if m.get("event") == "task_continue"]
    assert continues, "应发出 task_continue"
    texts = [m["text"] for m in continues]
    # 累积:后一段文本包含前一段
    for i in range(1, len(texts)):
        assert texts[i].startswith(texts[i - 1]), f"task_continue 应累积: {texts}"
    # 最终文本是完整拼接
    assert texts[-1] == "你好，我係林先生，想問下包裹幾時到？"
    # 首段不应为空/纯增量丢字
    assert texts[0] == "你好，"


def test_minimax_stream_missing_key_no_crash():
    tts = MiniMaxTTS(voice="x", sample_rate=24000, api_key="")

    async def _make():
        return tts.stream()

    stream = asyncio.run(_make())
    assert stream is not None
