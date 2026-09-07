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
