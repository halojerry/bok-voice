"""跨轮复读防线（EX-2，2026-09-28，「已读乱回」）。

病灶：出口复读防线只比对上一条回复，同一通内隔轮复述（客户答非所问时 AI 把
两轮前的原文再讲一遍）拦不住。EX-2 让 ContextState 记最近 3 条 AI 回复
（gen=llm 才参与比对），`_RepeatSelfGuardStream` 逐句比对跨轮账本 + 整段滚动
相似（阈值可配 BOK_REPEAT_CROSS_TURN_SIM）。

复问放行（防误剥）：客户复述/追问上一问、或 verdict==REPEAT 时（allow_repeat），
模型复讲关键内容是正确行为——整段放行并打 REPEAT_CROSS_TURN_SKIPPED reason=reask。

脚本直念/罐头车道结构性不在 LLM 流内（session.say / 罐头音频），零影响。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import pytest  # noqa: E402
from livekit.agents import llm  # noqa: E402

from agent_runtime.agent import _reply_similarity  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    ContextAwareLLM,
    ContextState,
    _RepeatSelfGuardStream,
    _repeat_cross_turn_sim,
)

ROOT = Path(__file__).resolve().parents[1]
PLUGINS_SRC = (
    ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
).read_text(encoding="utf-8")
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")

# 目标漂移参照:归一相似 0.8846(单句比对 <0.9 不触发自我防线,整段滚动阈值决定)
_ENTRY = "您这单是三天前寄出的，尾号七八九零，我帮您核对一下物流进度。"
_NEAR = "您这单是三天前发的，尾号七八九零，我帮您查一下物流进度。"


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


def _run(
    chunks: list[str],
    *,
    last_reply: str = "",
    ledger: list[str] | None = None,
    allow_repeat: bool = False,
    repeat_requested: bool = False,
) -> str:
    async def _inner() -> str:
        ctx = ContextState(account_id="t")
        if last_reply:
            ctx.set_last_reply(last_reply)
        for t in ledger or []:
            ctx.record_reply(t, "llm")
        ctx.set_allow_repeat(allow_repeat)
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


# ---- 账本写/读 ----

def test_ledger_record_read_and_bound():
    ctx = ContextState(account_id="t")
    ctx.record_reply("第一条 LLM 回复内容足够长", "llm")
    ctx.record_reply("脚本直念不进比对面", "script")
    ctx.record_reply("第二条 LLM 回复内容足够长", "llm")
    assert ctx.reply_ledger() == ["第一条 LLM 回复内容足够长", "第二条 LLM 回复内容足够长"]
    ctx.record_reply("第三条 LLM", "llm")
    ctx.record_reply("第四条 LLM", "llm")
    # 有界 last 3:script 已被挤出,只剩 llm 三条
    assert ctx.reply_ledger() == ["第二条 LLM 回复内容足够长", "第三条 LLM", "第四条 LLM"]
    assert all(g == "llm" for _, g in ctx._reply_ledger)


def test_ledger_ignores_empty():
    ctx = ContextState(account_id="t")
    ctx.record_reply("", "llm")
    ctx.record_reply("   ", "llm")
    assert ctx.reply_ledger() == []


def test_set_allow_repeat_flag():
    ctx = ContextState(account_id="t")
    assert ctx.allow_repeat is False
    ctx.set_allow_repeat(True)
    assert ctx.allow_repeat is True


# ---- 复读剥除 / 复问放行 ----

def test_cross_turn_repeat_stripped(monkeypatch):
    # last_reply 空 → 自我防线旁路;跨轮账本命中同句 → 整条剥空。
    monkeypatch.delenv("BOK_REPEAT_CROSS_TURN", raising=False)
    out = _run(
        ["您这单是三天前寄出的，尾号七八九零，我帮您核对一下物流进度。"],
        last_reply="",
        ledger=[_ENTRY],
    )
    assert out.strip() == ""


def test_cross_turn_new_content_kept(monkeypatch):
    monkeypatch.delenv("BOK_REPEAT_CROSS_TURN", raising=False)
    out = _run(
        ["我们核实到您是普哥本人，现在帮您登记。"],
        last_reply="",
        ledger=[_ENTRY],
    )
    assert "我们核实到您是普哥本人" in out


def test_reask_allowed(monkeypatch):
    # 复问放行(allow_repeat):账本命中亦整段播出。
    monkeypatch.delenv("BOK_REPEAT_CROSS_TURN", raising=False)
    out = _run(
        ["您这单是三天前寄出的，尾号七八九零，我帮您核对一下物流进度。"],
        last_reply="",
        ledger=[_ENTRY],
        allow_repeat=True,
    )
    assert "尾号七八九零" in out


def test_reask_similarity_gate(monkeypatch):
    # re-ask gate 纯函数:客户复述/追问上一问 → 高相似(≥0.8)。
    assert _reply_similarity(
        "那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？",
        "那您最近有没有在拼多多、淘宝或者京东下单，但还没收到货？",
    ) >= 0.8
    assert _reply_similarity("帮我查一下物流", "好的我马上安排专员") < 0.8


# ---- env 闸 / 阈值 ----

def test_env_off_no_stripping(monkeypatch):
    monkeypatch.setenv("BOK_REPEAT_CROSS_TURN", "0")
    out = _run(
        ["您这单是三天前寄出的，尾号七八九零，我帮您核对一下物流进度。"],
        last_reply="",
        ledger=[_ENTRY],
    )
    assert "尾号七八九零" in out  # 闸关=账本不参与,零剥除


def test_threshold_env_honored(monkeypatch):
    monkeypatch.delenv("BOK_REPEAT_CROSS_TURN", raising=False)
    # 默认 0.85:整段滚动相似 0.8846 ≥ 0.85 → 剥
    monkeypatch.delenv("BOK_REPEAT_CROSS_TURN_SIM", raising=False)
    assert _repeat_cross_turn_sim() == 0.85
    out_lo = _run([_NEAR], last_reply="", ledger=[_ENTRY])
    assert out_lo.strip() == ""
    # 阈值抬到 0.95:0.8846 < 0.95 且单句比对 <0.9 → 不剥
    monkeypatch.setenv("BOK_REPEAT_CROSS_TURN_SIM", "0.95")
    assert _repeat_cross_turn_sim() == 0.95
    out_hi = _run([_NEAR], last_reply="", ledger=[_ENTRY])
    assert "尾号七八九零" in out_hi


# ---- 结构 pin:防线只包 LLM 流 ----

def test_guard_only_wraps_llm_stream():
    # 唯一构造点在 ContextAwareLLM.chat(LLM 流出口);脚本/罐头车道零引用。
    assert PLUGINS_SRC.count("= _RepeatSelfGuardStream(") == 1  # 仅构造点(类定义不计)
    i_chat = PLUGINS_SRC.index("class ContextAwareLLM")
    i_build = PLUGINS_SRC.index("= _RepeatSelfGuardStream(")
    i_next_cls = PLUGINS_SRC.index("class _PartialCaptureStream")
    assert i_chat < i_build < i_next_cls  # 构造落在 ContextAwareLLM 体内
    # agent.py 的脚本车道 chokepoint 不构造防线(结构性绕开=不受影响)
    i_reg = AGENT_SRC.index("def _register_reply_lane(")
    i_reg_end = AGENT_SRC.index("def _consume_reply_ticket(", i_reg)
    assert "_RepeatSelfGuardStream" not in AGENT_SRC[i_reg:i_reg_end]


def test_guard_ledger_and_reask_params_wired():
    # 构造点确实喂了账本/阈值/复问(源级 pin:删参数即红)
    assert "ledger=_cross_ledger" in PLUGINS_SRC
    assert "threshold=_repeat_cross_turn_sim()" in PLUGINS_SRC
    assert "allow_repeat=self._ctx.allow_repeat" in PLUGINS_SRC
    assert "REPEAT_CROSS_TURN_SKIPPED reason=reask" in PLUGINS_SRC
    assert "REPEAT_CROSS_TURN_SUPPRESSED" in PLUGINS_SRC
    assert "REPEAT_CROSS_TURN_EMPTY" in PLUGINS_SRC
