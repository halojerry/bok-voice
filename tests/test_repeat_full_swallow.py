"""FIX-3(D2-4,2026-10-01):复读防线全吞上抛——「4B 复读」不再记成「AI 死了」。

病灶:_RepeatSelfGuardStream 全吞收尾(REPEAT_CROSS_TURN_EMPTY/整句全剥路径)
无 assistant item → relieve/reask 归零不发生、_assistant_out 不置位 → starve
计数+1、6s 响应看门狗强断念道歉——账本归因污染。

修(最小手术,不引入新出声行为):guard 收尾把被吞全文经 on_full_swallow 上抛;
agent 侧接住=①立刻拆响应看门狗(响应发生过,静默是刻意决定)②turns 落一行
provider=repeat-suppressed/gen=llm/文本=被吞内容 ③REPEAT_SUPPRESSED 标记日志。
不改 _assistant_out/starve(客户耳中确是静默,starve-ack 是正确可闻兜底),
不抵销 stall(复读是坏输出不是好答案)。

本文件:guard 层行为(全吞回调/部分剥除不上抛/回调异常不破流)+ agent 接线源级 pin。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from livekit.agents import llm  # noqa: E402

from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    ContextAwareLLM,
    ContextState,
)

# 与 test_repeat_self_guard.py 同款样本(复读判定已在该文件钉死)。
REPEAT = "您这单是三天前寄出的，尾号七八九零，对吧？"
LAST2 = "好的，普哥，明白。 那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？"


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


_UNSET = object()


def _run_llm(chunks, *, last_reply=REPEAT, ledger_texts=(), cb=_UNSET):
    """跑一轮带全吞回调的包装链,返回 (输出文本, 回调收到文本列表)。

    cb=_UNSET=默认收集回调;cb=None=不注入(旧装配行为);cb=callable=直用。
    """
    calls: list[str] = []

    async def _inner():
        ctx = ContextState(account_id="t")
        ctx.set_last_reply(last_reply)
        for t in ledger_texts:
            ctx.record_reply(t, "llm")
        wrapped = ContextAwareLLM(inner=_StreamInner(chunks), context_state=ctx)
        if cb is _UNSET:

            async def _collect(text: str) -> None:
                calls.append(text)

            wrapped.set_full_swallow_cb(_collect)
        elif cb is not None:
            wrapped.set_full_swallow_cb(cb)
        cc = llm.ChatContext()
        cc.add_message(role="user", content="你好")
        parts: list[str] = []
        async for ev in wrapped.chat(chat_ctx=cc):
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)
        return "".join(parts)

    out = asyncio.run(_inner())
    return out, calls


# ---- guard 层行为 ----


def test_full_swallow_fires_callback_with_swallowed_text():
    # 整条回复=上一句复读 → 全吞:零输出 + 回调收到被吞全文(证据保全)
    out, calls = _run_llm([REPEAT])
    assert out.strip() == ""
    assert calls == [REPEAT]


def test_multi_sentence_full_swallow_concatenates_evidence():
    # 多句全吞 → 回调一次、文本=被吞句拼接(逐句形态保留,可供账本区分)
    out, calls = _run_llm(["好的，普哥，明白。", "那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？"], last_reply=LAST2)
    assert out.strip() == ""
    assert len(calls) == 1
    assert "好的，普哥，明白。" in calls[0]
    assert "那您最近有没有在拼多多" in calls[0]


def test_partial_swallow_does_not_fire_callback():
    # 部分剥除(新内容照发,复读句吞掉)=正常说话,不触发回调
    out, calls = _run_llm(["我们核实到您是普哥本人。", REPEAT])
    assert "我们核实到您是普哥本人" in out
    assert "尾号七八九零" not in out  # 复读句被剥
    assert calls == []


def test_cross_turn_full_swallow_fires_callback():
    # 跨轮账本路径（last_reply 空但账本在场,bypass 被 cross_on 接住）同样全吞上抛
    text = "那您最近有没有在拼多多、淘宝或京东下单，但还没收到货？"
    out, calls = _run_llm([text], last_reply="", ledger_texts=(text,))
    assert out.strip() == ""
    assert calls == [text]


def test_callback_exception_does_not_break_stream():
    # 回调异常绝不被流收尾(证据记账尽力而为;旧行为=全吞静默收尾照旧)
    async def _boom(text: str) -> None:
        raise RuntimeError("boom")

    out, calls = _run_llm([REPEAT], cb=_boom)
    assert out.strip() == ""
    assert calls == []


def test_no_callback_configured_is_old_behavior():
    # 未注入回调(旧装配/B 线)=全吞静默收尾,零异常(逐字节旧行为)
    out, calls = _run_llm([REPEAT], cb=None)
    assert out.strip() == ""
    assert calls == []


# ---- agent 接线源级 pin ----


def _agent_src() -> str:
    return (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


def test_agent_callback_body_pinned():
    src = _agent_src()
    idx = src.index("async def _repeat_full_swallow(text: str) -> None:")
    seg = src[idx : idx + 2000]
    # ①拆响应看门狗 ②放行 W-GATE ③turns 行 provider/gen 标定 ④标记日志
    assert "_cancel_response_watchdog()" in seg
    assert "_reply_done_event.set()" in seg
    assert 'provider="repeat-suppressed", gen="llm"' in seg
    assert "REPEAT_SUPPRESSED full_swallow chars=" in seg
    # (c) 不改 _assistant_out/starve、不抵销 stall(代码形态;docstring 提及不算)
    assert '_assistant_out["on"]' not in seg
    assert "flow_ctrl.relieve_stall_streak(" not in seg
    assert "_starve[" not in seg


def test_agent_wiring_set_full_swallow_cb_pinned():
    src = _agent_src()
    assert '_set_fscb = getattr(llm_provider, "set_full_swallow_cb", None)' in src
    assert "_set_fscb(_repeat_full_swallow)" in src


def test_guard_callback_source_pinned():
    psrc = (
        ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
    ).read_text(encoding="utf-8")
    # 全吞判定=一句未放出且有剥除;三处剥除点全部记账
    assert "if not self._emitted and self._swallowed and self._on_full_swallow is not None:" in psrc
    assert "self._swallowed.append(sentence)" in psrc
    assert "self._swallowed.append(rest)" in psrc
    # 构造接线:ContextAwareLLM 把注入回调传给 guard
    assert "on_full_swallow=self._full_swallow_cb" in psrc
    assert "def set_full_swallow_cb(self, cb) -> None:" in psrc
    # 回调异常不破流收尾
    assert "except Exception as exc:  # noqa: BLE001 - 回调异常不破流收尾" in psrc
