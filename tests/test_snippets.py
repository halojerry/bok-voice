"""E1 snippet 后置正则轨纯函数矩阵（core，2026-09-20）。

契约：``bok_voice_core.snippets``（type4me ``SnippetStorage.swift`` 移植 + 我们的
数字铁律/空 trigger 铁律/长度护栏）。本文件只测纯函数面，不碰接线。

术语：语言相关字面量只用 zh / cantonese / en（AGENTS.md 术语铁律）。
"""

from __future__ import annotations

import re

import pytest

from bok_voice_core.snippets import (
    MAX_REPLACEMENT_CHARS,
    MAX_TRIGGER_CHARS,
    CompiledRule,
    SnippetApplication,
    SnippetRule,
    apply_snippets,
    build_flex_pattern,
    compile_rules,
    compile_rules_with_skipped,
    merge_rules,
    normalize_key,
    parse_rule,
    validate_rule,
)


# ---- 1. pattern 形状：逐字符 escape + \s* 连接 + ASCII lookaround ----


def test_build_flex_pattern_latin_shape():
    assert build_flex_pattern("web coding") == (
        r"(?<![a-zA-Z0-9])w\s*e\s*b\s*c\s*o\s*d\s*i\s*n\s*g(?![a-zA-Z0-9])"
    )


def test_build_flex_pattern_cjk_shape():
    # 逐字符独立 re.escape，CJK 不被转义；字间统一 \s*。
    assert build_flex_pattern("单后") == r"(?<![a-zA-Z0-9])单\s*后(?![a-zA-Z0-9])"


def test_build_flex_pattern_escapes_regex_metachars():
    # 逐字符 escape：'.' 是字面点，不是通配符。
    assert build_flex_pattern("a.b") == r"(?<![a-zA-Z0-9])a\s*\.\s*b(?![a-zA-Z0-9])"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", "\u3000"])
def test_build_flex_pattern_rejects_blank(bad):
    # 空 trigger 会生成匹配空串的正则——拒绝构造（空 trigger 铁律的机械防线）。
    with pytest.raises(ValueError):
        build_flex_pattern(bad)


# ---- 2. 大小写不敏感 + 空格不敏感 ----


@pytest.mark.parametrize(
    "text",
    ["webcoding", "web coding", "Web  Coding", "WEBCODING", "Web\tCoding", "WeB CoDiNg"],
)
def test_case_and_space_insensitive(text):
    rules = [SnippetRule("web coding", "WebCoding")]
    out, applied = apply_snippets(text, rules)
    assert out == "WebCoding"
    assert applied == [("web coding", "WebCoding")]


def test_flex_matches_across_whitespace_in_sentence():
    rules = [SnippetRule("单 后", "单号")]
    out, applied = apply_snippets("我个单   后系咩", rules)
    assert out == "我个单号系咩"
    assert applied == [("单 后", "单号")]


def test_ignorecase_flag_is_set():
    rule = compile_rules([SnippetRule("WebCoding", "Web Coding")])[0]
    assert rule.pattern.flags & re.IGNORECASE
    assert rule.trigger == "WebCoding"
    assert rule.replacement == "Web Coding"


# ---- 3. 词边界：拉丁邻字母不命中，CJK 邻字按 ASCII 类判定 ----


def test_latin_word_boundary_blocks_adjacent_letters():
    rules = [SnippetRule("web", "网页")]
    assert apply_snippets("website", rules).text == "website"
    assert apply_snippets("abcweb", rules).text == "abcweb"
    assert apply_snippets("webs", rules).text == "webs"
    # 真正的独立词照常命中
    assert apply_snippets("a web b", rules).text == "a 网页 b"
    assert apply_snippets("请上 web 睇", rules).text == "请上 网页 睇"


def test_latin_boundary_also_blocks_adjacent_digits():
    rules = [SnippetRule("web", "网页")]
    assert apply_snippets("web2", rules).text == "web2"
    assert apply_snippets("2web", rules).text == "2web"


