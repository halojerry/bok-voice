"""prompt 手术② 单测（2026-09-28）：步骤纪律上移稳定前缀 + 记忆块减半。

「只围绕当前这一步…」收口指令原挂在 flow.current_step_text() 尾部逐轮复读，
无条件文本逐轮重 prefill 是纯浪费（S5「重复控制」同款病灶）。本测钉住：
①纪律在前缀里（无条件进每通）；②尾部步骤文本不再复读；③记忆摘要总长
默认 600（原 1200 减半——尾部每轮 prefill 是 TTFT 直接组分）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import (  # noqa: E402
    STEP_DISCIPLINE_RULE,
    FlowController,
    parse_step_ref,
)
from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402


def _controller() -> FlowController:
    ref = (
        "您好，請問係{姓名}小姐嗎？我係{快遞}客服。\n"
        "如果客户问係邊個→我係{快遞}客服，打嚟通知個包裹。"
    )
    return FlowController.from_template(
        {
            "steps_json": json.dumps(
                [
                    {"ref": ref, "goal": "确认客户身份", "lang": "cantonese"},
                    {"ref": "你喺邊個平台買嘅？\n如果客户讲拼多多→确认拼多多订单", "goal": "确认平台"},
                ]
            )
        },
        object_card={"display_name": "陳小姐", "courier": "XX快遞"},
    )


def test_discipline_rule_in_static_prefix():
    """纪律块必须无条件进稳定前缀（整场 KV 命中）。"""
    cs = ContextState.from_env(account_id="acc-test")
    prefix = cs.render_instruction_prefix()
    assert "【步骤纪律】" in prefix
    assert "只围绕当前这一步回应" in prefix
    # 前缀不变量：两次渲染逐字节相同（ Surgery 后仍是稳定前缀）。
    assert prefix == cs.render_instruction_prefix()


def test_discipline_rule_not_in_per_turn_step_text():
    """尾部步骤文本不再复读纪律块——这正是手术要省的逐轮 prefill。"""
    flow = _controller()
    text = flow.current_step_text()
    assert "只围绕当前这一步回应" not in text
    assert "不无限追问" not in text
    # 而步骤正文本身仍在（守卫/底稿等照旧）。
    assert "现在这一步" in text or "流程第" in text or "确认客户身份" in text


def test_rule_text_semantics_preserved():
    """上移后措辞除「上面的应对→本步给到的应对」外逐字保留。"""
    assert "不照念底稿" in STEP_DISCIPLINE_RULE
    assert "本步给到的应对" in STEP_DISCIPLINE_RULE
    assert "上面的应对" not in STEP_DISCIPLINE_RULE
    assert "不无限追问" in STEP_DISCIPLINE_RULE


def test_summary_cap_halved_default():
    """记忆摘要总长默认 180（F2 手术③ 600→250；5b 2026-09-30 soak A/B 再
    250→180，与 HISTORY=6 同臂——TTFT/commit_to_audio 双降）：尾部每轮 prefill
    的直接组分；渲染行数默认 3（原 6），env BOK_MEMORY_CHARS 可覆盖总长。"""
    cs = ContextState.from_env(account_id="acc-test")
    assert cs._max_summary_chars == 180
    # 行为不变量：超限裁最旧行，且永不裁到 0 行。
    for i in range(40):
        cs.add_summary("user", f"第{i}轮" + "很长的对话内容" * 20)
    assert len("\n".join(cs._summary_lines)) <= 180
    assert len(cs._summary_lines) >= 1
