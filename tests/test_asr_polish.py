"""asr_polish 确定性音近纠错核心单测(2026-09-27)。

覆盖:冻结铁律(数字/中文数字词/拉丁 run)、粤语特征字不减、等位替换、
生成资产的吸附质量、en 车道 vocab snap、max_edits 整层放弃、车道判定。

离线纯函数,零网络零模型;en 车道用构造 vocab,zh/cantonese 兼用**随源码
分发的真实资产**(load_variant_table())与**构造表**(钉死具体行为)。
"""

from __future__ import annotations

import json
from pathlib import Path

from bok_voice_core.asr_polish import (
    PolishResult,
    detect_lane,
    load_variant_table,
    polish_transcript,
)

# 随仓资产(生成物,质量面直接测它)。
TABLE = load_variant_table()

# 构造表:用于把「具体替换行为」钉死,不依赖生成排序。
T_ZHAPIAN = {"cantonese": {"詐騙": ["乍騙"]}}
T_SHUENGFUNG = {"cantonese": {"順豐": ["順風"]}}


# ---------------------------------------------------------------------------
# 车道判定
# ---------------------------------------------------------------------------

def test_detect_lane_cantonese_zh_en():
    assert detect_lane("你哋公司喺邊度") == "cantonese"
    assert detect_lane("你们公司在哪里") == "zh"
    assert detect_lane("SF Express") == "en"
    assert detect_lane("13800138000") == "en"
    # 空/纯符号退化 zh(不纠错),不误判 en。
    assert detect_lane("") == "zh"
    assert detect_lane("，。！") == "zh"
    # 汉拉混排按汉字主体判(拉丁只是专名)。
    assert detect_lane("我的WhatsApp係三七七八九零") == "cantonese"


# ---------------------------------------------------------------------------
# 冻结铁律
# ---------------------------------------------------------------------------

def test_freeze_wa_digit_report_untouched():
    # WhatsApp 报号场景:阿拉伯+中文数字混排,一个字符都不许动。
    for text in (
        "我的WhatsApp係三七七八九零",
        "我的WhatsApp是13800138000",
        "單號 1234 5678 九十",
    ):
        res = polish_transcript(text, "cantonese", TABLE)
        assert res.text == text
        assert res.edits == []


def test_freeze_blocks_even_when_variant_matches_number_span():
    # 极端构造:表里有「三七」这个变体,但它是中文数字串的一部分 → 禁止改。
    table = {"cantonese": {"準備": ["三七"]}}
    text = "我係三七七八九零"
    res = polish_transcript(text, "cantonese", table)
    assert res.text == text
    assert res.edits == []


def test_freeze_latin_run_on_zh_cantonese_lane():
    # zh/cantonese 车道拉丁 run 冻结:即使表里登记了 latin 变体也不动。
    table = {"cantonese": {"準備": ["SF"]}}
    text = "順豐SF"
    res = polish_transcript(text, "cantonese", table)
    assert res.text == text
    assert res.edits == []


# ---------------------------------------------------------------------------
# 粤语特征字保留 + 等位替换
# ---------------------------------------------------------------------------

def test_cantonese_markers_never_reduced():
    text = "你哋係唔係香港嘅，我俾乍騙咗"
    res = polish_transcript(text, "cantonese", T_ZHAPIAN)
    assert "詐騙" in res.text and "乍騙" not in res.text
    # 特征字集合(output) ⊇ 特征字集合(input)
    markers = frozenset("係哋嘅喺唔掂嚟啱嘢乜嘥咁咗嗰啲冇")
    assert {c for c in text if c in markers} <= {c for c in res.text if c in markers}


def test_marker_reduction_guard_aborts():
    # 构造一个会把粤语特征字纠掉的表 → 语言不变性守卫整层放弃。
    table = {"cantonese": {"詐騙": ["嘅騙"]}}
    text = "嘅騙"
    res = polish_transcript(text, "cantonese", table)
    assert res.text == text
    assert res.edits == []


def test_equal_span_replacement_same_length():
    text = "我俾乍騙咗"
    res = polish_transcript(text, "cantonese", T_ZHAPIAN)
    assert res.text == "我俾詐騙咗"
    assert len(res.text) == len(text)  # 同长变体 → 输出等长
    start, end, before, after = res.edits[0]
    assert (before, after) == ("乍騙", "詐騙")
    # 只改 span 内:span 外前后缀原样。
    assert res.text == text[:start] + after + text[end:]


def test_span_only_change_with_length_shift():
    # 变体比正确词长 1:输出整体短 1,但只有 span 内变化。
    table = {"cantonese": {"詐騙": ["乍騙騙"]}}
    text = "我睇乍騙騙呀"
    res = polish_transcript(text, "cantonese", table)
    start, end, before, after = res.edits[0]
    assert before == "乍騙騙" and after == "詐騙"
    assert res.text == text[:start] + after + text[end:]
    assert res.text == "我睇詐騙呀"


# ---------------------------------------------------------------------------
# 吸附质量(生成资产)
# ---------------------------------------------------------------------------