def test_cjk_neighbour_is_ascii_boundary_only():
    # ASCII lookaround 的刻意取舍：CJK 邻字**不**阻挡命中（否则中文句内的 snippet
    # 永不生效）。CJK 触发词在句中出现即命中。
    rules = [SnippetRule("单后", "单号")]
    assert apply_snippets("我个单后系咩", rules).text == "我个单号系咩"
    assert apply_snippets("查下单后", rules).text == "查下单号"
    # 拉丁/数字邻字仍被边界挡住（ASCII 类生效的证据）
    assert apply_snippets("abc单后", rules).text == "abc单后"
    assert apply_snippets("单后abc", rules).text == "单后abc"


def test_task_spec_example_embedded_cjk_matches_by_design():
    """规格任务书要求「trigger 京东 / 文本 南京东京 不命中」——与 ASCII lookaround 机制冲突。

    plan §26.2-E1 与 §26.3 参数表把边界机制单点钉为 ``(?<![a-zA-Z0-9])``：CJK 既不在
    该类内，邻字就不构成阻挡；若改成「CJK 也阻挡」，中文句内的 snippet（如「我个**单后**系」）
    会整批失效——与 E1 的立项目的（治热词 bias 拉不动的硬混淆）直接冲突。本实现以机制
    规格为准，此处钉住真实行为供审查；偏差已写进交付报告。
    """
    rules = [SnippetRule("京东", "某东")]
    assert apply_snippets("南京东京", rules).text == "南某东京"
    assert apply_snippets("南京东京", rules).applied == [("京东", "某东")]


# ---- 4. 串行链式（前条输出 = 后条输入） ----


def test_rules_chain_serially():
    rules = [SnippetRule("单后", "单号"), SnippetRule("单号", "运单号")]
    out, applied = apply_snippets("我个单后", rules)
    assert out == "我个运单号"
    assert applied == [("单后", "单号"), ("单号", "运单号")]


def test_chain_is_ordered_by_rule_sequence():
    # 反序传入：第二条先跑，链式结果不同（顺序即语义）。
    rules = [SnippetRule("单号", "运单号"), SnippetRule("单后", "单号")]
    out, applied = apply_snippets("我个单后", rules)
    assert out == "我个单号"
    assert applied == [("单后", "单号")]


def test_single_rule_replaces_all_occurrences():
    rules = [SnippetRule("单后", "单号")]
    out, _ = apply_snippets("单后同埋单后", rules)
    assert out == "单号同埋单号"


# ---- 5. 数字铁律 ----


@pytest.mark.parametrize(
    "rule",
    [
        SnippetRule("3Q", "三Q"),
        SnippetRule("单后", "单号123"),
        SnippetRule("３Q", "三Q"),  # 全角数字 trigger
        SnippetRule("单后", "单号１２"),  # 全角数字 replacement
        SnippetRule("单0后", "单号"),
    ],
)
def test_digits_rejected(rule):
    assert validate_rule(rule) == "digits"


def test_digits_rules_skipped_others_still_compile():
    bad_trigger = SnippetRule("3Q", "三Q", source="builtin")
    bad_replacement = SnippetRule("单后", "单号123", source="account")
    good = SnippetRule("单后", "单号", source="template")
    report = compile_rules_with_skipped([bad_trigger, bad_replacement, good])
    assert [c.trigger for c in report.compiled] == ["单后"]
    assert [s.reason for s in report.skipped] == ["digits", "digits"]
    assert [s.rule.source for s in report.skipped] == ["builtin", "account"]
    assert compile_rules([bad_trigger, bad_replacement, good]) == list(report.compiled)


def test_digits_rule_never_touches_number_string():
    # 客户报的数字串一字不动：被拒规则整条不参与替换。
    rules = [SnippetRule("12345", "67890"), SnippetRule("单后", "单号")]
    out, applied = apply_snippets("我个单后系12345", rules)
    assert out == "我个单号系12345"
    assert applied == [("单后", "单号")]


# ---- 6. 空 trigger 铁律 + 长度护栏 ----


@pytest.mark.parametrize("trigger", ["", "   ", "\t", "\u3000", "\u00a0"])
def test_empty_trigger_rejected(trigger):
    assert validate_rule(SnippetRule(trigger, "单号")) == "empty_trigger"


