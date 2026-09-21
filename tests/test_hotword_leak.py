"""E2 热词泄漏清洗器单测（type4me 判例 + 我们两处有意偏差）。

契约见 ``bok_voice_core.hotword_leak`` 模块 docstring 与计划档
``docs/superpowers/plans/2026-09-21-a-line-speed-asr-decision-verification.md``
§26.2-E2。覆盖：剥标签 / 最长连续热词前缀（词数优先、并列比消费长度）/ 泄漏
两档判据（标签或 ≥2 词 / 单词命中四条件含 CJK 护栏）/ 分隔符容忍 / 大小写
不敏感 / 偏差①纯 dump 丢弃 / 偏差②无 fallback 单词不误判。
"""

from __future__ import annotations

from pathlib import Path

from bok_voice_core.hotword_leak import contains_cjk, sanitize

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "core"
    / "bok_voice_core"
    / "hotword_leak.py"
)


# ---- type4me 判例（对照其硬拒绝清单） ----


def test_single_word_leak_returns_matching_remainder() -> None:
    # type4me 判例：单词命中 + remainder 与 fallback 相等 → 保 remainder。
    assert sanitize("一二三四三字", ["一二三四"], "三字") == "三字"


def test_label_with_two_words_cleans_without_fallback() -> None:
    # type4me 判例：标签 ≥1 词命中即泄漏（标签法不需要 fallback）。
    assert (
        sanitize("Vocabulary: OpenAI, Qwen, hello world", ["OpenAI", "Qwen"])
        == "hello world"
    )


def test_pure_hotword_dump_is_dropped() -> None:
    # 偏差①：type4me 原样保留纯 dump，我们整段丢弃 → ""（上游走静音分支）。
    assert sanitize("OpenAI Qwen Claude", ["OpenAI", "Qwen", "Claude"]) == ""


def test_pure_hotword_dump_dropped_even_with_fallback() -> None:
    # 偏差①与 fallback 无关：remainder 空即丢弃。
    assert sanitize("OpenAI Qwen Claude", ["OpenAI", "Qwen", "Claude"], "你好") == ""


def test_single_word_without_fallback_kept() -> None:
    # 偏差②：无 fallback 时单词命中不误判（与原版一致）——呼叫方应尽量传。
    assert sanitize("一二三四三字", ["一二三四"]) == "一二三四三字"


# ---- CJK 护栏：形状同构，仅 consumed 是否含 CJK 决定 ----


def test_single_word_cjk_consumed_is_cleaned() -> None:
    assert sanitize("京东 你好", ["京东"], "你好") == "你好"


def test_single_word_non_cjk_consumed_not_cleaned() -> None:
    # 与上一例形状完全同构（remainder==fallback、prefix guard 过关），
    # 但 consumed "OpenAI" 无 CJK → 不算泄漏。
    assert sanitize("OpenAI hello", ["OpenAI"], "hello") == "OpenAI hello"


def test_non_cjk_spec_sample_kept() -> None:
    # 规格原例：consumed "Claude" 无 CJK、remainder 与 fallback 无关 → 原文返回。
    assert (
        sanitize("Claude, OpenAI, unrelated", ["Claude"], "真实内容")
        == "Claude, OpenAI, unrelated"
    )


# ---- 最长连续热词前缀择优 ----


def test_longest_prefix_wins_by_consumed_length() -> None:
    # 短词排在前，但更长消费的起始下标仍胜出（不会停在短词）。
    assert sanitize("一二三四五", ["一二", "一二三四"], "五") == "五"


def test_longest_prefix_order_independent() -> None:
    # 换词表顺序 → 结果不变（所有起始下标都试）。
    assert sanitize("一二三四五", ["一二三四", "一二"], "五") == "五"


def test_longest_prefix_prefers_more_words() -> None:
    # 先比命中词数：2 词（消费 5 字符）胜过任何 1 词（消费 6 字符）。
    assert sanitize("ab cd rest", ["ab", "cd", "abcdef"]) == "rest"


# ---- 分隔符容忍 / 大小写不敏感 ----


def test_separator_tolerance_between_words() -> None:
    # 全角逗号分隔：仍命中两词 → 判泄漏 → 保 remainder。
    assert sanitize("OpenAI，Qwen 你好", ["OpenAI", "Qwen"]) == "你好"


def test_case_insensitive_matching() -> None:
    assert sanitize("OPENAI qwen 你好", ["OpenAI", "Qwen"]) == "你好"


# ---- fallback 前缀护栏 / 无关 remainder ----


def test_fallback_starting_with_hotword_is_not_leak() -> None:
    # consumed "一二" 整段是 fallback "一二三" 的前缀 → 热词是客户真实所说 → 不判泄漏。
    assert sanitize("一二三", ["一二"], "一二三") == "一二三"


def test_remainder_unrelated_to_fallback_kept() -> None:
    # 单词命中但 remainder 与 fallback 无关 → 拿不准就不动（返回原文）。
    assert sanitize("一二三四新内容", ["一二三四"], "完全无关") == "一二三四新内容"


# ---- 标签顺序 / 冒号不残留 ----


def test_label_stripped_leaves_no_colon() -> None:
    out = sanitize("Vocabulary: hello world", ["OpenAI"])
    assert out == "hello world"
    assert ":" not in out


def test_fullwidth_colon_label() -> None:
    # 全角冒号标签 + 两词命中 → 剥标签取 remainder。
    assert sanitize("词汇：OpenAI，Qwen 你好", ["OpenAI", "Qwen"]) == "你好"


def test_bare_and_ascii_colon_labels() -> None:
    assert sanitize("Hotwords: A B 你好", ["A", "B"]) == "你好"
    # 单字 CJK 热词：单词命中（consumed 含 CJK）+ remainder==fallback → 保 remainder。
    assert sanitize("关键词:京 你好", ["京"], "你好") == "你好"


# ---- 无词表 / 空输入 / 辅助函数 ----


def test_empty_input_returns_empty() -> None:
    assert sanitize("   ", ["OpenAI"]) == ""
    assert sanitize("", ["OpenAI"]) == ""


def test_no_hotwords_returns_uncleaned() -> None:
    assert sanitize("Vocabulary: OpenAI Qwen", []) == "Vocabulary: OpenAI Qwen"
    assert sanitize("Vocabulary: OpenAI Qwen", ["", "  "]) == "Vocabulary: OpenAI Qwen"


def test_contains_cjk_ranges() -> None:
    assert contains_cjk("一二三")
    assert contains_cjk("a北b")
    assert not contains_cjk("abc 123")
    assert not contains_cjk("")


# ---- 术语门禁（本地自证；全仓门禁见 tests/test_cantonese_terminology.py） ----


def test_no_legacy_cantonese_spelling_in_module() -> None:
    text = _MODULE_PATH.read_text(encoding="utf-8").lower()
    # 拼出来比对：本文件自身不能出现旧拼写字面量（术语门禁扫已跟踪文件，落库即红）。
    banned = "y" + "ue"
    assert banned not in text
