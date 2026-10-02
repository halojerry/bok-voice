"""ASR 热词组装单测:asr_hotword_context(lang, object_card)——行业静态词 +
对象文字字段(courier/contact_channel),数字串过滤,长度护栏,kill-switch。

Qwen3-ASR 官方 customizable context = system message 自由文本(官方无格式
要求;「Vocabulary:」标签 2026-09-27 A/B 砍除,见 agent.py 内注释),与
language 强制可叠加;组装发生在 A 线会话装配,经 /api/start 下发。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import asr_hotword_context  # noqa: E402


def test_static_words_by_language():
    canto = asr_hotword_context("cantonese", None)
    assert not canto.startswith("Vocabulary")  # 标签已砍:裸 join
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
    for tok in ctx.split(", "):
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


# ---- 开采热词(第四来源,EX-H2)----------------------------------------------


def test_mined_empty_byte_identical_zero_drift():
    """零漂移铁律:mined_hotwords 为空串时组装产物逐字节同旧(无参数形态)。

    覆盖默认 cap、行业段保底截断、对象字段、B 线无行业段四类形态。
    """
    cases = [
        ("cantonese", None, ""),
        ("cantonese", {"courier": "京東物流", "contact_channel": "WhatsApp"}, ""),
        ("zh", None, "順豐速運, 生果日報"),
        ("en", {"courier": "SF Express"}, "Pinduoduo"),
        ("", None, "順豐速運"),  # 未知语言回退粤语表
    ]
    for lang, card, extra in cases:
        legacy = asr_hotword_context(lang, card, extra)
        with_empty = asr_hotword_context(lang, card, extra, mined_hotwords="")
        assert with_empty == legacy, (lang, card, extra, with_empty, legacy)
        assert with_empty.encode("utf-8") == legacy.encode("utf-8")


def test_mined_empty_byte_identical_under_cap(monkeypatch):
    """超限截断形态也零漂移:cap 压到 30 时两形态产物逐字节相同。"""
    import agent_runtime.agent as ag

    monkeypatch.setattr(ag, "_ASR_HOTWORD_MAX_CHARS", 30)
    for lang, card, extra in (
        ("cantonese", None, ""),
        ("cantonese", {"courier": "京東物流"}, "順豐速運"),
        ("zh", {"courier": "顺丰"}, "拼多多"),
    ):
        legacy = asr_hotword_context(lang, card, extra)
        assert asr_hotword_context(lang, card, extra, mined_hotwords="") == legacy


def test_mined_order_template_then_mined_then_industry_then_object():
    """非空插入序:模板词 → 开采词(保服务端频次序) → 行业静态词 → 对象字段。"""
    ctx = asr_hotword_context(
        "cantonese",
        {"courier": "京東物流"},
        extra_hotwords="順豐速運",
        mined_hotwords="開採甲,開採乙",
    )
    assert "開採甲" in ctx and "開採乙" in ctx
    order = [ctx.index(t) for t in ("順豐速運", "開採甲", "開採乙", "單號", "京東物流")]
    assert order == sorted(order), ctx
    assert ctx.index("開採乙") < ctx.index("單號"), ctx  # 开采段整段先于行业段


def test_mined_dedupe_against_industry_word():
    """开采词与静态行业表重复→去重(大小写不敏感);开采位次保留(先入者赢)。"""
    ctx = asr_hotword_context("cantonese", None, mined_hotwords="單號, whatsapp")
    assert ctx.count("單號") == 1, ctx
    assert ctx.lower().count("whatsapp") == 1, ctx  # 大小写不敏感去重(行业表 WhatsApp 让位)
    # 开采段(先于行业段)保留首现位次,不与行业表重复出现
    assert ctx.index("單號") < ctx.index("運單"), ctx


def test_mined_digit_dominant_dropped():
    """数字主导开采词丢弃(皮带:服务端已过滤,本地再兜一层)。"""
    ctx = asr_hotword_context("cantonese", None, mined_hotwords="12345,開採甲")
    assert "12345" not in ctx
    assert "開採甲" in ctx


def test_mined_mixed_separators_same_discipline():
    """开采词与 extra_hotwords 同款拆分纪律:中英逗号/顿号/分号/换行都收。"""
    ctx = asr_hotword_context("cantonese", None, mined_hotwords="甲、乙；丙\n丁")
    for w in ("甲", "乙", "丙", "丁"):
        assert w in ctx, (w, ctx)


def test_mined_cap_truncation_drops_deterministically_and_logs(monkeypatch, capsys):
    """超限截断:行业段整段保底,模板段先入、开采段让位;丢词必打点。"""
    import agent_runtime.agent as ag

    monkeypatch.setattr(ag, "_ASR_HOTWORD_MAX_CHARS", 20)
    monkeypatch.setattr(
        ag, "_ASR_HOTWORDS", {"cantonese": ("單號", "運單")}  # 行业段整段保底可行
    )
    ctx = asr_hotword_context(
        "cantonese", None, extra_hotwords="模板一", mined_hotwords="開採甲,開採乙"
    )
    out = capsys.readouterr().out
    # 模板段(3)+接缝(2)+首枚开采词(2+3)+行业段(2+2+2+2)=18 ≤ 20;
    # 第二枚开采词(2+3)超预算 → 丢 1 枚(joint 后 fit 到 pre_budget=12 为止)。
    assert len(ctx) <= 20
    assert "模板一" in ctx and "開採甲" in ctx
    assert "開採乙" not in ctx  # 开采段让位于模板段/行业段
    assert "單號" in ctx and "運單" in ctx  # 行业段整段保底
    assert "ASR_HOTWORD_TRUNCATED dropped=1 layer=mined" in out, out


def test_mined_plain_greedy_truncation_logs_layer(monkeypatch, capsys):
    """行业段自身超限 → 整体贪心(旧档),开采段在行业段前;丢行业尾必打点。"""
    import agent_runtime.agent as ag

    monkeypatch.setattr(ag, "_ASR_HOTWORD_MAX_CHARS", 20)
    ctx = asr_hotword_context("cantonese", None, mined_hotwords="開採一號,開採二號")
    out = capsys.readouterr().out
    assert len(ctx) <= 20
    assert "開採一號" in ctx and "開採二號" in ctx  # 开采段先入=优先保留
    assert "單號" in ctx  # 紧跟其后(预算内)
    assert "ASR_HOTWORD_TRUNCATED" in out and "layer=" in out, out

