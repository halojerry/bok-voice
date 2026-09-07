"""ASR 热词组装单测:asr_hotword_context(lang, object_card)——行业静态词 +
对象文字字段(courier/contact_channel),数字串过滤,长度护栏,kill-switch。

Qwen3-ASR 官方 customizable context = system message 词汇表(「Vocabulary: …」
示例格式),与 language 强制可叠加;组装发生在 A 线会话装配,经 /api/start 下发。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import asr_hotword_context  # noqa: E402


def test_static_words_by_language():
    canto = asr_hotword_context("cantonese", None)
    assert canto.startswith("Vocabulary: ")
    for w in ("單號", "賠償", "WhatsApp"):
        assert w in canto, (w, canto)
    zh = asr_hotword_context("zh", None)
    assert "单号" in zh and "赔偿" in zh
    en = asr_hotword_context("en", None)
    assert "tracking" in en and "refund" in en


def test_object_fields_injected_digits_excluded():
    ctx = asr_hotword_context(
        "cantonese",
        {"courier": "京東物流", "contact_channel": "WhatsApp", "tracking_no": "12345678", "phone": "64325432"},
    )
    assert "京東物流" in ctx
    # 数字串(单号/电话)不进热词:幻听数字风险,下游 known-number 过滤兜底
    assert "12345678" not in ctx and "64325432" not in ctx


def test_digit_dominant_object_value_dropped():
    ctx = asr_hotword_context("cantonese", {"courier": "12345678"})
    assert "12345678" not in ctx
    # 静态表照常
    assert "單號" in ctx


def test_dedup_static_and_object():
    ctx = asr_hotword_context("cantonese", {"contact_channel": "WhatsApp"})
    assert ctx.count("WhatsApp") == 1, ctx


def test_length_cap_no_half_word(monkeypatch):
    import agent_runtime.agent as ag

    monkeypatch.setattr(ag, "_ASR_HOTWORD_MAX_CHARS", 30)
    ctx = asr_hotword_context("cantonese", {"courier": "京東物流"})
    assert len(ctx) <= 30
    assert ctx.startswith("Vocabulary: ")
    body = ctx[len("Vocabulary: "):]
    for tok in body.split(", "):
        assert tok and tok in ("單號", "運單", "賠償", "運費", "專員", "集運", "時效", "上門", "追蹤", "核實", "WhatsApp", "微信", "京東物流"), tok


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_ASR_HOTWORDS", "0")
    assert asr_hotword_context("cantonese", {"courier": "京東物流"}) == ""


def test_unknown_lang_falls_back_to_cantonese_table():
    ctx = asr_hotword_context("", None)
    assert "單號" in ctx


def test_template_hotwords_merged_and_prioritised():
    """模板 hotwords 字段(二期):话术专属词入列且排在静态表前(超限截断时优先保留)。"""
    ctx = asr_hotword_context("cantonese", None, extra_hotwords="順豐速運, 生果日報")
    assert "順豐速運" in ctx and "生果日報" in ctx
    assert ctx.index("順豐速運") < ctx.index("單號"), ctx  # 模板词先于静态行业词


def test_template_hotwords_mixed_separators_and_filters():
    """中英逗号/顿号/分号/换行都收;与静态表去重;数字主导词丢弃(幻听号码风险)。"""
    ctx = asr_hotword_context("cantonese", None, extra_hotwords="單號、淘寶；64325432\n丰巢")
    assert ctx.count("單號") == 1, ctx  # 与静态表去重
    assert "淘寶" in ctx and "丰巢" in ctx
    assert "64325432" not in ctx


def test_template_hotwords_cap_keeps_template_words_first(monkeypatch):
    import agent_runtime.agent as ag

    monkeypatch.setattr(ag, "_ASR_HOTWORD_MAX_CHARS", 30)
    ctx = asr_hotword_context("cantonese", None, extra_hotwords="順豐速運, 極長嘅自訂詞語示例超出上限")
    assert "順豐速運" in ctx  # 模板词最优先保留,静态表让位
    assert len(ctx) <= 30


def test_template_hotwords_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_ASR_HOTWORDS", "0")
    assert asr_hotword_context("cantonese", None, extra_hotwords="順豐速運") == ""
