"""B 线同传 glossary 装配 + manual 自驱管线 turn_handling 配对单测（2026-09-16 P0/P2）。

四块内容（无网络、不起 worker）：
1. 术语表纯函数链：parse_glossary → glossary_source_terms（ASR 热词侧）/
   glossary_block（MT prompt 术语槽，400 字护栏）；
2. 落点接线：asr_hotword_context(include_industry=False)（B 线不吃 A 线快递
   行业词）、_mt_prompt 术语槽（空串逐字节同旧模板）、_build_llm_provider 把
   glossary 传进 StatelessMTLLM、_translation_instructions 回退路径术语行；
3. turn_handling 配对（P2 manual 自驱管线）：默认 turn_detection=manual +
   endpointing 0.25/0.6（A 线句级提交校准档）；TURN_DETECTION kill-switch 族 →
   不带 turn_detection key + min_delay 自动回 ≥0.35 地板；打断恒关。
4. _build_mt_context / _mt_once：manual 模式下单句直调翻译 LLM 的上下文形状
   （instructions + 滚动「源→译」对 + 当前句）与流式收集。

已播出译文不被推翻（承诺制）由结构保证：句级提交后 committed 前缀永不重发
（停嘴 finish 只发 _uncommitted 尾巴、尾巴空则不发 FINAL）——行为钉死在
test_sentence_commit.py::test_vad_stop_tail_end_to_end /
test_uncommitted_redecode_correction_dropped_and_continuation_kept。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import agent_runtime.interpret as interpret  # noqa: E402
from agent_runtime.agent import asr_hotword_context  # noqa: E402
from agent_runtime.providers import livekit_plugins as lp  # noqa: E402


# ---- 1. 术语表纯函数链 ----


def test_parse_glossary_pairs_plain_and_separators():
    pairs = interpret.parse_glossary("顺丰=SF Express；拼多多=Pinduoduo\n林总")
    assert pairs == (("顺丰", "SF Express"), ("拼多多", "Pinduoduo"), ("林总", ""))


def test_parse_glossary_empty_and_malformed_tolerant():
    assert interpret.parse_glossary("") == ()
    assert interpret.parse_glossary(None) == ()  # type: ignore[arg-type]
    assert interpret.parse_glossary("，，；\n") == ()
    # 空=右侧/空=左侧:左侧空丢弃,右侧空=纯词条语义
    assert interpret.parse_glossary("=译名") == ()
    assert interpret.parse_glossary("专名=") == (("专名", ""),)


def test_glossary_source_terms_joins_src_side():
    pairs = interpret.parse_glossary("顺丰=SF, 12345678=号码, 京东=JD")
    assert interpret.glossary_source_terms(pairs) == "顺丰,12345678,京东"


def test_glossary_block_renders_and_caps():
    pairs = interpret.parse_glossary("顺丰=SF Express；拼多多")
    assert interpret.glossary_block(pairs) == "顺丰=SF Express；拼多多"
    # 护栏:超长从尾部丢弃,不截半条
    long_pairs = tuple((f"词{i}", "x" * 50) for i in range(30))
    block = interpret.glossary_block(long_pairs)
    assert len(block) <= interpret._GLOSSARY_MAX_CHARS + len("x" * 50)
    assert block.count("=") >= 1
    assert interpret.glossary_block(()) == ""


# ---- 2. 落点接线 ----


def test_asr_hotword_context_no_industry_words_for_bline(monkeypatch):
    monkeypatch.delenv("BOK_ASR_HOTWORDS", raising=False)
    ctx = asr_hotword_context("cantonese", None, extra_hotwords="順豐速運, 京東物流", include_industry=False)
    assert ctx == "Vocabulary: 順豐速運, 京東物流"
    # include_industry=True(缺省,A 线)才带行业静态词——B 线不吃
    ctx_industry = asr_hotword_context("cantonese", None, extra_hotwords="順豐速運")
    assert "單號" in ctx_industry
    # 数字主导词条丢弃(biasing 幻听数字风险)
    ctx_digit = asr_hotword_context("zh", None, extra_hotwords="64325432, 順豐", include_industry=False)
    assert "64325432" not in ctx_digit and "順豐" in ctx_digit
    # kill-switch:BOK_ASR_HOTWORDS=0 → 不下发
    monkeypatch.setenv("BOK_ASR_HOTWORDS", "0")
    assert asr_hotword_context("zh", None, extra_hotwords="順豐", include_industry=False) == ""


def test_mt_prompt_glossary_slot_and_byte_identity():
    old = "将以下文本翻译为 `英语`，注意只需要输出翻译后的结果，不要额外解释：\n\n`hello`"
    assert lp._mt_prompt("hello", "en") == old  # 空 glossary 逐字节同旧
    with_g = lp._mt_prompt("hello", "en", "顺丰=SF Express")
    assert with_g.startswith("术语表（保持一致）：顺丰=SF Express\n\n")
    assert with_g.endswith(old)


def test_build_llm_provider_passes_glossary(monkeypatch):
    from agent_runtime.providers.livekit_plugins import StatelessMTLLM

    monkeypatch.setenv("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
    provider = interpret._build_llm_provider({}, "en", glossary="顺丰=SF Express")
    assert isinstance(provider, StatelessMTLLM)
    assert provider._glossary == "顺丰=SF Express"
    # 缺省(不传)=空术语槽,行为同旧
    assert interpret._build_llm_provider({}, "en")._glossary == ""


def test_translation_instructions_glossary_line():
    base = interpret._translation_instructions("zh", "en")
    assert "Glossary" not in base
    with_g = interpret._translation_instructions("zh", "en", glossary="顺丰=SF Express")
    assert "- Glossary (keep these renderings exactly): 顺丰=SF Express" in with_g


# ---- 3. turn_handling 配对（P2 manual 自驱管线）----


def test_turn_handling_default_manual_pipeline(monkeypatch):
    monkeypatch.delenv("TURN_DETECTION", raising=False)
    monkeypatch.delenv("ENDPOINT_MIN_DELAY", raising=False)
    monkeypatch.delenv("ENDPOINT_MAX_DELAY", raising=False)
    monkeypatch.delenv("INTERRUPT_MIN_DURATION", raising=False)
    monkeypatch.delenv("RESUME_FALSE_INTERRUPTION", raising=False)
    monkeypatch.delenv("FALSE_INTERRUPTION_TIMEOUT", raising=False)
    monkeypatch.delenv("PREEMPTIVE_MAX_RETRIES", raising=False)

    opts = interpret._turn_handling_opts()
    assert opts["turn_detection"] == "manual"  # P2:框架不自动回复,MT→say 自驱管线
    assert opts["endpointing"] == {"mode": "dynamic", "min_delay": 0.25, "max_delay": 0.6}
    # 打断恒关:manual 下没有自动回复可打断,此开关只余「音频活动打断译文」
    # 一条路——那是 P0 实证过会吞译文的危害路径
    assert opts["interruption"]["enabled"] is False
    monkeypatch.setenv("BOK_INTERP_INTERRUPT", "1")
    opts_on = interpret._turn_handling_opts()
    assert opts_on["interruption"]["enabled"] is False  # B 线恒关,env 不再是逃生门
    monkeypatch.delenv("BOK_INTERP_INTERRUPT", raising=False)
    # 抢跑预算(P1 churn 收敛档)
    assert opts["preemptive_generation"]["max_retries"] == 3
    assert opts["preemptive_generation"]["preemptive_tts"] is False


def test_turn_handling_kill_switch_paired(monkeypatch):
    """TURN_DETECTION≠stt（kill-switch 族）→ 不带 turn_detection key（框架回
    EOT 自动回复旧档）+ endpointing min_delay 自动回 ≥0.35 地板——插件句级
    FINAL 同源熄火（test_sentence_commit::sentence_commit_enabled 同判），
    manual 管线与句级 FINAL 同进同退,不存在错配组合。"""
    monkeypatch.setenv("TURN_DETECTION", "")
    monkeypatch.delenv("ENDPOINT_MIN_DELAY", raising=False)
    monkeypatch.delenv("ENDPOINT_MAX_DELAY", raising=False)
    opts = interpret._turn_handling_opts()
    assert "turn_detection" not in opts
    assert opts["endpointing"]["min_delay"] == 0.35

    # vad 档:同属 kill-switch 族 → 回 EOT 自动回复旧档(manual 只配 stt)
    monkeypatch.setenv("TURN_DETECTION", "vad")
    opts_vad = interpret._turn_handling_opts()
    assert "turn_detection" not in opts_vad
    assert opts_vad["endpointing"]["min_delay"] == 0.35


def test_mt_prompt_glossary_via_stateless_wrapper():
    """StatelessMTLLM chat 组装的 user 内容带术语槽(用 fake 内芯截获)。"""

    class _Inner(lp.llm.LLM):
        def __init__(self):
            super().__init__()
            self.captured = None

        def chat(self, *, chat_ctx, **kw):
            self.captured = chat_ctx
            raise RuntimeError("stop")

    inner = _Inner()
    wrapper = lp.StatelessMTLLM(inner, "en", glossary="顺丰=SF Express")

    import contextlib

    ctx = lp.llm.ChatContext()
    ctx.add_message(role="user", content="hello")
    with contextlib.suppress(RuntimeError):
        wrapper.chat(chat_ctx=ctx)

    texts = [
        str(getattr(item, "text_content", "") or "")
        for item in inner.captured.items
        if getattr(item, "role", None) == "user"
    ]
    assert texts == [lp._mt_prompt("hello", "en", "顺丰=SF Express")]
    assert texts[0].startswith("术语表（保持一致）：顺丰=SF Express\n\n")


# ---- P1-B MT 滚动上下文(BOK_INTERP_MT_CONTEXT,默认 0=关) ----


def test_rolling_pairs_extraction():
    class _Inner(lp.llm.LLM):
        def chat(self, *, chat_ctx, **kw):  # pragma: no cover - 只为构造存在
            raise RuntimeError("stop")

    wrapper = lp.StatelessMTLLM(_Inner(), "en", context_turns=2)
    ctx = lp.llm.ChatContext()
    ctx.add_message(role="user", content="我的包裹三天了还没到。")
    ctx.add_message(role="assistant", content="My parcel hasn't arrived for three days.")
    ctx.add_message(role="user", content="你们是不是发错地方了。")
    ctx.add_message(role="assistant", content="Did you send it to the wrong place?")
    ctx.add_message(role="user", content="它什么时候能到？")
    pairs = wrapper._rolling_pairs(ctx)
    # 旧→新序,只取最近 2 对,当前句(它什么时候能到？)不在内
    assert pairs == [
        ("我的包裹三天了还没到。", "My parcel hasn't arrived for three days."),
        ("你们是不是发错地方了。", "Did you send it to the wrong place?"),
    ]


def test_rolling_pairs_off_or_empty_ctx():
    class _Inner(lp.llm.LLM):
        def chat(self, *, chat_ctx, **kw):  # pragma: no cover
            raise RuntimeError("stop")

    off = lp.StatelessMTLLM(_Inner(), "en", context_turns=0)
    ctx = lp.llm.ChatContext()
    ctx.add_message(role="user", content="hello")
    assert off._rolling_pairs(ctx) == []  # 关=恒空

    on = lp.StatelessMTLLM(_Inner(), "en", context_turns=2)
    assert on._rolling_pairs(lp.llm.ChatContext()) == []  # 空 ctx
    solo = lp.llm.ChatContext()
    solo.add_message(role="user", content="第一句")  # 只有当前句,无历史对
    assert on._rolling_pairs(solo) == []


def test_chat_assembles_context_block():
    """context_turns=2 时组装 user 内容=上文参考块+模板;=0 时逐字节同旧。"""

    class _Inner(lp.llm.LLM):
        def __init__(self):
            super().__init__()
            self.captured = None

        def chat(self, *, chat_ctx, **kw):
            self.captured = chat_ctx
            raise RuntimeError("stop")

    import contextlib

    inner = _Inner()
    wrapper = lp.StatelessMTLLM(inner, "en", context_turns=2)
    ctx = lp.llm.ChatContext()
    ctx.add_message(role="user", content="我的包裹三天了还没到。")
    ctx.add_message(role="assistant", content="My parcel has not arrived for three days.")
    ctx.add_message(role="user", content="它什么时候能到？")
    with contextlib.suppress(RuntimeError):
        wrapper.chat(chat_ctx=ctx)
    sent = [str(getattr(i, "text_content", "") or "") for i in inner.captured.items if getattr(i, "role", None) == "user"]
    assert sent[0].startswith("上文参考（保持译名与指代一致，勿输出本段）：\n源：我的包裹三天了还没到。\n译：My parcel has not arrived for three days.\n\n")
    assert "将以下文本翻译为 `英语`" in sent[0]

    # 关(=0):同输入组装结果不含参考块
    inner2 = _Inner()
    off = lp.StatelessMTLLM(inner2, "en", context_turns=0)
    with contextlib.suppress(RuntimeError):
        off.chat(chat_ctx=ctx)
    sent2 = [str(getattr(i, "text_content", "") or "") for i in inner2.captured.items if getattr(i, "role", None) == "user"]
    assert sent2 == [lp._mt_prompt("它什么时候能到？", "en")]


# ---- P1-A MT 出口包裹引号剥离 ----


def _quote_stream():
    """构造 _StripMTQuoteStream(需事件循环,同其它用例 asyncio.run 姿势)。

    只测 _transform/_held 状态机,inner 用哑对象(_run 不会被驱动)。"""
    import asyncio as _a

    class _Inner(lp.llm.LLM):
        def chat(self, *, chat_ctx, **kw):  # pragma: no cover
            raise RuntimeError("stop")

    async def _mk():
        wrapper = lp.StatelessMTLLM(_Inner(), "en")
        return lp._StripMTQuoteStream(wrapper, object())

    return _a.run(_mk())


def test_mt_quote_strip_wrapping_quotes():
    stream = _quote_stream()
    # 首块 “Hello, → 剥“、末字符逗号扣住 → 出 "Hello"
    assert stream._transform("“Hello,") == "Hello"
    # 下块:扣住的逗号补发;新末字符 ” 扣住 → 出 ", how are you"
    assert stream._transform(" how are you”") == ", how are you"
    # 收尾:held 是闭合引号 → _run 判定吞(此处验证判定面)
    assert stream._held == "”" and stream._held in lp._MT_QUOTES_CLOSE


def test_mt_quote_strip_backticks_systematic():
    """反引号包裹(Hy-MT2 镜像模板,:1236 直打 100% 复现)——剥首尾。"""
    stream = _quote_stream()
    # 首块剥 `,末字符 ? 扣住
    assert stream._transform("`When will it arrive?") == "When will it arrive"
    # 下块只有闭合 `:扣住的 ? 补发、` 扣住 → 出 "?"
    assert stream._transform("`") == "?"
    assert stream._held == "`" and stream._held in lp._MT_QUOTES_CLOSE


def test_mt_quote_strip_plain_text_intact():
    stream = _quote_stream()
    parts = ["My parcel", " hasn't arrived", "."]
    out = []
    for p in parts:
        out.append(stream._transform(p))
    # 末字符“.”被扣(流未结束)——收尾非引号应补发
    assert "".join(out) + (stream._held or "") == "My parcel hasn't arrived."
    assert stream._held not in lp._MT_QUOTES_CLOSE


def test_parse_glossary_digit_terms_reach_asr_layer_dropped():
    """端到端小闭环:建单 glossary 文本 → parse → ASR 侧(数字丢)/MT 侧(原样)。"""
    raw = "顺丰=SF Express, 順豐速運；64325432"
    pairs = interpret.parse_glossary(raw)
    assert interpret.glossary_block(pairs) == "顺丰=SF Express；順豐速運；64325432"
    asr_ctx = asr_hotword_context(
        "cantonese", None, extra_hotwords=interpret.glossary_source_terms(pairs), include_industry=False
    )
    assert "順豐速運" in asr_ctx
    assert "64325432" not in asr_ctx  # ASR 侧数字主导丢弃(biasing 风险)


# ---- 5. MiniMax 语气词标记(2026-09-16 P2+)----


def test_voice_tags_supported_gate():
    """语气标记仅 2.8 系合成模型支持;非 2.8 会把标记当文本念出来,必须门控。"""
    assert interpret._voice_tags_supported("speech-2.8-hd") is True
    assert interpret._voice_tags_supported("speech-2.8-turbo") is True
    assert interpret._voice_tags_supported("speech-2.6-turbo") is False
    assert interpret._voice_tags_supported("") is False


def test_apply_voice_tags_leading_interjections():
    """Hy-MT2 把语气照词翻译(Hahaha/Coughs…),句首引导词换成 2.8 括号标记——
    合成层出真声,不再是假人念稿。只动句首,中性句原样返回。"""
    assert interpret._apply_voice_tags("Hahaha, this idea is really good.") == "(laughs) this idea is really good."
    assert interpret._apply_voice_tags("Coughs, let's continue the meeting.") == "(coughs) let's continue the meeting."
    assert interpret._apply_voice_tags("Sighs, we missed the target.") == "(sighs) we missed the target."
    assert interpret._apply_voice_tags("hehe, nice try!") == "(chuckle) nice try!"
    assert interpret._apply_voice_tags("Lol! That's wild.") == "(laughs) That's wild."
    assert interpret._apply_voice_tags("Alas, the deal fell through.") == "(sighs) the deal fell through."
    # 中性句/句中语气词不动
    assert interpret._apply_voice_tags("Today's meeting room is on the third floor.") == (
        "Today's meeting room is on the third floor."
    )
    assert interpret._apply_voice_tags("Well, hahaha happened mid-sentence.") == (
        "Well, hahaha happened mid-sentence."
    )
    # 纯语气句:只剩标记本身
    assert interpret._apply_voice_tags("Hahaha!") == "(laughs)"


def test_strip_voice_tags():
    """标记只属合成层:字幕/落库口径剥掉,剥后压掉多余空白与标点前悬挂空格。"""
    assert interpret._strip_voice_tags("Good morning (laughs), really?") == "Good morning, really?"
    assert interpret._strip_voice_tags("嗯 (coughs) 我看一下。") == "嗯 我看一下。"
    assert interpret._strip_voice_tags("no tags here") == "no tags here"
    assert interpret._strip_voice_tags("(Sighs) fine.") == "fine."  # 大小写不敏感
    # 括号内容不在白名单(如大写缩写)不误伤
    assert interpret._strip_voice_tags("call (USA) now") == "call (USA) now"


def test_mt_prompt_no_tag_rule():
    """语气标记不走 prompt:Hy-MT2 对模板外指示无视(实测),规则行纯浪费 token。"""
    prompt = lp._mt_prompt("你好", "en", "")
    assert "(laughs)" not in prompt
    assert prompt == lp._mt_prompt("你好", "en")  # 字节稳定


# ---- 4. manual 自驱管线：_build_mt_context / _mt_once（P2）----


def test_build_mt_context_shape():
    """单句翻译 ctx = instructions(system) + 滚动「源→译」对(旧→新) + 当前句(user)。
    StatelessMTLLM 只读最后一条 user(模板无状态),对历史给 _rolling_pairs 抽;
    回退通用 LLM 则当真实上下文翻译。"""
    ctx = interpret._build_mt_context(
        "You are an interpreter.",
        [("你们好", "Hello"), ("报价多少", "How much is the quote")],
        "今天能发货吗",
    )
    roles = [getattr(it, "role", None) for it in ctx.items]
    assert roles == ["system", "user", "assistant", "user", "assistant", "user"]
    texts = [getattr(it, "text_content", None) for it in ctx.items]
    assert texts[0] == "You are an interpreter."
    assert texts[1] == "你们好" and texts[2] == "Hello"
    assert texts[3] == "报价多少" and texts[4] == "How much is the quote"
    assert texts[5] == "今天能发货吗"


def test_build_mt_context_empty_pairs_and_no_instructions():
    ctx = interpret._build_mt_context("", [], "hello")
    assert [getattr(it, "role", None) for it in ctx.items] == ["user"]


def test_mt_once_collects_stream_deltas():
    """_mt_once 直调 chat(chat_ctx=...) 并把 delta.content 拼成整句(剥引号由
    _StripMTQuoteStream 在 StatelessMTLLM 内完成)。"""

    class _FakeStream:
        def __init__(self, chunks):
            self._chunks = chunks

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for c in self._chunks:
                yield c

    class _Chunk:
        def __init__(self, content):
            self.delta = type("D", (), {"content": content})()

    captured = {}

    class _FakeLLM:
        def chat(self, *, chat_ctx, **kwargs):
            captured["ctx"] = chat_ctx
            return _FakeStream([_Chunk("Hello"), None, _Chunk(" there"), _Chunk("")])

    import asyncio

    ctx = interpret._build_mt_context("", [], "你好")
    out = asyncio.run(interpret._mt_once(_FakeLLM(), ctx))
    assert out == "Hello there"
    assert captured["ctx"] is ctx


def test_mt_once_timeout_raises():
    import asyncio

    class _SlowStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(30)
            raise StopAsyncIteration

    class _SlowLLM:
        def chat(self, *, chat_ctx, **kwargs):
            return _SlowStream()

    async def _run():
        ctx = interpret._build_mt_context("", [], "你好")
        return await interpret._mt_once(_SlowLLM(), ctx, timeout_s=0.05)

    try:
        asyncio.run(_run())
        raise AssertionError("expected TimeoutError")
    except asyncio.TimeoutError:
        pass
