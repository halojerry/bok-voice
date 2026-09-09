"""尾部锚拟声剥离：ContextAwareLLM 出口吞掉模型复刻的【你上一句】块。

2026-09-09 call-974d8da3 实证:S5 尾部瘦身令易变尾部以【你上一句】「…」块
收尾,4B 模型照抄格式进回复(答案后追加标签+自引整块),TTS 念出声。修复=
LLM 流出口单点剥离,渲染不动。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from livekit.agents import llm

from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    ContextAwareLLM,
    ContextState,
    _StripTailAnchorStream,
)


class _FakeInnerStream(llm.LLMStream):
    def __init__(self, plugin, chunks: list[str]):
        from livekit.agents.types import APIConnectOptions

        super().__init__(
            llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions()
        )
        self._chunks = chunks

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        for i, c in enumerate(self._chunks):
            self._event_ch.send_nowait(
                llm.ChatChunk(id=str(i), delta=llm.ChoiceDelta(content=c, role="assistant"))
            )


class _StreamInner(llm.LLM):
    def __init__(self, chunks: list[str]):
        super().__init__()
        self._chunks = chunks

    def chat(self, *, chat_ctx, **kw):
        return _FakeInnerStream(self, self._chunks)


async def _consume(stream) -> str:
    parts: list[str] = []
    async for ev in stream:
        delta = getattr(ev, "delta", None)
        content = getattr(delta, "content", None) if delta is not None else None
        if content:
            parts.append(content)
    return "".join(parts)


def _run_llm(chunks: list[str]) -> str:
    async def _inner() -> str:
        # livekit LLMStream.__init__ 依赖运行中的事件循环,流必须在循环内构造
        wrapped = ContextAwareLLM(inner=_StreamInner(chunks), context_state=ContextState(account_id="t"))
        cc = llm.ChatContext()
        cc.add_message(role="user", content="你好")
        return await _consume(wrapped.chat(chat_ctx=cc))

    return asyncio.run(_inner())


def test_strips_cross_chunk_mimicked_block():
    # 真实事故形状(call-974d8da3):答案 + 标签被流劈开 + 自引整块
    out = _run_llm(
        [
            "好的，您这单是三天前寄出的，对吧？",
            "\n\n【你上一",
            "句】「好的，您这单是三天前寄出的，对吧？」",
        ]
    )
    assert out.strip() == "好的，您这单是三天前寄出的，对吧？"


def test_strips_unclosed_block_at_stream_end():
    # 块到流末都没闭合:整段吞掉(块必然缀在答案后,丢尾安全)
    out = _run_llm(["答案讲完了。", "【你上一句】「答案讲完了"])
    assert out.strip() == "答案讲完了。"


def test_discards_incomplete_label_prefix_at_end():
    # 模仿起头没写完就断流:残缺「【你上一」弃掉,不念残句
    out = _run_llm(["回复正文。", "【你上一"])
    assert out == "回复正文。"


def test_clean_reply_passes_through_intact():
    out = _run_llm(["今天天气", "不错，帮您查。"])
    assert out == "今天天气不错，帮您查。"


def test_benign_bracket_text_not_eaten():
    # 非「你上一句」的括号内容不受牵连(部分前缀只扣住、确认非标签即放行)
    out = _run_llm(["查到啦【好", "消息】马上发您。"])
    assert out == "查到啦【好消息】马上发您。"


def test_stream_wrapper_used_directly():
    # 裸流直喂(不经 ContextAwareLLM)同款行为——多层包装时的直连路径
    async def _inner() -> str:
        src = _StreamInner(["好的。", "\n【你上一句】「好的。」"])
        return await _consume(_StripTailAnchorStream(src, src.chat(chat_ctx=llm.ChatContext())))

    out = asyncio.run(_inner())
    assert out.strip() == "好的。"
