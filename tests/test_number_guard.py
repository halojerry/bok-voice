"""编造号码输出守卫（2026-10-01，实弹 call-231aa92a）：纯函数矩阵 + 出口接线。

病灶：A 线粤语外呼第 5 步索取 WhatsApp 号码，客户未报任何号码时 9B 凭空输出
「收到，尾號係七七八八九九八，啱唔啱？」——编造数字做确认（合规级）。
不变量（Ethan 第一性原理定案）：确认句数字 run ∈ {捕获账本 captured, 本轮
客户原话转写 turn_user_text}；听错靠复述环路本身兜住，守卫绝不猜「正确」号码。
出口接线=LLM 流句级放行前（TTS 念的与 turns 账本同文本，原文单轨）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _part in ("packages/core", "apps/agent", "tools"):
    _p = str(ROOT / _part)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bok_voice_core.output_guard import (  # noqa: E402
    GUARD_SOLICIT_LINES,
    guard_fabricated_number,
    number_guard_pending,
)

CANTONESE = GUARD_SOLICIT_LINES["cantonese"]
ZH = GUARD_SOLICIT_LINES["zh"]
EN = GUARD_SOLICIT_LINES["en"]

FABRICATED_CANTO = "收到，尾號係七七八八九九八，啱唔啱？"


# ---- 纯函数：无捕获的编造确认 → 索取句（三语） ----


@pytest.mark.parametrize(
    "text,lang,expected",
    [
        (FABRICATED_CANTO, "cantonese", CANTONESE),
        ("收到，尾号是七七八八九九八，对吗？", "zh", ZH),
        ("Got it, your number is 7788998, right?", "en", EN),
        # 混合数字形态（中文数词+阿拉伯）同样识别。
        ("收到，尾號係七七八八998，啱唔啱？", "cantonese", CANTONESE),
        # lang 未知回落 zh（与 _call_language 缺省链同口径）。
        (FABRICATED_CANTO, "", ZH),
    ],
)
def test_fabricated_confirmation_without_capture_replaced(text, lang, expected):
    assert guard_fabricated_number(text, lang, None) == expected


def test_fabricated_confirmation_with_empty_capture_is_no_capture():
    # captured=""（拿不到号）等同无捕获——按编造处理，绝不放过确认。
    assert guard_fabricated_number(FABRICATED_CANTO, "cantonese", "") == CANTONESE


# ---- 纯函数：有捕获 + 正确复述（含尾号子串形态）→ 原样 ----


@pytest.mark.parametrize(
    "text,captured",
    [
        ("收到，尾號係七八九零，啱唔啱？", "7890"),  # 完整复述
        ("收到，尾號係 7890，啱唔啱？", "7890"),  # 阿拉伯形态
        ("你嘅尾號係八九九八，啱唔啱？", "7788998"),  # 尾号 4 位子串
        ("你嘅尾號係七七八八九九八，啱唔啱？", "7788998"),  # 完整复述（中文）
        ("你嘅尾號係七八八九九八，啱唔啱？", "7788998"),  # 缺首位的 6 位子串
    ],
)
def test_captured_correct_repeat_passes_through(text, captured):
    assert guard_fabricated_number(text, "cantonese", captured) == text


# ---- 纯函数：有捕获 + 错误数字 → 改正为捕获号码（中文数字逐位） ----


def test_captured_mismatch_corrected_to_capture():
    out = guard_fabricated_number(FABRICATED_CANTO, "cantonese", "7890")
    assert out == "收到，尾號係七八九零，啱唔啱？"


def test_captured_mismatch_arabic_run_corrected():
    # 原位替换 run（两侧标点/空白原样保留）。
    out = guard_fabricated_number("收到，尾号是 7788998，对吗？", "zh", "6432543")
    assert out == "收到，尾号是 六四三二五四三，对吗？"


def test_captured_mismatch_but_transcript_matches_passes():
    # 改口轮：捕获账本=旧值（滞后），本轮转写含新号 → 放行不误改回旧值。
    out = guard_fabricated_number(
        "收到，尾號係六四三二五四三，啱唔啱？",
        "cantonese",
        "7890",
        turn_user_text="係咁嘅，我轉咗號碼，六四三二五四三",
    )
    assert out == "收到，尾號係六四三二五四三，啱唔啱？"


def test_no_capture_but_transcript_matches_passes():
    out = guard_fabricated_number(
        "收到，你的号码是 6432543，对吗？",
        "zh",
        None,
        turn_user_text="我的WhatsApp号码是6432543",
    )
    assert out == "收到，你的号码是 6432543，对吗？"


def test_transcript_grouped_number_passes():
    # 转写带分组分隔符（7788-998）=同一个号；守卫侧折叠后比对。
    out = guard_fabricated_number(
        "你嘅號碼係七八八九九八，啱唔啱？",
        "cantonese",
        None,
        turn_user_text="我嘅號碼係 7788-998",
    )
    assert out == "你嘅號碼係七八八九九八，啱唔啱？"


def test_neither_source_contains_number_rewritten_to_capture():
    # 转写与捕获都不含该数字 → 编造；有捕获 → 改正为捕获（不是索取句）。
    out = guard_fabricated_number(FABRICATED_CANTO, "cantonese", "7890", "唔係呀")
    assert out == "收到，尾號係七八九零，啱唔啱？"


# ---- 纯函数：非确认语境数字绝不动（规则 4） ----


@pytest.mark.parametrize(
    "text,captured",
    [
        ("我哋最低賠償 300 蚊，三至五個工作天到帳。", None),
        ("賠償金額係 3000 蚊。", None),
        ("我哋最低賠償 300 蚊。", "7890"),
        ("三至五個工作天就會到帳。", "7890"),
        # 语境词在场但 run 在别的分句（金额）——不跨分句认确认。
        ("我幫你查咗單號，賠償金額係 3000 蚊。", None),
        ("我幫你查咗單號，賠償金額係 3000 蚊。", "7890"),
        ("你嘅貨三日內到，到時再聯絡你。", "7890"),
    ],
)
def test_non_confirmation_numbers_untouched(text, captured):
    assert guard_fabricated_number(text, "zh", captured) == text


def test_context_word_without_digit_run_untouched():
    text = "你嘅 WhatsApp 號碼我記低咗，陣間再確認。"
    assert guard_fabricated_number(text, "cantonese", None) == text


def test_digit_run_without_context_word_untouched():
    text = "我幫你 check 咗 7788998 嘅物流進度。"
    assert guard_fabricated_number(text, "cantonese", "7890") == text


# ---- 纯函数：混合句只动含确认的句子 ----


def test_mixed_sentence_only_confirmation_replaced():
    text = "唔好意思，我幫你查一查。" + FABRICATED_CANTO
    out = guard_fabricated_number(text, "cantonese", None)
    assert out == "唔好意思，我幫你查一查。" + CANTONESE


def test_mixed_sentence_only_wrong_run_corrected():
    text = "你嘅件聽日到。" + FABRICATED_CANTO
    out = guard_fabricated_number(text, "cantonese", "7890")
    assert out == "你嘅件聽日到。收到，尾號係七八九零，啱唔啱？"


def test_guard_is_idempotent():
    once = guard_fabricated_number(FABRICATED_CANTO, "cantonese", None)
    assert guard_fabricated_number(once, "cantonese", None) == once
    twice = guard_fabricated_number(FABRICATED_CANTO, "cantonese", "7890")
    assert guard_fabricated_number(twice, "cantonese", "7890") == twice


def test_empty_and_none_inputs_identity():
    assert guard_fabricated_number("", "zh", None) == ""
    assert guard_fabricated_number(None, "zh", None) == ""  # type: ignore[arg-type]


# ---- 纯函数：早发扣留判据 ----


@pytest.mark.parametrize(
    "text,expected",
    [
        ("收到，尾號", True),
        ("收到，我睇尾", True),  # 语境词残件（单字前缀）也要扣住
        ("話咁快就 7", True),
        ("你好，請問", False),
        ("WhatsApp", True),
    ],
)
def test_number_guard_pending(text, expected):
    assert number_guard_pending(text) is expected


# ---- 出口接线（行为面）：LLM 流句级放行前守卫 ----


def _make_fakes():
    from livekit.agents import llm

    class _FakeInner(llm.LLMStream):
        def __init__(self, plugin, chunks_):
            from livekit.agents.types import APIConnectOptions

            super().__init__(
                llm=plugin,
                chat_ctx=llm.ChatContext(),
                tools=[],
                conn_options=APIConnectOptions(),
            )
            self._chunks = chunks_

        async def _metrics_monitor_task(self, event_aiter):
            async for _ in event_aiter:
                pass

        async def _run(self):
            for i, c in enumerate(self._chunks):
                self._event_ch.send_nowait(
                    llm.ChatChunk(id=str(i), delta=llm.ChoiceDelta(content=c, role="assistant"))
                )

    class _Inner(llm.LLM):
        def __init__(self, chunks_):
            super().__init__()
            self._chunks = chunks_

        def chat(self, *, chat_ctx, **kw):
            return _FakeInner(self, self._chunks)

    return llm, _FakeInner, _Inner


def _run_llm(
    chunks: list[str],
    *,
    captured: str = "",
    turn_text: str = "",
    lang: str = "cantonese",
    last_reply: str = "",
) -> str:
    from agent_runtime.providers.livekit_plugins import ContextAwareLLM, ContextState

    llm, _fake, _Inner = _make_fakes()

    async def _inner() -> str:
        ctx = ContextState(account_id="t")
        ctx.set_user_language(lang)
        if captured:
            ctx.set_whatsapp_note(captured)
        ctx.set_turn_user_text(turn_text)
        if last_reply:
            ctx.set_last_reply(last_reply)
        wrapped = ContextAwareLLM(inner=_Inner(chunks), context_state=ctx)
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


def test_stream_turn_text_read_lazily():
    # 抢跑时序：流构造时本轮原话还没写完（空），消费前才写入——守卫必须拿
    # 最新值（惰性取），否则会把客户真报过的号码误判成编造。
    from agent_runtime.providers.livekit_plugins import _RepeatSelfGuardStream

    llm, _fake, _Inner = _make_fakes()
    state = {"turn": ""}

    async def _inner() -> str:
        plugin = _Inner(["收到，尾號係六四三二五四三，啱唔啱？"])
        inner_stream = plugin.chat(chat_ctx=llm.ChatContext())
        guard = _RepeatSelfGuardStream(
            plugin,
            inner_stream,
            "",
            number_on=True,
            number_lang="cantonese",
            number_captured=None,
            number_turn_text=lambda: state["turn"],
        )
        # 流已构造（快照期）→ 钩子此刻才写完本轮原话。
        state["turn"] = "我嘅號碼係六四三二五四三"
        parts: list[str] = []
        async for ev in guard:
            delta = getattr(ev, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)
        return "".join(parts)

    out = asyncio.run(_inner())
    assert out == "收到，尾號係六四三二五四三，啱唔啱？"


def test_stream_replaces_fabricated_confirmation():
    out = _run_llm([FABRICATED_CANTO])
    assert out == CANTONESE
    assert "七七八八九九八" not in out


def test_stream_replaces_fabricated_confirmation_zh():
    out = _run_llm(["收到，尾号是七七八八九九八，对吗？"], lang="zh")
    assert out == ZH


def test_stream_keeps_correct_capture_confirmation():
    out = _run_llm(["收到，尾號係七八九零，啱唔啱？"], captured="7890")
    assert out == "收到，尾號係七八九零，啱唔啱？"


def test_stream_corrects_wrong_number_with_capture():
    out = _run_llm([FABRICATED_CANTO], captured="7890")
    assert out == "收到，尾號係七八九零，啱唔啱？"


def test_stream_hold_across_chunks_no_partial_leak():
    # 首段早发让位到句界：半截编造号码绝不提前出声。
    out = _run_llm(["收到，尾號係七七八", "八九九八，啱唔啱？"])
    assert out == CANTONESE
    assert "七七八" not in out


def test_stream_change_of_mind_turn_passes():
    out = _run_llm(
        ["收到，尾號係六四三二五四三，啱唔啱？"],
        captured="7890",
        turn_text="我轉咗號碼，六四三二五四三",
    )
    assert out == "收到，尾號係六四三二五四三，啱唔啱？"


def test_stream_kill_switch_off_identity(monkeypatch):
    monkeypatch.setenv("BOK_NUMBER_GUARD", "0")
    out = _run_llm([FABRICATED_CANTO])
    assert out == FABRICATED_CANTO


def test_stream_guard_independent_of_repeat_guard(monkeypatch):
    # 复读闸关（en 0）≠ 号码闸关：两条防线互相独立。
    monkeypatch.setenv("BOK_REPEAT_GUARD", "0")
    out = _run_llm([FABRICATED_CANTO])
    assert out == CANTONESE


# ---- 出口接线（源级 pin）：删接线/删 env 登记即红 ----


def test_source_pins_plugin_calls_guard():
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(
        encoding="utf-8"
    )
    assert (
        "from bok_voice_core.output_guard import guard_fabricated_number, number_guard_pending"
        in src
    )
    assert 'os.environ.get("BOK_NUMBER_GUARD", "1")' in src
    # 守卫必须在句级放行前（_feed 的 emit 处）且流末余段同样过——两条调用缺一即红。
    assert "sentence = guard_fabricated_number(" in src
    assert "rest = guard_fabricated_number(" in src
    # 早发扣留（半截号码不外泄）挂在首段早发两处判据上。
    assert "_number_hold()" in src
    assert "number_guard_pending(self._buf)" in src


def test_source_pins_agent_writes_turn_user_text():
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert "context_state.set_turn_user_text(user_text)" in src


def test_source_pin_forward_env_registered():
    import bok  # noqa: E402  (tools/bok.py：_FORWARD_ENV 立法门禁源)

    assert "BOK_NUMBER_GUARD" in bok.env._FORWARD_ENV


# ------------------------------------------------- 合法源之三:对象档案(2026-10-01 补)


def test_object_brief_tracking_number_readback_passes():
    """AI 念读对象档案里的快递单号做确认=合法确认环(客户可当场纠正),放行。"""
    t = guard_fabricated_number(
        "你張快遞單號係七八六五四三二一，啱唔啱？",
        "cantonese",
        None,
        turn_user_text=None,
        known_text="快遞單號 78654321 到倉 2026-09-30",
    )
    assert t == "你張快遞單號係七八六五四三二一，啱唔啱？"


def test_object_brief_absent_still_fabrication():
    """对象档案不含该单号(known_text 缺席/无此数字)→ 仍按编造处理。"""
    t = guard_fabricated_number(
        "你張快遞單號係七八六五四三二一，啱唔啱？",
        "cantonese",
        None,
        turn_user_text=None,
        known_text=None,
    )
    assert t == "你報個常用嘅 WhatsApp 號碼俾我，我再同你確認一次。"


def test_object_brief_mismatch_run_corrected_to_capture_when_captured():
    """known 不匹配且已捕获 WhatsApp → 违约 run 改正为捕获值(WA 语境主路径不变)。"""
    t = guard_fabricated_number(
        "收到，你嘅WhatsApp號碼係九九八八七七六六，啱唔啱？",
        "cantonese",
        "12234456",
        turn_user_text=None,
        known_text="快遞單號 78654321",
    )
    assert "一二二三四四五六" in t
    assert "九九八八七七六六" not in t
