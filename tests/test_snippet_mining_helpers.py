"""snippet_seed_mining.py 的离线纯函数自检（2026-09-21 批次 3）。

钉住面（不经真栈、不碰业务库/LLM 端点）：
- 词表 dump 行判定 `looks_like_hotword_dump`：dump 行必须识别、报号行/真话必须放行
  （批次 3 实弹发现：挖掘语料的证据行被 dump 行污染，会让 E1 候选变成 dump 伪影）；
- 语言分域三档 `decide_lang_scope`（fail-safe：宁可漏改、不可改坏）；
- 变体卫生 `is_malformed_variant` / 反向安全 `reverse_safety_reason` 的基本面；
- 生成端白名单守卫 `llm_allow`（换档 9B 也必须过闸，不是「白名单外随便填」）；
- 分域候选的 `lang` 值能过 snippets 严格轨（两个模块的契约对齐）。

术语：语言字面量只用 zh / cantonese / en（AGENTS.md 术语铁律）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "snippet_seed_mining", _ROOT / "scripts" / "snippet_seed_mining.py"
)
ssm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ssm)

sys.path.insert(0, str(_ROOT / "packages" / "core"))

from bok_voice_core.snippets import SnippetRule, validate_rule  # noqa: E402


# ---- 词表 dump 行判定 ---------------------------------------------------------

_DUMP_ROWS = [
    # 纯 dump（繁体词表原样抄出）
    "單號，運單，賠償，運費，專員，集運，時效，上門，追蹤，核實，WhatsApp，微信，顺丰速递",
    # dump + 真话前缀（前缀不改变整行仍是 dump 的事实）
    "什么货啊？顺豐速運，運通，理賠，京東，拼多多，单号，运单，赔偿，运费，专员，"
    "集运，时效，上门，追踪，核实，微信，顺丰物流。",
    "记得了。顺豐速運，運通，理賠，京東，拼多多，單號，運單，賠償，運費，專員，集運",
]


@pytest.mark.parametrize("row", _DUMP_ROWS)
def test_dump_rows_detected(row):
    assert ssm.looks_like_hotword_dump(row) is True


_REAL_ROWS = [
    "我想問下集運幾時到",  # 普通短句（token 少）
    "我個單號係一二三，唔該你幫我查下，聽日送到",  # 含逗号真话（长块拉低密度）
    "WhatsApp係一、二、三、四、五、六、七。係。",  # 报号行：与 dump 同形但语义关键
    "你好，我係林生，我想問下我嘅快遞去咗邊度呀？",  # 正常问句
    "",  # 空行
]


@pytest.mark.parametrize("row", _REAL_ROWS)
def test_real_utterances_not_flagged_as_dump(row):
    assert ssm.looks_like_hotword_dump(row) is False


def test_numeral_tokens_excluded_from_dump_density():
    """报号行豁免的机制钉死：纯数词块不计入「短块」，故达不到短块密度。

    报号句（WhatsApp/单号捕获路径的输入）与 dump 行同形（都是「一串短块」），
    首版按「最长块 ≤N」判定时把报号行误判成 dump——本判例锁住修法。
    """
    report_row = "WhatsApp係一、二、三、四、五、六、七。係。"
    as_dump_shape = report_row.replace("一", "甲").replace("二", "乙").replace(
        "三", "丙"
    ).replace("四", "丁").replace("五", "戊").replace("六", "己").replace("七", "庚")
    assert ssm.looks_like_hotword_dump(report_row) is False
    assert ssm.looks_like_hotword_dump(as_dump_shape) is True


def test_dump_needs_min_tokens():
    # 低于 token 门槛的应承串不判 dump（避免误伤「係，好，得」这类短轮）
    assert ssm.looks_like_hotword_dump("係，好，得") is False


# ---- 语言分域三档 -------------------------------------------------------------


def test_scope_all_languages_when_no_non_zh():
    assert ssm.decide_lang_scope({"zh": 9}) == ("", "")
    assert ssm.decide_lang_scope({}) == ("", "")
    # 语言字段为空的行（旧数据/未标注）不算「非 zh」，不因此收窄
    assert ssm.decide_lang_scope({"": 5, "zh": 2}) == ("", "")


def test_scope_to_zh_when_seen_in_other_language_too():
    # 关键判例（本轮实弹数据）：繁体形态在粤语通话里是**正确写法**，
    # 占比再低也收窄——否则全局替换会改坏粤语通话的正确转写。
    assert ssm.decide_lang_scope({"zh": 5, "cantonese": 4}) == ("zh", "")
    assert ssm.decide_lang_scope({"zh": 6, "cantonese": 2}) == ("zh", "")
    assert ssm.decide_lang_scope({"zh": 20, "cantonese": 1}) == ("zh", "")
    assert ssm.decide_lang_scope({"zh": 3, "en": 1}) == ("zh", "")


def test_discard_when_only_other_language():
    # 零 zh 证据：那是别语言的正确形态，收窄成 zh 只会变成永不命中的死规则
    assert ssm.decide_lang_scope({"cantonese": 2}) == ("", "lang_context_correct")
    assert ssm.decide_lang_scope({"cantonese": 4, "en": 2}) == ("", "lang_context_correct")


def test_scoped_candidate_lang_passes_snippets_strict_rail():
    """两模块契约对齐：分域产出的 lang 必须能过 snippets 严格轨（否则白挖）。"""
    lang, reason = ssm.decide_lang_scope({"zh": 5, "cantonese": 4})
    assert (lang, reason) == ("zh", "")
    assert validate_rule(SnippetRule(trigger="專員", replacement="专员", lang=lang)) == ""


# ---- 变体卫生 / 反向安全 / 白名单 --------------------------------------------


@pytest.mark.parametrize("bad", ["赔偿→培偿", "单号 -> 单浩", "[单浩]", "单号：单浩"])
def test_malformed_variants_rejected(bad):
    assert ssm.is_malformed_variant(bad) is True


@pytest.mark.parametrize("good", ["培偿", "单浩", "集雲"])
def test_real_variants_pass_hygiene(good):
    assert ssm.is_malformed_variant(good) is False


def test_reverse_safety_reasons():
    canon = ["单号", "京东", "集运"]
    freq = {"集运", "物流", "时效"}
    assert ssm.reverse_safety_reason("单号", canon, freq) == "identity"
    assert ssm.reverse_safety_reason("我的单号", canon, freq) == "contains_canonical"
    assert ssm.reverse_safety_reason("单", canon, freq) == "substring_of_canonical"
    assert ssm.reverse_safety_reason("物流", canon, freq) == "freq_correct_word"
    assert ssm.reverse_safety_reason("", canon, freq) == "empty_trigger"
    # 真错形：不在正形/高频里 → 通过
    assert ssm.reverse_safety_reason("单浩", canon, freq) == ""


def test_reverse_safety_compacts_before_compare():
    # 匹配面与 snippets.normalize_key 同源（去空白 + lower）
    assert ssm.reverse_safety_reason("物 流", ["物流"], set()) == "identity"


def test_generation_endpoint_guard_allows_only_known_ports():
    assert ssm.llm_allow(1235) == (("127.0.0.1", 1235),)
    assert ssm.llm_allow(1237) == (("127.0.0.1", 1237),)
    for bad_port in (1234, 1236, 80, 0, 65535):
        with pytest.raises(ssm.UrlGuardError):
            ssm.llm_allow(bad_port)


def test_guard_rejects_non_http_and_foreign_host():
    with pytest.raises(ssm.UrlGuardError):
        ssm.guard_url("https://127.0.0.1:1235/v1/chat/completions", ssm.llm_allow(1235))
    with pytest.raises(ssm.UrlGuardError):
        ssm.guard_url("http://10.0.0.9:1235/v1/chat/completions", ssm.llm_allow(1235))


def test_occurrence_counting_uses_compact_form():
    assert ssm.count_occurrences(ssm._compact("我 的 单 浩 呀"), ssm._compact("单浩")) == 1
    assert ssm.count_occurrences("", "单浩") == 0
    assert ssm.count_occurrences("单浩", "") == 0
