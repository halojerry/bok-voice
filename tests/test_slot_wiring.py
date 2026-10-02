"""D1 槽位化 actor 接线源级 pin：闸登记 / A 线装配点 / B 线零接线 / prewarm 形状。

规格铁律：闸 BOK_SLOT_ACTOR（默认 "0"）A 线装配点读；B 线 interpret.py 不接；
任务块只加新分支（record_applied_tail 机制原样复用）；prewarm system 闸开时
渲染角色卡（形状仍 [system, assistant(greeting), user] 恒带尾 user）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "agent"))
sys.path.insert(0, str(_ROOT / "tools"))

import bok  # noqa: E402

_AGENT = _ROOT / "apps" / "agent" / "agent_runtime"
_AGENT_SRC = (_AGENT / "agent.py").read_text(encoding="utf-8")
_PLUGIN_SRC = (_AGENT / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
_FLOW_SRC = (_AGENT / "flow.py").read_text(encoding="utf-8")
_SPEC_SRC = (_AGENT / "prefill_speculator.py").read_text(encoding="utf-8")
_INTERP_SRC = (_AGENT / "interpret.py").read_text(encoding="utf-8")
_SLOT_SRC = (_AGENT / "slot_actor.py").read_text(encoding="utf-8")


def test_slot_env_registered_in_forward_env():
    """BOK_SLOT_ACTOR 进 tools/bok.py _FORWARD_ENV（prod launchd 封闭 env 面可达）。"""
    assert "BOK_SLOT_ACTOR" in bok._FORWARD_ENV
    assert 'os.environ.get("BOK_SLOT_ACTOR", "0")' in _SLOT_SRC, "闸读法=字面量缺省 0"


def test_slot_assembly_point_reads_gate_and_builds_card():
    """A 线装配点：读闸一次置 slot_mode；语言落定后渲染角色卡。"""
    assert "from .slot_actor import build_slot_system, slot_actor_enabled" in _AGENT_SRC
    assert "context_state.slot_mode = slot_actor_enabled()" in _AGENT_SRC
    assert "build_slot_system(" in _AGENT_SRC
    assert "context_state.set_slot_system(_slot_card)" in _AGENT_SRC


def test_slot_push_helper_replaces_all_seven_sites():
    """编排器给槽单点：_push_flow_state 收口全部推进/直念推点；
    直调 set_flow_current(current_step_text()) 只剩 helper 内一处（旧路径）。

    计数=def + 7 调用点（2026-10-02 批3 合流修后回归 8）：main 闭包版的
    jump 臂曾退化为 legacy 直调（slot 模式图跳后槽位不更新）——已修回
    _push_flow_state，七个推进点重新全数收口。"""
    assert _AGENT_SRC.count("_push_flow_state(context_state, flow_ctrl)") == 8  # def + 7 调用点
    assert _AGENT_SRC.count("context_state.set_flow_current(flow_ctrl.current_step_text())") == 1
    # 装配点分流：slot 只推槽位（总览族不进 prompt）；legacy 走 set_flow。
    assert _AGENT_SRC.count("context_state.set_slot_step(flow_ctrl.slot_step_view())") == 2
    assert _AGENT_SRC.count("context_state.set_flow(flow_ctrl.flow_overview()") == 1


def test_slot_prewarm_uses_role_card_and_keeps_tail_user():
    """prewarm（闸开）：system=角色卡本身；形状 [system, assistant(greeting), user]
    恒带尾 user（9B 无尾 user 404 陷阱，计划档 §七）。"""
    from agent_runtime.agent import _build_prefix_prewarm_messages
    from agent_runtime.providers.livekit_plugins import ContextState

    ctx = ContextState(account_id="t")
    ctx.slot_mode = True
    card = "你是小蓝。\n【语言】用自然口语的普通话回复。"
    ctx.set_slot_system(card)
    msgs = _build_prefix_prewarm_messages(ctx, "人設base", "您好，请问是陈先生吗？")
    assert [m["role"] for m in msgs] == ["system", "assistant", "user"]
    assert msgs[0]["content"] == card, "闸开 system 必须只发角色卡（不并 instructions）"
    assert "人設base" not in msgs[0]["content"]
    assert msgs[-1]["role"] == "user"
    # 闸关（缺省）：system=prefix+"\n"+instructions，逐字节旧形。
    ctx2 = ContextState(account_id="t")
    ctx2.set_user_language("zh")
    msgs2 = _build_prefix_prewarm_messages(ctx2, "人設base", "您好")
    assert msgs2[0]["content"] == ctx2.render_instruction_prefix() + "\n" + "人設base"
    assert [m["role"] for m in msgs2] == ["system", "assistant", "user"]


def test_slot_context_state_default_off():
    """ContextState 缺省 slot_mode=False——所有既有调用点零变化（零漂移前提）。"""
    from agent_runtime.providers.livekit_plugins import ContextState

    assert ContextState(account_id="t").slot_mode is False


def test_slot_plugin_branches_and_ledger_reuse():
    """插件层：三处 slot 分支（前缀/尾部/chat 合并）+ 冻结账本机制原样复用。"""
    assert "self.slot_mode: bool = False" in _PLUGIN_SRC
    # 前缀/尾部两处 `if self.slot_mode:` + chat 合并/组装两处 `self._ctx.slot_mode`。
    assert _PLUGIN_SRC.count("if self.slot_mode:") == 2
    assert "if self._ctx.slot_mode and prefix:" in _PLUGIN_SRC
    assert "if self._ctx.slot_mode:" in _PLUGIN_SRC
    assert "build_slot_task_block(" in _PLUGIN_SRC
    assert "compose_slot_user_message(tail, body)" in _PLUGIN_SRC
    # 冻结机制不删不改语义：既有 record_applied_tail（def+2 调用）全在——任务块经它入史。
    assert _PLUGIN_SRC.count("record_applied_tail(") >= 3
    assert "def record_applied_tail" in _PLUGIN_SRC
    # 旧路径表达式逐字节保留（_compose legacy 分支）。
    assert 'return f"{body}\\n\\n{tail}" if (body and tail) else (body or tail)' in _PLUGIN_SRC


def test_slot_speculator_same_order_single_source():
    """prefill 投机预热与 chat 冻结点共用 compose_slot_user_message（防组装分叉）。"""
    assert "compose_slot_user_message(tail, prefix)" in _SPEC_SRC
    assert 'user_content = f"{prefix}\\n\\n{tail}" if tail else prefix' in _SPEC_SRC


def test_slot_b_line_interpret_untouched():
    """B 线 interpret.py 零接线（规格：B 线不接槽位化）。"""
    for needle in ("slot_actor", "BOK_SLOT_ACTOR", "slot_mode", "set_slot_step", "set_slot_system"):
        assert needle not in _INTERP_SRC, f"B 线出现槽位化接线: {needle}"


def test_slot_flow_view_method_and_no_parser():
    """当步槽位视图=FlowController 结构化方法（渲染侧零文本解析）。"""
    assert "def slot_step_view(self) -> dict" in _FLOW_SRC
    assert "def current_step_text(self) -> str" in _FLOW_SRC, "legacy 渲染面保留"
