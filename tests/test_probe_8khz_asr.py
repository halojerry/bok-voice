"""8kHz 窄带探针的判定口径（spec 2026-09-13 §6 门禁的数学部分）。

探针本体要真栈才跑得起来，但「转写→准确率/号码逐位」这层是纯函数，且**直接决定
门禁过不过**——先钉住它，避免探针在真实数据上算错分（假绿/假红）。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

# 会引入 TTS 内部停顿的标点（空格不算：英文靠空格分词）。
_PAUSE_PUNCT_RE = re.compile(r"[。，,．.！!？?～~—、；;：:'\"“”‘’()（）\[\]{}]")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_8khz_asr.py"
_spec = importlib.util.spec_from_file_location("probe_8khz_asr", _SCRIPT)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
sys.modules["probe_8khz_asr"] = probe
_spec.loader.exec_module(probe)


def test_normalize_strips_punctuation_and_space():
    assert probe.normalize_text("你好，我是 快递公司的专员。") == "你好我是快递公司的专员"
    assert probe.normalize_text("Let me check that for you.") == "Letmecheckthatforyou"


def test_char_accuracy_full_and_partial():
    assert probe.char_accuracy("你好我是快递公司的专员", "你好我是快递公司的专员") == 1.0
    # 只认字面：多字/少字按 difflib 打折，不会因为「意思对」给满分。
    assert probe.char_accuracy("你好我是快递公司", "你好我是快递公司的专员") < 1.0
    assert probe.char_accuracy("", "你好") == 0.0
    assert probe.char_accuracy("随便", "") == 0.0  # 空台词不给满分（防除零假绿）


def test_char_accuracy_ignores_punctuation_only():
    assert probe.char_accuracy("你好，我是快递公司的专员。", "你好我是快递公司的专员") == 1.0


def test_digits_of_covers_chinese_english_and_arabic():
    assert probe.digits_of("六四三二零一一一") == "64320111"
    assert probe.digits_of("我WhatsApp係六四三二零一一一") == "64320111"
    assert probe.digits_of("6 4 3 2 0 1 1 1") == "64320111"
    assert probe.digits_of("six four three two zero one one one") == "64320111"
    # 全角数字（ASR 偶发，照 flow._digit_normalize 语义收）。
    assert probe.digits_of("６４３２０１１１") == "64320111"


def test_digits_of_keeps_order_and_non_digits_irrelevant():
    assert probe.digits_of("好的，我嘅號碼係 64320111 呀") == "64320111"


def test_customer_transcripts_filters_speaker_and_blanks():
    turns = [
        {"speaker": "customer", "transcript": "你好我是快递公司的专员"},
        {"speaker": "agent_ai", "transcript": "请问方便讲电话吗"},
        {"speaker": "customer", "transcript": "   "},
        {"speaker": "customer", "transcript": "好的再见"},
    ]
    assert probe.customer_transcripts(turns) == ["你好我是快递公司的专员", "好的再见"]


# brief Task 5 指定的 zh 台词（11 个汉字，但无标点 → TTS 渲染无内部停顿；
# vad-pause 提交门槛 10 字，碎片都低于门槛不会被当整轮提交，故可放行）。
BRIEF_ZH_LINE = "你好我是快递公司的专员"


def test_legs_contract_single_breath_and_digits_target():
    """台词铁律：单口气（中文 ≤10 字 / 英文 ≤6 词，无标点停顿），号码句目标钉死。"""
    by_name = {leg.name: leg for leg in probe.DEFAULT_LEGS}
    assert by_name["cantonese-number"].digits == "64320111"
    for leg in probe.DEFAULT_LEGS:
        for line in leg.lines:
            assert not _PAUSE_PUNCT_RE.search(line), f"{leg.name} 台词带停顿标点: {line!r}"
            cjk = sum(1 for ch in line if "\u4e00" <= ch <= "\u9fff")
            if cjk:
                if line == BRIEF_ZH_LINE:
                    continue  # brief 指定台词，单点豁免（见上）
                assert cjk <= 10, f"{leg.name} 中文台词超单口气门限: {line!r} ({cjk} 字)"
            else:
                assert len(line.split()) <= 6, f"{leg.name} 英文台词过长: {line!r}"
    # 三语句腿（不含号码腿）是门禁分母；号码腿单列逐位比对。
    assert [leg.name for leg in probe.DEFAULT_LEGS if leg.sentence] == [
        "zh-sentence", "en-sentence", "hotword"]


def test_every_language_has_probe_template_steps():
    assert set(probe.PROBE_STEPS) == {"zh", "cantonese", "en"}
    for lang, steps in probe.PROBE_STEPS.items():
        assert len(steps) >= 2, lang
        assert all(step.get("ref") and step.get("say") for step in steps), lang
