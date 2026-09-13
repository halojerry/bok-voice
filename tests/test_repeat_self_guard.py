"""出口复读防线：LLM 流出口逐句剥掉拟声复读句（2026-09-12 P0 兜底层）。

call-8fa17d2b 实证：两轮回复一字不差（#11=#13）、连续两轮重念整段通知
（#5/#7）——渐进披露治源头，这里钉死兜底：与上一句回复任一原句归一化
相似 ≥0.9 的句子不出声；客户要求重讲（REPEAT 轮）放行。
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
    _is_parrot_sentence,
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


def _run_llm(chunks: list[str], last_reply: str, *, repeat_requested: bool = False) -> str:
    async def _inner() -> str:
        ctx = ContextState(account_id="t")
        ctx.set_last_reply(last_reply)
        ctx.repeat_requested = repeat_requested
        wrapped = ContextAwareLLM(inner=_StreamInner(chunks), context_state=ctx)
        cc = llm.ChatContext()
        cc.add_message(role="user", content="你好")
        parts: list[str] = []
        async for ev in wrapped.chat(chat_ctx=cc):
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)
        return "".join(parts)

    return asyncio.run(_inner())


def test_is_parrot_sentence_identical():
    assert _is_parrot_sentence(
        "那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？",
        "好的，普哥，明白。 那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？ 麻烦把相关订单截图发我，我帮您核对。",
    )


def test_is_parrot_sentence_reworded_not_flagged():
    # 换措辞重问（正确行为）不能被误剥——相似度必须显著高才拦。
    assert not _is_parrot_sentence(
        "方便的话您看看最近一单还没到的是什么平台买的呢？",
        "好的，普哥，明白。 那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？ 麻烦把相关订单截图发我，我帮您核对。",
    )


def test_identical_repeat_sentence_suppressed():
    out = _run_llm(
        ["好的，普哥，明白。 那您最近有没有在拼多多、淘宝或京东下单？"],
        last_reply="好的，普哥，明白。 那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？",
    )
    assert "好的，普哥，明白" not in out
    assert out.strip() == ""


def test_mixed_reply_new_sentence_kept_parrot_dropped():
    out = _run_llm(
        ["我们核实到您是普哥本人。", "好的，普哥，明白。 那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？"],
        last_reply="好的，普哥，明白。 那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？",
    )
    assert "我们核实到您是普哥本人" in out
    assert "那您最近有没有在拼多多" not in out


def test_repeat_requested_bypass():
    # 客户要求重讲（REPEAT 轮）：照讲上一句是正确行为，放行。
    out = _run_llm(
        ["您这单是三天前寄出的，尾号七八九零，对吧？"],
        last_reply="您这单是三天前寄出的，尾号七八九零，对吧？",
        repeat_requested=True,
    )
    assert "尾号七八九零" in out


def test_cross_chunk_sentence_boundary():
    # 句子被流劈开也能判:整句到齐在句边界才比对。
    out = _run_llm(
        ["您这单是三天前寄出的，尾号七", "八九零，对吧？"],
        last_reply="您这单是三天前寄出的，尾号七八九零，对吧？",
    )
    assert out.strip() == ""
