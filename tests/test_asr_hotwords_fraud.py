"""RC5 ASR 热词扩容单测(2026-09-17 防诈/异议域)。

实测粤语碎裂:「詐騙集團→田静宁」「機器人→细人」「主管→一旦主管」
「倉喺邊度→宝健」「你話我知→打雷啦」——防诈/异议域词零覆盖。修法:
三语表扩容 + cap 120→200 + 截断行业段整段保底(模板词吃满预算也挤不掉
行业表,保持「改一处表运营立即生效」契约;字符串位次不变=模板→行业→对象,
与 test_asr_hotwords.py 既有钉死口径兼容)。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import agent_runtime.agent as ag  # noqa: E402
from agent_runtime.agent import _ASR_HOTWORDS, _ASR_HOTWORD_MAX_CHARS, asr_hotword_context  # noqa: E402

_NEW_WORDS = {
    "cantonese": ("詐騙", "呃人", "證明", "機器人", "投訴", "主管", "人工", "轉接", "退款", "倉庫", "熱線", "官網"),
    "zh": ("诈骗", "骗子", "证明", "机器人", "投诉", "主管", "人工", "转接", "退款", "仓库", "热线", "官网"),
    "en": ("scam", "fraud", "robot", "complaint", "manager", "transfer", "warehouse", "hotline", "website"),
}


def test_new_words_in_context_all_langs():
    for lang, words in _NEW_WORDS.items():
        ctx = asr_hotword_context(lang, None)
        assert ctx.startswith("Vocabulary: ")
        for w in words:
            assert w in ctx, (lang, w, ctx)


def test_cap_raised_to_200_and_full_tables_fit():
    """扩容的意义:三语整表(含新词)必须在 cap 200 内完整下发,零截断。"""
    assert _ASR_HOTWORD_MAX_CHARS == 200
    for lang in ("cantonese", "zh", "en"):
        ctx = asr_hotword_context(lang, None)
        assert ctx == "Vocabulary: " + ", ".join(_ASR_HOTWORDS[lang]), (lang, ctx)
        assert len(ctx) <= 200


def test_industry_words_survive_template_inflation():
    """行业段保底截断:模板热词吃满预算也不得挤掉行业表(旧逐词截断会把
    「官網」类表尾词整段丢掉=运营改表不生效)。模板词仍先入列,超预算者让位。"""
    # 6 个 16 字模板词:总长 221 字 > cap 200,旧逻辑下行业表尾被截。
    template_words = [f"超長測試模板熱詞佔位演示第{c}號位" for c in "甲乙丙丁戊己"]
    ctx = asr_hotword_context("cantonese", None, extra_hotwords=", ".join(template_words))
    assert len(ctx) <= _ASR_HOTWORD_MAX_CHARS, ctx
    # 行业表头尾都在(保底)
    assert "詐騙" in ctx and "官網" in ctx, ctx
    # 模板词先入列(位次契约不变),超预算的让位
    assert template_words[0] in ctx, ctx
    assert ctx.index(template_words[0]) < ctx.index("單號"), ctx
    assert template_words[4] not in ctx and template_words[5] not in ctx, ctx
    # 整词粒度:每个 token 都是已知词,唔截半词
    known = set(_ASR_HOTWORDS["cantonese"]) | set(template_words)
    for tok in ctx[len("Vocabulary: "):].split(", "):
        assert tok in known, tok


def test_fallback_plain_truncation_when_industry_alone_over_cap(monkeypatch):
    """行业表自身超限 → 退回整体贪心(既有行为:模板→行业→对象原序,尾部让位)。"""
    monkeypatch.setattr(ag, "_ASR_HOTWORD_MAX_CHARS", 30)
    ctx = asr_hotword_context("cantonese", None, extra_hotwords="順豐速運")
    assert ctx.startswith("Vocabulary: ")
    assert len(ctx) <= 30
    for tok in ctx[len("Vocabulary: "):].split(", "):
        assert tok in ("順豐速運", "單號", "運單", "賠償", "運費", "專員", "集運", "時效", "上門", "追蹤"), tok


def test_bline_no_industry_path_unaffected():
    """B 线 include_industry=False:不吃行业表,glossary 词照常(B 线契约不变)。"""
    ctx = asr_hotword_context("cantonese", None, extra_hotwords="順豐速運", include_industry=False)
    assert "順豐速運" in ctx
    assert "單號" not in ctx and "詐騙" not in ctx, ctx


def test_no_digit_dominant_among_new_words():
    """防诈域词全是文字词——幻听数字防线(数字主导 token 丢弃)不该有输入。"""
    for lang, words in _NEW_WORDS.items():
        for w in words:
            assert not ag._is_digit_dominant(w), (lang, w)
