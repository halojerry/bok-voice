"""话术外问题集锦探针纯函数（2026-09-17）：应答归属/质量旗/跨通聚合。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_offscript_soak as off  # noqa: E402


def test_attribute_replies_pairs_by_quota():
    turns = [
        {"role": "user", "transcript": "你好邊位呀", "provider": ""},
        {"role": "assistant", "transcript": "你好，我係……", "gen": "llm"},
        {"role": "user", "transcript": "你係咪詐騙集團嚟㗎", "provider": ""},
        {"role": "assistant", "transcript": "唔好意思令你有咁嘅疑慮", "gen": "llm"},
        {"role": "user", "transcript": "我個件上個禮拜寄出", "provider": ""},
        {"role": "user", "transcript": "禮拜祭出", "provider": ""},  # 拆段第二行
        {"role": "assistant", "transcript": "明白，我幫你記低", "gen": "script"},
    ]
    rounds = off.attribute_replies(turns, [1, 1, 2])
    assert rounds[0]["user_texts"] == ["你好邊位呀"]
    assert rounds[0]["assistant_texts"] == ["你好，我係……"]
    assert rounds[1]["assistant_texts"] == ["唔好意思令你有咁嘅疑慮"]
    assert rounds[2]["user_texts"] == ["我個件上個禮拜寄出", "禮拜祭出"]
    assert rounds[2]["assistant_texts"] == ["明白，我幫你記低"]


def test_attribute_replies_mech_rows_do_not_advance():
    turns = [
        {"role": "user", "transcript": "你先講", "provider": "storm-listen"},
        {"role": "user", "transcript": "你好", "provider": ""},
        {"role": "assistant", "transcript": "你好呀", "gen": "script"},
    ]
    rounds = off.attribute_replies(turns, [1, 1])
    # 机制行唔占客户口：配额 1 由「你好」吃满才进轮 2。
    assert rounds[0]["user_texts"] == ["你好"]
    assert rounds[0]["assistant_texts"] == ["你好呀"]
    assert rounds[1]["assistant_texts"] == []


def test_attribute_replies_unaligned_round_does_not_advance():
    turns = [
        {"role": "user", "transcript": "完全认唔出嘅转写", "provider": ""},
        {"role": "assistant", "transcript": "回应A", "gen": "llm"},
        {"role": "assistant", "transcript": "回应B", "gen": "script"},
    ]
    rounds = off.attribute_replies(turns, [0, 1])
    # 轮 0 未对齐（count=0）→ 游标停喺轮 0，后续应答堆轮 0，唔会漏计。
    assert rounds[0]["assistant_texts"] == ["回应A", "回应B"]
    assert rounds[1]["assistant_texts"] == []


def test_reply_quality_flags_empty_and_repeat():
    rounds = [
        {"assistant_texts": []},
        {"assistant_texts": ["好的明白，马上帮你处理。"]},
        {"assistant_texts": ["好的明白，马上帮你处理！"]},  # 仅标点差=整句复读
        {"assistant_texts": ["不同的回答内容"]},
    ]
    flags = off.reply_quality_flags(rounds)
    assert flags == {"empty_reply": 1, "repeat_reply": 1}


def test_summarize_all_aggregates():
    mk = lambda v: {k: v for k in off.SENTINEL_KEYS}
    results = [
        {
            "measures": [{"first_audio_ms": 1000.0, "answered": True},
                         {"first_audio_ms": None, "answered": False}],
            "perceived": [{"total": 2200}],
            "summary": {"rounds": 2, "answered": 1, "mute": 1,
                        "markers": {k: (3 if k == "BOK_FILLER fired" else 0) for k in off.SENTINEL_KEYS}},
            "gen_counts": {"llm": 1, "script": 1},
            "setup_ok": True,
        },
    ] * 2
    budgets = {"first_ms": 2500.0, "perceived_ms": 3000.0}
    agg = off.summarize_all(results, budgets)
    assert agg["calls"] == 2 and agg["rounds"] == 4
    assert agg["mute"] == 2 and agg["answered"] == 2
    assert agg["first_audio"]["n"] == 2 and agg["first_audio"]["p50"] == 1000.0
    assert agg["markers"]["BOK_FILLER fired"] == 6
    assert agg["gen_counts"] == {"llm": 2, "script": 2}
