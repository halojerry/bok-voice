"""延迟测试台报告纯函数(2026-09-17):百分位/拆轮分配/聚合汇总。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from probe_latency_soak import (  # noqa: E402
    assign_user_turns,
    percentile,
    split_pcm,
    summarize_report,
)


def test_percentile_nearest():
    assert percentile([], 50) is None
    assert percentile([100.0], 50) == 100.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0
    assert percentile([10.0, 20.0, 30.0], 0) == 10.0


def test_split_pcm_frame_aligned():
    pcm = b"\x01\x00" * 16000  # 1s
    parts = split_pcm(pcm, gap_s=0.6)
    assert len(parts) == 3
    assert parts[1] == b"\x00" * (int(16000 * 0.6) * 2)
    assert len(parts[0]) % 640 == 0
    assert len(parts[0]) + len(parts[2]) == len(pcm)


def test_assign_user_turns_single():
    counts = assign_user_turns(["你好", "我個件遲咗"], ["你好", "我個件遲咗成個禮拜"])
    assert counts == [1, 1]


def test_assign_user_turns_split_detected():
    # 拆段轮(multi=True)一句被收成两轮:count=2 → 拆轮旗。
    counts = assign_user_turns(
        ["我個件上個禮拜寄出"],
        ["我個件上個禮拜", "寄出咗"],
        multi=[True],
    )
    assert counts == [2]


def test_assign_user_turns_normal_round_never_multi():
    # 非 multi 轮多吃行=唔可能:额外行(回声/串轮)唔分配,拆轮旗零误报。
    counts = assign_user_turns(["你好"], ["你好", "另外"])
    assert counts == [1]


def test_assign_user_turns_asr_rewrite_fallback():
    # 轮1 被回声隐藏(冇落行)/ASR 面目全非 → count=0 报「未对齐」,游标唔消耗,
    # 后续轮照常锚定(首跑实证:「你好」被回声守卫隐藏,旧行分配法从此串位)。
    counts = assign_user_turns(["你好", "幾時處理得好"], ["哈喽", "幾時處理得好"])
    assert counts == [0, 1]


def test_assign_user_turns_cap_four():
    rows = ["你好", "x1", "x2", "x3", "x4", "x5"]
    counts = assign_user_turns(["你好"], rows, multi=[True])
    assert counts == [4]  # 拆段轮 4 行封顶


def test_assign_user_turns_multi_stop_on_same_prefix():
    # multi 轮续吃时撞同款开头(下一轮同文) → 停手,唔吞下一轮。
    counts = assign_user_turns(["單號係八六五", "單號係八六五"], ["單號係八六五", "單號係八六五"], multi=[True, False])
    assert counts == [1, 1]


def test_summarize_report_counts():
    measures = [
        {"first_audio_ms": 1200.0, "answered": True, "op": "normal", "text": "你好", "user_turns": 1},
        {"first_audio_ms": 3100.0, "answered": True, "op": "normal", "text": "怎么赔偿", "user_turns": 1},
        {"first_audio_ms": None, "answered": False, "op": "split", "text": "我的件", "user_turns": 2},
    ]
    perceived = [{"total": 1800}, {"total": 3500}]
    counts = {"[storm] engage": 1, "QA_FASTPATH hit=1": 2}
    budgets = {"first_ms": 2500.0, "perceived_ms": 3000.0}
    s = summarize_report(measures, perceived, counts, budgets)
    assert s["rounds"] == 3
    assert s["mute"] == 1
    assert s["first_audio"]["n"] == 2
    assert s["first_audio"]["over_budget"] == 1
    assert s["perceived"]["over_budget"] == 1
    assert s["perceived"]["max"] == 3500
    assert s["markers"]["[storm] engage"] == 1