def test_empty_trigger_skipped_and_others_applied():
    report = compile_rules_with_skipped([SnippetRule("", "X"), SnippetRule("单后", "单号")])
    assert [c.trigger for c in report.compiled] == ["单后"]
    assert [s.reason for s in report.skipped] == ["empty_trigger"]
    assert apply_snippets("单后", [SnippetRule("  ", "X"), SnippetRule("单后", "单号")]).text == "单号"


def test_length_guardrails():
    assert validate_rule(SnippetRule("a" * MAX_TRIGGER_CHARS, "x")) == ""
    assert validate_rule(SnippetRule("a" * (MAX_TRIGGER_CHARS + 1), "x")) == "trigger_too_long"
    assert validate_rule(SnippetRule("单后", "x" * MAX_REPLACEMENT_CHARS)) == ""
    assert validate_rule(SnippetRule("单后", "x" * (MAX_REPLACEMENT_CHARS + 1))) == "replacement_too_long"


def test_length_measured_after_whitespace_strip():
    # 归一去空白后只剩 17 字 → 合法（原始串长 34 不算超限）。
    trigger = "a " * 17
    assert len(trigger) > MAX_TRIGGER_CHARS
    assert validate_rule(SnippetRule(trigger, "x")) == ""


def test_oversized_rule_skipped():
    report = compile_rules_with_skipped([SnippetRule("a" * 40, "x"), SnippetRule("单后", "单号")])
    assert [c.trigger for c in report.compiled] == ["单后"]
    assert [s.reason for s in report.skipped] == ["trigger_too_long"]


def test_single_char_trigger_rejected():
    # 审查补的第三条铁律：单字 trigger 在 CJK 邻字可命中的机制下 = 全句该字皆被替换
    # （「南京东京」类的极端面），与 §26-E6「单汉字替换永不生成全局映射」同款纪律。
    assert validate_rule(SnippetRule("倉", "仓库")) == "trigger_too_short"
    assert validate_rule(SnippetRule("a", "b")) == "trigger_too_short"
    report = compile_rules_with_skipped([SnippetRule("倉", "仓库"), SnippetRule("单后", "单号")])
    assert [c.trigger for c in report.compiled] == ["单后"]
    assert [s.reason for s in report.skipped] == ["trigger_too_short"]


def test_two_char_trigger_is_the_floor():
    assert validate_rule(SnippetRule("单后", "单号")) == ""


# ---- 7. applied 记账 + 替换值不展开模板 ----


def test_unmatched_rule_not_recorded():
    out, applied = apply_snippets("唔关事", [SnippetRule("单后", "单号")])
    assert out == "唔关事"
    assert applied == []


def test_applied_only_records_hits_in_chain():
    rules = [SnippetRule("单后", "单号"), SnippetRule("无中生有", "有中生无")]
    out, applied = apply_snippets("我个单后", rules)
    assert out == "我个单号"
    assert applied == [("单后", "单号")]


@pytest.mark.parametrize("replacement", ["a$b\\c", "x$&y", "$x", "\\g<x>"])
def test_replacement_is_literal_not_template(replacement):
    out, applied = apply_snippets("单后", [SnippetRule("单后", replacement)])
    assert out == replacement
    assert applied == [("单后", replacement)]


def test_application_is_named_tuple():
    result = apply_snippets("单后", [SnippetRule("单后", "单号")])
    assert isinstance(result, SnippetApplication)
    text, applied = result
    assert text == "单号"
    assert applied == [("单后", "单号")]


# ---- 8. merge 覆盖语义与去重 ----


def test_merge_later_list_overrides_value():
    merged = merge_rules(
        [SnippetRule("单后", "单号", source="builtin")],
        [SnippetRule("单后", "运单号", source="template")],
    )
    assert len(merged) == 1
    assert merged[0].replacement == "运单号"
    assert merged[0].source == "template"


