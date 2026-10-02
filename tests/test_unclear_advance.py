"""unclear 连续推进（P3.1，spec 2026-09-29 v2 §6）。

产品语义（Ethan 2026-09-29 拍板「意图不明确的时候就一步一步往下走」）：
judge 连续 N 轮 unclear（route=keep 家族）→ 按 rule=auto 同构管线推一步，
新步内容照常 LLM 应答——「流程别停」。

契约：
- FlowController.unclear_streak（per-step dict，step_streak 同族形态）；
- bump_unclear_streak()（judge unclear 消费点）/ relieve_unclear_streak()（实答
  轮 -1 抵销，挂 relieve_stall_streak 同点）/ advance·jump_to 清零；
- 模块级纯函数 unclear_should_advance(streak, threshold)；
- agent 消费点：BOK_UNCLEAR_ADVANCE（默认开）/BOK_UNCLEAR_ADVANCE_N（默认 3），
  非最后一步 + 非 closing/done + WA 收号门，日志 [flow] unclear-advance。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import (  # noqa: E402
    FlowController,
    FlowStep,
    unclear_should_advance,
)

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


def _fc3() -> FlowController:
    return FlowController(
        steps=[FlowStep(goal="g1", ref="r1"), FlowStep(goal="g2", ref="r2"), FlowStep(goal="g3", ref="r3")]
    )


def test_unclear_should_advance_threshold():
    assert unclear_should_advance(1, 3) is False
    assert unclear_should_advance(2, 3) is False
    assert unclear_should_advance(3, 3) is True
    assert unclear_should_advance(5, 3) is True


def test_bump_and_advance_reset():
    fc = _fc3()
    assert fc.bump_unclear_streak() == 1
    assert fc.bump_unclear_streak() == 2
    assert fc.unclear_at(fc.current) == 2
    fc.advance()
    assert fc.unclear_at(0) == 0 and fc.unclear_at(1) == 0  # advance 清零（新步新账）


def test_relieve_decrements_never_below_zero():
    fc = _fc3()
    fc.bump_unclear_streak()
    fc.bump_unclear_streak()
    fc.relieve_unclear_streak()
    assert fc.unclear_at(fc.current) == 1
    fc.relieve_unclear_streak()
    fc.relieve_unclear_streak()
    assert fc.unclear_at(fc.current) == 0  # 0 下限


def test_jump_to_clears():
    fc = _fc3()
    fc.bump_unclear_streak()
    fc.jump_to(2)
    assert all(v == 0 for v in fc.unclear_streak.values())


def test_agent_consumption_wiring_pinned():
    """源级 pin：judge 消费点接 unclear 推进（kill-switch/门槛/边界门齐全）。"""
    assert 'os.environ.get("BOK_UNCLEAR_ADVANCE", "1") == "1"' in AGENT_SRC
    assert 'os.environ.get("BOK_UNCLEAR_ADVANCE_N", "3")' in AGENT_SRC
    assert "unclear_should_advance(" in AGENT_SRC
    assert "[flow] unclear-advance step=" in AGENT_SRC
    # 实答抵销挂点：与 relieve_stall_streak 同点（_report_assistant_turn 实答判据）
    assert "flow_ctrl.relieve_unclear_streak()" in AGENT_SRC
    # 边界门：非最后一步 + closing/done
    assert "flow_ctrl.current < len(flow_ctrl.steps) - 1" in AGENT_SRC
