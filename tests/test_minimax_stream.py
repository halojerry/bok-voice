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


def test_minimax_splits_sentences_for_task_continue(monkeypatch):
    """增量语义:按句切分,每个 task_continue 发单句增量(非累积全文)。

    MiniMax WS 实测:发累积全文会重复合成前面句子;按句发增量不重复且首句即出声。
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
            # 之后立刻断开:结束 recv_loop,让 _run 快速收尾
            raise Exception("fake ws closed")

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
        # 跨多块输入,含两句话
        s.push_text("你好，我係林先生。")
        s.push_text("想問下包裹幾時到？")
        s.end_input()
        # 等 _run 消费 input_ch 并发出 task_continue
        for _ in range(100):
            if len([m for m in sent if m.get("event") == "task_continue"]) >= 2:
                break
            await asyncio.sleep(0.05)
        s._task.cancel()  # 测试结束,取消后台任务(FakeWS.recv 挂起不拖慢)

    asyncio.run(run())

    continues = [m for m in sent if m.get("event") == "task_continue"]
    assert continues, "应发出 task_continue"
    texts = [m["text"] for m in continues]
    # 按句切分:每句单独发送,不累积
    assert texts[0] == "你好，我係林先生。", f"首句应为完整单句: {texts}"
    assert texts[-1] == "想問下包裹幾時到？", f"次句应为完整单句: {texts}"
    # 不重复:没有任何 task_continue 的 text 包含前面已发过的句子
    for i, t in enumerate(texts):
        for prev in texts[:i]:
            assert prev not in t, f"task_continue 不应重发前面句子: {texts}"


def test_minimax_stream_missing_key_no_crash():
    tts = MiniMaxTTS(voice="x", sample_rate=24000, api_key="")

    async def _make():
        return tts.stream()

    stream = asyncio.run(_make())
    assert stream is not None


def test_truncate_chat_items_keeps_recent_turns():
    from agent_runtime.providers.livekit_plugins import _truncate_chat_items

    from livekit.agents import llm as lk_llm

    def msg(role, txt):
        return lk_llm.ChatMessage(role=role, content=[txt])

    items = [msg("system", "你是客服"), msg("system", "【知识】规则")]
    for i in range(12):  # 12 轮 user+assistant
        items.append(msg("user", f"客户{i}"))
        items.append(msg("assistant", f"回复{i}"))
    out = _truncate_chat_items(items, max_turns=4)
    roles = [getattr(m, "role", "") for m in out]
    # 开头 system 全保留
    assert roles[0] == "system" and roles[1] == "system"
    # 对话只剩最近 4 对(8 条)
    dialog = roles[2:]
    assert dialog == ["user", "assistant"] * 4, f"应保留最近4对: {dialog}"
    # 最近一轮在
    assert out[-1].content == ["回复11"]
    assert out[-2].content == ["客户11"]
    # 最早轮被截掉
    assert not any(getattr(m, "content", "") == ["客户0"] for m in out)


def test_truncate_chat_items_short_untouched():
    from agent_runtime.providers.livekit_plugins import _truncate_chat_items

    from livekit.agents import llm as lk_llm

    items = [lk_llm.ChatMessage(role="system", content=["s"]),
             lk_llm.ChatMessage(role="user", content=["u"]),
             lk_llm.ChatMessage(role="assistant", content=["a"])]
    out = _truncate_chat_items(items, max_turns=4)
    assert len(out) == 3