def test_merge_dedupes_case_and_space_variants():
    merged = merge_rules(
        [SnippetRule("Web Coding", "A")],
        [SnippetRule("webcoding", "B")],
    )
    assert len(merged) == 1
    assert merged[0].trigger == "webcoding"
    assert merged[0].replacement == "B"


def test_merge_keeps_first_position_and_three_layer_order():
    builtin = [SnippetRule("单后", "单号", "builtin"), SnippetRule("呃人", "骗人", "builtin")]
    account = [SnippetRule("点样", "怎样", "account")]
    template = [SnippetRule("单后", "底单号", "template")]
    merged = merge_rules(builtin, account, template)
    assert [(r.trigger, r.replacement, r.source) for r in merged] == [
        ("单后", "底单号", "template"),  # 位置=首次出现，值=最后定义
        ("呃人", "骗人", "builtin"),
        ("点样", "怎样", "account"),
    ]


def test_merge_accepts_json_mappings_and_skips_garbage():
    merged = merge_rules(
        [{"trigger": "单后", "replacement": "单号"}, None, "x", {"replacement": "y"}],
        [{"trigger": "点样", "replacement": "怎样", "source": "account"}],
    )
    assert [(r.trigger, r.replacement, r.source) for r in merged] == [
        ("单后", "单号", ""),
        ("点样", "怎样", "account"),
    ]


def test_merge_empty_lists():
    assert merge_rules() == []
    assert merge_rules(None, [], None) == []


def test_merge_output_feeds_apply():
    merged = merge_rules([SnippetRule("单后", "单号")], [SnippetRule("单后", "运单号")])
    assert apply_snippets("我个单后", merged).text == "我个运单号"


# ---- 9. 宽容解析 / 严格校验的分工 ----


def test_parse_rule_tolerance():
    rule = SnippetRule("单后", "单号")
    assert parse_rule(rule) is rule
    assert parse_rule({"trigger": "单后", "replacement": "单号"}) == rule
    assert parse_rule({"trigger": "单后", "replacement": "单号", "source": "acct"}) == SnippetRule(
        "单后", "单号", "acct"
    )
    assert parse_rule({"replacement": "单号"}) is None
    assert parse_rule({"trigger": "单后"}) is None
    assert parse_rule(None) is None
    assert parse_rule("单后") is None


def test_compile_skips_bad_rule_shapes():
    report = compile_rules_with_skipped([None, 42, {"trigger": "单后"}, SnippetRule("单后", "单号")])
    assert [c.trigger for c in report.compiled] == ["单后"]
    assert [s.reason for s in report.skipped] == ["bad_rule"] * 3


def test_compile_rules_returns_compiled_rule_objects():
    compiled = compile_rules([{"trigger": "单后", "replacement": "单号"}])
    assert len(compiled) == 1
    assert isinstance(compiled[0], CompiledRule)
    assert compiled[0].trigger == "单后"
    assert compiled[0].replacement == "单号"


def test_compile_does_not_dedupe():
    # 去重是 merge_rules 的唯一职责；compile 原样编译（重复项会各跑一遍）。
    compiled = compile_rules([SnippetRule("单后", "单号"), SnippetRule("单 后", "运单号")])
    assert len(compiled) == 2


def test_normalize_key():
    assert normalize_key("Web  Coding") == "webcoding"
    assert normalize_key("　单 后　") == "单后"


def test_rule_is_frozen():
    rule = SnippetRule("单后", "单号")
    with pytest.raises(Exception):
        rule.trigger = "改唔到"  # type: ignore[misc]


# ---- 10. 域内端到端样例（无数字） ----


def test_domain_example_asr_confusion_repair():
    rules = merge_rules(
        [SnippetRule("单后", "单号", "builtin"), SnippetRule("点样", "怎样", "builtin")],
        [SnippetRule("呃人", "骗人", "account")],
        [SnippetRule("单后", "快递单号", "template")],
    )
    out, applied = apply_snippets("我个单后点样查，你哋呃人咩", rules)
    assert out == "我个快递单号怎样查，你哋骗人咩"
    assert applied == [("单后", "快递单号"), ("点样", "怎样"), ("呃人", "骗人")]
