"""M-22 修复波#4:罐头/分支出声前内部指令守卫接线 + 罐头/graph 优先级新语义。

判据本体在 bok_voice_core.canned_guard(判据面钉在 test_canned_text_guard.py);
本文件钉**接线与语义**:
- 出声前守卫挂在两条分支出声出口(收线台词直念/罐头快路),命中拒出声改走
  LLM(教练文案照旧经渐进披露进 prompt=本来用途),打点 CANNED_COACH_BLOCKED;
- 分支罐头快路移到话术图引擎**之后**(T4 M1:罐头遮蔽 graph,V2 六通零
  FLOW_GRAPH)——确定性图命中>模糊分支命中,notify 打铃与罐头共存;
- 本轮推进过的轮(stale plan,branch/graph jump/规则 advance)分支罐头让位——
  旧版罐头会拿推进前旧步的分支应答抢答新步轮(zh/s1 报号轮实证);
- BOK_CANNED_TEXT_GUARD 进 _FORWARD_ENV(env 立法)。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("apps/agent", "packages/core", "scripts"):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def _agent_src() -> str:
    return (ROOT / "apps/agent/agent_runtime/agent.py").read_text(encoding="utf-8")


def _bok_src() -> str:
    return (ROOT / "tools/bok.py").read_text(encoding="utf-8")


# ---- ① 出声前守卫接线 ----


def test_guard_reads_env_and_registered_in_forward_env():
    src = _agent_src()
    assert 'os.environ.get("BOK_CANNED_TEXT_GUARD", "1") == "1"' in src  # 默认开
    assert '"BOK_CANNED_TEXT_GUARD"' in _bok_src()  # env 立法:进 _FORWARD_ENV


def test_guard_imported_from_core_module():
    src = _agent_src()
    assert "canned_guard" in src
    assert "is_internal_instruction" in src


def test_guard_on_both_branch_speak_exits():
    src = _agent_src()
    i_guardfirst = src.index("def branch_hit_plan(")
    # 收线台词出口:置 _branch_refuse_say 前有守卫(命中=当空台词,落 LLM 收尾)
    i_refuse_say = src.index('_branch_refuse_say = str(_branch_plan["text"])')
    assert "is_internal_instruction" in src[i_guardfirst:i_refuse_say]
    # 罐头快路出口:播 PCM 前有守卫
    i_bc = src.index("# ---- 分支罐头快路")
    i_pcm = src.index("_bc_pcm = _qa_pcm_for(_bc_resp)")
    assert "is_internal_instruction" in src[i_bc:i_pcm]


def test_coach_blocked_log_line_present():
    src = _agent_src()
    assert src.count("CANNED_COACH_BLOCKED") >= 2  # 收线/罐头两条出声出口都有打点


# ---- ② 罐头/graph 优先级新语义 ----


def test_branch_canned_block_after_graph_block():
    src = _agent_src()
    i_bc = src.index("# ---- 分支罐头快路")
    i_graph = src.index("# ---- 话术图引擎")
    i_qa = src.index("# ---- Q→A 检索快路")
    # 新序:REFUSE/DEFER/say > graph(jump/play/notify) > branch-canned > QA > LLM
    assert i_graph < i_bc < i_qa


def test_branch_canned_yields_on_advanced_turn():
    src = _agent_src()
    i_bc = src.index("# ---- 分支罐头快路")
    i_qa = src.index("# ---- Q→A 检索快路")
    region = src[i_bc:i_qa]
    # stale plan 丢弃:本轮推进过(规则 advance/graph jump/branch jump)→
    # 计划是推进前旧步匹配的,不许拿旧步罐头抢答新步轮(zh/s1 报号轮实证)
    assert "flow_ctrl.current != _flow_step_before" in region
    assert "BRANCH_CANNED stale" in region


def test_graph_jump_discards_branch_plan():
    src = _agent_src()
    # graph jump 实际位移后,旧步分支计划作废(StopResponse 不 raise,会落穿罐头腿)
    i_jump = src.index('if _gbinding.action == "jump_step":')
    i_notify = src.index("elif _gbinding.action == ACTION_NOTIFY_HUMAN:")
    assert "_branch_plan = None" in src[i_jump:i_notify]


def test_notify_arm_keeps_branch_plan_coexist():
    src = _agent_src()
    # notify 打铃不抢话:罐头照播(共存),不许在 notify 臂丢计划
    i_notify = src.index("elif _gbinding.action == ACTION_NOTIFY_HUMAN:")
    i_play = src.index("else:  # play_qa:条目在场且音频已物化才播")
    assert "_branch_plan = None" not in src[i_notify:i_play]


def test_notify_and_canned_same_turn_provider_merged():
    """M-1(评审返工;EX-2 2026-09-28 泛化):同轮 graph-notify 打铃 + 分支罐头
    共存——罐头出声不再覆写 graph-notify 标记,notify 顺延槽在 item 消费点并归,
    turns provider 合并归因(branch-canned+graph-notify),账本双标记俱在。"""
    src = _agent_src()
    # 合并归因:消费点把票据 lane 与 notify 顺延 lane 用 "+" 拼接(泛化,非特例)
    assert 'provider = f"{_ticket.lane}+{_pending}"' in src
    assert '_register_reply_lane(lane="branch-canned", text=_bc_resp)' in src  # 罐头建票
    assert '_register_reply_lane(lane="graph-notify", notify=True)' in src  # 打铃顺延
