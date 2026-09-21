"""E4 改口检测纯函数矩阵（core，2026-09-20）。

契约：``bok_voice_core.correction_intent``（type4me ``CorrectionIntentAnalysis.swift``
移植，口径取 **IntelliSense**——「不是A是B」豁免；离线润色面相反的 Polish 口径
不在本模块）。本文件只测纯函数面，不碰接线。

判例逐条移植自 type4me 锁定的行为，另补我们的「数字零改写」断言。

术语：语言相关字面量只用 zh / cantonese / en（AGENTS.md 术语铁律）。
"""

from __future__ import annotations

from bok_voice_core.correction_intent import (
    contains_explicit_correction,
    explicit_correction_ranges,
    semantic_negation_counts,
    span_after_last_correction,
)

_NEGATION_KEYS = {
    "prohibition",
    "inability",
    "absence",
    "contradiction",
    "never",
    "general",
}


# ---- 1. 显式改口:标记 + 可选后缀,取后值 ----


def test_explicit_time_switch_takes_tail():
    text = "今天7点……哦不，8点吧"
    assert contains_explicit_correction(text) is True
    assert explicit_correction_ranges(text)  # 非空
    assert span_after_last_correction(text) == "8点吧"


def test_restate_then_change_takes_tail():
    text = "算了，重说，改成明天三点"
    assert contains_explicit_correction(text) is True
    # 独立后续词「改成」自成一个区间(带「重说」后缀的标记区间也命中,但取后值一视同仁)
    assert (6, 8) in explicit_correction_ranges(text)
    assert span_after_last_correction(text) == "明天三点"


def test_suffix_is_absorbed_into_span():
    # 可选后缀(改成/换成/应该是/是)整段进区间 → 取后值不把「是」当值的一部分。
    assert explicit_correction_ranges("哦不，是 85212345") == [(0, 4)]
    assert span_after_last_correction("哦不，是 85212345") == "85212345"


def test_ranges_sorted_by_start():
    spans = explicit_correction_ranges("算了，重说，改成明天三点")
    assert spans == sorted(spans, key=lambda span: span[0])
    assert spans[0] == (0, 2)


# ---- 2. 裸对比豁免:「不是A，是B」不出区间(词表里根本没有「不是」) ----


def test_bare_contrast_is_not_a_correction():
    text = "不是发票，是快递单"
    assert explicit_correction_ranges(text) == []
    assert contains_explicit_correction(text) is False
    assert span_after_last_correction(text) is None
    counts = semantic_negation_counts(text)
    assert counts["contradiction"] == 1  # 「不是」算 contradiction
    assert counts["general"] == 0  # 未被 general 重复计数


# ---- 3. 被否定的「改成」不是改口:前缀位 + 后缀位双保险 ----


def test_negated_change_prefix_window_rejects():
    text = "不要改成 1500"
    assert explicit_correction_ranges(text) == []
    assert contains_explicit_correction(text) is False
    assert semantic_negation_counts(text)["prohibition"] == 1


def test_negated_change_guards():
    # 前缀位否定的后置窗:「不能改成」/「不要换成」前缀以否定词结尾 → 丢弃。
    assert explicit_correction_ranges("不能改成 200") == []
    assert explicit_correction_ranges("不要换成三号") == []
    # 负向后行 (?<!别):「别改成X」连区间都不该出现。
    assert explicit_correction_ranges("别改成X") == []
    assert explicit_correction_ranges("不改成X") == []
    # 后缀位否定 (?!不要):「改成不要…」被拦。
    assert explicit_correction_ranges("改成不要") == []


# ---- 4. 语义否定计数:元话语先删 + 顺序消费防重复 ----


def test_metadiscourse_removed_before_counting():
    # 「能不能」是疑问标记,先被删 → 不被当疑问误计,只剩「不要这样」。
    assert semantic_negation_counts("能不能不要这样")["prohibition"] == 1
    # 「不过」是让步连词,先被删 → 「不行」计 inability,而非 general 把两个「不」都算上。
    counts = semantic_negation_counts("不过不行")
    assert counts["inability"] == 1
    assert counts["general"] == 0


def test_sequential_consumption_no_double_count():
    # prohibition 吃掉「不要」、inability 吃掉「不能」、general 兜底「不」——各计一次。
    counts = semantic_negation_counts("不要不能不去")
    assert counts["prohibition"] == 1
    assert counts["inability"] == 1
    assert counts["general"] == 1


def test_negation_keys_and_classes():
    counts = semantic_negation_counts("我不去，但请你别催；我不能，也从不忘。")
    assert set(counts) == _NEGATION_KEYS
    assert counts["prohibition"] == 1  # 别
    assert counts["inability"] == 1  # 不能
    assert counts["never"] == 1  # 从不
    assert counts["general"] == 1  # 不去


# ---- 5. 英文标记 ----


def test_english_markers():
    text = "i mean sorry change it to 5"
    assert contains_explicit_correction(text) is True
    # 最后一个标记是 sorry,取其后段。
    assert span_after_last_correction(text) == "change it to 5"


# ---- 6. 边界 ----


def test_empty_text():
    assert explicit_correction_ranges("") == []
    assert contains_explicit_correction("") is False
    assert span_after_last_correction("") is None
    counts = semantic_negation_counts("")
    assert set(counts) == _NEGATION_KEYS
    assert all(value == 0 for value in counts.values())


def test_marker_without_tail_gives_none():
    assert explicit_correction_ranges("哦不") == [(0, 2)]
    assert span_after_last_correction("哦不") is None
    assert span_after_last_correction("算了") is None


# ---- 7. 数字保护联动:纯检测、零改写 ----


def test_number_segment_returned_verbatim():
    """改口取后值只切原文段;任何数字逐字原样,模块不修改输入语义。"""
    text = "哦不，是 85212345"
    span = span_after_last_correction(text)
    assert span == "85212345"
    # 返回值必为输入的一个连续子串(零改写),且原输入对象原样未变。
    assert span is not None and span in text
    assert text == "哦不，是 85212345"


def test_span_after_is_contiguous_slice_of_input():
    text = "算了，重说，改成明天三点 1500 号"
    span = span_after_last_correction(text)
    assert span is not None
    assert span in text
    assert "1500" in span  # 数字原样随段返回,未被归一/替换
    assert span == "明天三点 1500 号"


def test_functions_are_pure():
    text = "不要改成 1500"
    before = str(text)
    explicit_correction_ranges(text)
    contains_explicit_correction(text)
    span_after_last_correction(text)
    semantic_negation_counts(text)
    assert text == before  # 四个函数均零副作用、不改输入
