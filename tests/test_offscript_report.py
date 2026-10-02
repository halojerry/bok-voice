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


# ---- G7 快路覆盖实测面（qa_fastpath 占回复轮比例,2026-09-25） ----


def test_qa_fastpath_stats_denominator_and_rate():
    turns = [
        {"role": "user", "transcript": "幾時送到", "gen": ""},
        {"role": "assistant", "transcript": "兩到三日到。", "gen": "qa_fastpath"},
        {"role": "assistant", "transcript": "", "gen": "filler"},  # 垫话账本：不进分母
        {"role": "user", "transcript": "可以退貨嗎", "gen": ""},
        {"role": "assistant", "transcript": "七日內。", "gen": "llm"},
        {"role": "assistant", "transcript": "…", "gen": "interrupted"},  # 打断残行：不进分母
        {"role": "user", "transcript": "轉人工", "gen": ""},
        # 旧数据 gen 空 ×2：按 llm 归桶、照进分母
        {"role": "assistant", "transcript": "我幫你轉接。", "gen": ""},
        {"role": "assistant", "transcript": "好嘅。", "gen": "script"},
    ]
    fs = off.qa_fastpath_stats(turns)
    assert fs["reply_turns"] == 4
    assert fs["qa_fastpath_turns"] == 1
    assert fs["qa_fastpath_rate"] == round(1 / 4, 4)
    assert fs["by_gen"] == {"qa_fastpath": 1, "llm": 2, "script": 1}


def test_qa_fastpath_stats_empty_turns_zero_denominator():
    fs = off.qa_fastpath_stats([])
    assert fs == {"reply_turns": 0, "qa_fastpath_turns": 0, "qa_fastpath_rate": 0.0, "by_gen": {}}
    only_ledger = off.qa_fastpath_stats([{"role": "assistant", "gen": "filler"}])
    assert only_ledger["reply_turns"] == 0 and only_ledger["qa_fastpath_rate"] == 0.0


def test_summarize_all_aggregates_fastpath_stats():
    budgets = {"first_ms": 2500.0, "perceived_ms": 3000.0}
    base = {
        "measures": [],
        "perceived": [],
        "summary": {"rounds": 0, "answered": 0, "mute": 0, "markers": {}},
        "gen_counts": {},
        "setup_ok": True,
    }
    results = [
        {**base, "fastpath_stats": {"reply_turns": 4, "qa_fastpath_turns": 1, "qa_fastpath_rate": 0.25}},
        {**base, "fastpath_stats": {"reply_turns": 6, "qa_fastpath_turns": 2, "qa_fastpath_rate": 1 / 3}},
        dict(base),  # 旧结果无 fastpath_stats：聚合零贡献不炸
    ]
    agg = off.summarize_all(results, budgets)
    assert agg["fastpath_stats"]["reply_turns"] == 10
    assert agg["fastpath_stats"]["qa_fastpath_turns"] == 3
    assert agg["fastpath_stats"]["qa_fastpath_rate"] == 0.3
