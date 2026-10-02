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


# ---- 实答抵销(2026-09-28 多轮卡死实证:call-bbf700a4/call-2d9b35e9) ----
# 健康问答(客户连续提问、AI 连续作答,verdict 全 UNCLEAR/QUESTION)旧账本
# 会静默攒 streak 到 3,下一轮降级问法顶替真答案(「件到哪」被回「答个是或
# 不是」),degrade 连发两轮再落 bypass——多轮对话卡死体感的主源。


def test_relieve_stall_streak_decrements_never_below_zero():
    fc = _fc()
    # 空账本抵销:不产生负数、不新建条目
    fc.relieve_stall_streak()
    assert fc.step_streak.get(0, 0) == 0
    fc.note_turn_outcome("unclear", 0, "k1")
    fc.note_turn_outcome("unclear", 0, "k2")
    assert fc.step_streak.get(0) == 2
    fc.relieve_stall_streak()
    assert fc.step_streak.get(0) == 1
    fc.relieve_stall_streak()
    assert fc.step_streak.get(0) == 0
    fc.relieve_stall_streak()
    assert fc.step_streak.get(0) == 0


def test_answered_chain_never_climbs_to_ladder():
    # 全答链:每轮 +1(unclear 判决)→ 实答交付 -1 → 恒 0,阶梯永不顶替真答案
    fc = _fc()
    for i in range(6):
        fc.note_turn_outcome("unclear", 0, f"q{i}")
        fc.relieve_stall_streak()
        assert stall_ladder_level(fc.step_streak.get(0, 0)) == ""
    # 真死火:连续无出口轮(无实答抵销)照常爬升
    for i in range(3):
        fc.note_turn_outcome("unclear", 0, f"d{i}")
    assert stall_ladder_level(fc.step_streak.get(0, 0)) == "degrade"


def test_ladder_settle_same_level_fires_once_per_run():
    # 车道发射后的落账契约:degrade→4(下一轮 UNCLEAR 直落 bypass)、
    # bypass→7(直落 close)——每级每次升迁只发一次,不再复读同级台词。
    from agent_runtime.flow import STALL_BYPASS_N, STALL_CLOSE_N
    fc = _fc()
    for i in range(3):
        fc.note_turn_outcome("unclear", 1, f"k{i}")
    assert stall_ladder_level(fc.step_streak[1]) == "degrade"
    fc.step_streak[1] = STALL_BYPASS_N - 1  # agent 发射 degrade 后的落账
    fc.note_turn_outcome("unclear", 1, "k3")
    assert stall_ladder_level(fc.step_streak[1]) == "bypass"
    fc.step_streak[1] = STALL_CLOSE_N - 1  # agent 发射 bypass 后的落账
    fc.note_turn_outcome("unclear", 1, "k4")
    assert stall_ladder_level(fc.step_streak[1]) == "close"


def test_substantive_reply_relieve_wiring_pinned():
    # 源级 pin:_report_assistant_turn 只对实答 gen(llm/qa_fastpath)抵销;
    # 阶梯发射走 ladder_should_fire 同级门（P2.b 后的新契约，防回归拆门）。
    root = Path(__file__).resolve().parents[1]
    src = (root / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert 'if gen in ("llm", "qa_fastpath"):\n            flow_ctrl.relieve_stall_streak()' in src
    assert "ladder_should_fire(flow_ctrl.ladder_fired, _lvl)" in src


# ---- P2.b 同级连发修复（spec 2026-09-29 v2 §5）----
# 病灶（call-ed6aa9b8 17:30:37/17:30:46 两发同句）：「发射后顶 streak 到下一级
# 门槛-1」的数值魔术有洞——degrade 发射后 streak=4，stall_ladder_level(4) 仍判
# degrade → 下一轮同级再发。修法：ladder_fired 显式账本（本步已发射级别集合），
# 发射条件加 ladder_should_fire；advance/jump 清空（换步同级别可再发）。


def test_ladder_should_fire_gate():
    from agent_runtime.flow import ladder_should_fire

    assert ladder_should_fire(set(), "degrade") is True
    assert ladder_should_fire({"degrade"}, "degrade") is False  # 同级已发：拦
    assert ladder_should_fire({"degrade"}, "bypass") is True  # 升级：放行
    assert ladder_should_fire({"degrade", "bypass"}, "bypass") is False


def test_same_level_not_fired_twice_across_user_turns():
    """2026-09-29 实证形状回归钉：streak=3 发 degrade 后，streak=4 轮不得再发
    degrade（旧数值魔术此处漏拦）；streak=5 发 bypass。"""
    from agent_runtime.flow import ladder_should_fire

    fc = _fc()
    # 17:30:37 轮：streak=3 → degrade 发射 → fired 记账（新逻辑不再顶 streak）
    fc.ladder_fired.add("degrade")
    assert stall_ladder_level(4) == "degrade"  # streak=4 仍判 degrade（阈值函数不变）
    assert ladder_should_fire(fc.ladder_fired, "degrade") is False  # 但同级门拦住
    assert ladder_should_fire(fc.ladder_fired, "bypass") is True  # streak=5 升级放行


def test_ladder_fired_cleared_on_advance_and_jump():
    from agent_runtime.flow import FlowStep

    fc = FlowController(steps=[FlowStep(goal="g1", ref="r1"), FlowStep(goal="g2", ref="r2")])
    fc.ladder_fired.add("degrade")
    fc.ladder_fired.add("bypass")
    fc.advance()
    assert fc.ladder_fired == set()  # 换步：同级别可再发（新步新账）
    fc.ladder_fired.add("degrade")
    fc.jump_to(0)
    assert fc.ladder_fired == set()
