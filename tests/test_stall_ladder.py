"""stall 升级阶梯:纯函数阈值 + 账本去重/清零(漏斗 v2 P0,spec §3.1)。"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import FlowController, stall_ladder_level  # noqa: E402


def test_level_thresholds():
    assert stall_ladder_level(0) == ""
    assert stall_ladder_level(2) == ""
    assert stall_ladder_level(3) == "degrade"
    assert stall_ladder_level(4) == "degrade"
    assert stall_ladder_level(5) == "bypass"
    assert stall_ladder_level(7) == "bypass"
    assert stall_ladder_level(8) == "close"
    assert stall_ladder_level(20) == "close"


def _fc() -> FlowController:
    return FlowController(steps=[])


def test_note_unclear_counts_and_dedupes_per_turn():
    fc = _fc()
    assert fc.note_turn_outcome("unclear", 3, "k1") == 1
    # 同轮双路(rule+judge)同 key 只计 1
    assert fc.note_turn_outcome("unclear", 3, "k1") == 1
    assert fc.note_turn_outcome("unclear", 3, "k2") == 2
    # 不同步各自计
    assert fc.note_turn_outcome("unclear", 4, "k3") == 1


def test_question_neutral_objection_resets():
    fc = _fc()
    fc.note_turn_outcome("unclear", 3, "k1")
    fc.note_turn_outcome("unclear", 3, "k2")
    # QUESTION 中性:唔计唔清(否则 judge 攒的 streak 被提问轮抹平,阶梯失效)
    assert fc.note_turn_outcome("question", 3, "k3") == 2
    # 决定性 verdict(异议)先清零
    assert fc.note_turn_outcome("objection", 3, "k4") == 0


def test_advance_clears_streak():
    fc = _fc()
    fc.note_turn_outcome("unclear", 0, "k1")
    fc.note_turn_outcome("unclear", 0, "k2")
    fc.advance()
    assert fc.step_streak.get(0, 0) == 0


def test_ladder_line_three_langs():
    from agent_runtime.agent import _stall_ladder_line  # noqa: E402
    for lang in ("zh", "cantonese", "en"):
        for level in ("degrade", "bypass"):
            assert _stall_ladder_line(lang, level)
