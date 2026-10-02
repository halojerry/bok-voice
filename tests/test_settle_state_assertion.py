"""D3 结算状态对账（2026-09-30 多轮上下文计划 Phase 4）。

τ-bench 用 DB-diff 判分、BFCL v3 每轮 state-check——状态层（FlowController）
与历史层（turns 账本）从不对账是我们缺的面。本修：agent 终态快照随官方
session_report 上行（flow_state=step:N,closing:0|1,wa:0|1），CP 结算时
`_assert_flow_state_vs_turns` 纯函数比对，脱节响亮报审计+控制台、绝不破结算
（W5 SMS 尾钩同款纪律）。无快照/无轮次/坏 JSON 自动跳过（旧通话零误报）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-plane"))

from control_plane.main import _assert_flow_state_vs_turns  # noqa: E402

CP_SRC = (ROOT / "apps" / "control-plane" / "control_plane" / "main.py").read_text(
    encoding="utf-8"
)
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)
PROBE_SRC = (ROOT / "scripts" / "probe_flow_20rounds.py").read_text(encoding="utf-8")


def _rep(fs: str) -> str:
    import json

    return json.dumps({"flow_state": fs, "llm_usage": {}})


def _turn(role: str, step: int, provider: str = "") -> dict:
    return {"role": role, "template_step": step, "provider": provider}


# ---- 纯函数比对矩阵 ----

def test_clean_state_no_mismatch():
    turns = [_turn("user", 1), _turn("assistant", 1), _turn("assistant", 5, "flow-say")]
    assert _assert_flow_state_vs_turns(_rep("step:5,closing:0,wa:0"), turns) is None


def test_step_ahead_of_ledger_is_mismatch():
    """快照步号超过账本最大步+1=步进了轮次没跟上（+1=挂断恰逢步进后未发轮,
    合法放行）。"""
    turns = [_turn("assistant", 2), _turn("assistant", 3)]
    assert _assert_flow_state_vs_turns(_rep("step:6,closing:0,wa:0"), turns) == (
        "step:6>turns_max:3"
    )
    # 恰好 +1=合法
    assert _assert_flow_state_vs_turns(_rep("step:4,closing:0,wa:0"), turns) is None


def test_wa_captured_without_ledger_trace_is_mismatch():
    turns = [_turn("assistant", 3)]
    assert _assert_flow_state_vs_turns(_rep("step:3,closing:0,wa:1"), turns) == (
        "wa:1-no-ledger-trace"
    )
    # wa-merged/任何 wa 车道痕迹都算账本证据
    turns_ok = [_turn("user", 5, "wa-merged"), _turn("assistant", 5)]
    assert _assert_flow_state_vs_turns(_rep("step:5,closing:0,wa:1"), turns_ok) is None


def test_both_issues_joined():
    turns = [_turn("assistant", 2)]
    out = _assert_flow_state_vs_turns(_rep("step:9,closing:1,wa:1"), turns)
    assert out == "step:9>turns_max:2;wa:1-no-ledger-trace"


def test_no_snapshot_or_bad_payload_skips():
    assert _assert_flow_state_vs_turns("", [_turn("assistant", 1)]) is None
    assert _assert_flow_state_vs_turns("not json", [_turn("assistant", 1)]) is None
    assert _assert_flow_state_vs_turns(_rep("step:9,closing:0,wa:0"), []) is None
    assert _assert_flow_state_vs_turns(_rep("step:x,closing:0,wa:0"), [_turn("assistant", 1)]) is None


# ---- 接线 pin ----

def test_agent_flow_state_injection_pinned():
    assert '_rd["flow_state"]' in AGENT_SRC
    assert 'f"step:{(int(flow_ctrl.current) + 1) if flow_ctrl.has_steps else 0}"' in AGENT_SRC


def test_settle_hook_pinned():
    """结算对账尾钩：审计 settle.state_mismatch + 控制台行 + 唔破结算包裹。"""
    assert "_assert_flow_state_vs_turns(call.get(\"session_report\") or \"\", turns)" in CP_SRC
    assert "SETTLE_STATE_MISMATCH call=" in CP_SRC
    assert '"settle.state_mismatch"' in CP_SRC


def test_flow20_passk_pinned():
    assert "def aggregate_passk(results: list[bool]) -> dict:" in PROBE_SRC
    assert '"--repeats"' in PROBE_SRC
    assert "[passk] run " in PROBE_SRC