def test_no_hit_leaves_text_untouched():
    # 无域词变体命中 → 全文原样(不许拿「地/既」这类常见错字乱纠)。
    text = "你地公司係唔係香港既"
    assert detect_lane(text) == "cantonese"
    res = polish_transcript(text, detect_lane(text), TABLE)
    assert res.text == text
    assert res.edits == []


def test_asset_snaps_zhapian_variant():
    # 生成资产里 詐騙 的变体含「乍騙」→ 就地吸附回 詐騙。
    text = "我俾乍騙咗"
    res = polish_transcript(text, "cantonese", TABLE)
    assert res.text == "我俾詐騙咗"
    assert len(res.edits) == 1
    assert res.edits[0][2:] == ("乍騙", "詐騙")


def test_curated_shunfeng_and_zh_mirror():
    res = polish_transcript("順風快遞", "cantonese", TABLE)
    assert "順豐" in res.text and "順風" not in res.text
    res2 = polish_transcript("顺风快递", "zh", TABLE)
    assert "顺丰" in res2.text and "顺风" not in res2.text


# ---------------------------------------------------------------------------
# en 车道 vocab snap
# ---------------------------------------------------------------------------

def test_en_vocab_snap():
    vocab = ["SF Express", "Pinduoduo"]
    res = polish_transcript("SF Expres", "en", TABLE, vocab=vocab)
    assert res.text == "SF Express"
    assert res.edits[0][2:] == ("SF Expres", "SF Express")


def test_en_short_abbrev_and_digits_untouched():
    vocab = ["SF Express", "Pinduoduo"]
    # 全大写缩写 ≤3 字母不动。
    assert polish_transcript("SF", "en", TABLE, vocab=vocab).text == "SF"
    assert polish_transcript("SF 12345", "en", TABLE, vocab=vocab).text == "SF 12345"
    # 数字 token 参与时整窗放弃。
    assert polish_transcript("Pinduoduo7", "en", TABLE, vocab=vocab).text == "Pinduoduo7"
    # 长度 <4 的 token 不动。
    assert polish_transcript("Sf", "en", TABLE, vocab=vocab).text == "Sf"


def test_en_ambiguous_vocab_not_touched():
    # 与两个 vocab 等距 → 不动。
    vocab = ["Bobcat", "Bobcats"]
    res = polish_transcript("Bobcas", "en", TABLE, vocab=vocab)
    assert res.text == "Bobcas"
    assert res.edits == []


def test_en_clean_word_not_touched():
    vocab = ["Pinduoduo"]
    res = polish_transcript("Pinduoduo", "en", TABLE, vocab=vocab)
    assert res.text == "Pinduoduo" and res.edits == []


# ---------------------------------------------------------------------------
# max_edits 整层放弃
# ---------------------------------------------------------------------------

def test_max_edits_overflow_abandons_all():
    text = "乍騙乍騙"
    # 命中 2 处 > cap 1 → 整层放弃,不做半纠。
    res = polish_transcript(text, "cantonese", T_ZHAPIAN, max_edits=1)
    assert res.text == text and res.edits == []
    # cap 2 → 两处都改。
    res2 = polish_transcript(text, "cantonese", T_ZHAPIAN, max_edits=2)
    assert res2.text == "詐騙詐騙" and len(res2.edits) == 2


def test_max_edits_zero_disables():
    res = polish_transcript("乍騙", "cantonese", T_ZHAPIAN, max_edits=0)
    assert res.text == "乍騙" and res.edits == []


def test_en_max_edits_overflow_abandons():
    vocab = ["Pinduoduo", "Taobao", "Douyin"]
    res = polish_transcript("Pinduodu Taoba Douyi", "en", TABLE, vocab=vocab, max_edits=2)
    assert res.text == "Pinduodu Taoba Douyi"
    assert res.edits == []


# ---------------------------------------------------------------------------
# 资产形态 / 车道回退
# ---------------------------------------------------------------------------

def test_lane_fallback_and_result_shape():
    res = polish_transcript("你哋其中一個係乍騙", "", T_ZHAPIAN)
    assert isinstance(res, PolishResult)
    assert res.lane == "cantonese"
    assert "詐騙" in res.text


def test_load_variant_table_accepts_plain_and_wrapped(tmp_path: Path):
    plain = {"zh": {"测试": ["测試"]}}
    p1 = tmp_path / "plain.json"
    p1.write_text(json.dumps(plain, ensure_ascii=False), encoding="utf-8")
    assert load_variant_table(p1) == plain

    wrapped = {"meta": {"version": 1}, "variants": plain}
    p2 = tmp_path / "wrapped.json"
    p2.write_text(json.dumps(wrapped, ensure_ascii=False), encoding="utf-8")
    assert load_variant_table(p2) == plain


def test_asset_meta_present():
    # 生成资产必须带 meta 头(时间戳/来源署名),且 zh/cantonese 两车道非空。
    data = json.loads(
        (Path(__file__).resolve().parents[1] / "packages" / "core" / "bok_voice_core"
         / "assets" / "asr_variants.json").read_text(encoding="utf-8")
    )
    assert "meta" in data and "variants" in data
    assert data["meta"]["counts"]["zh"]["variants"] > 0
    assert data["meta"]["counts"]["cantonese"]["variants"] > 0
    assert any("CC-BY-4.0" in s for s in data["meta"]["sources"])
