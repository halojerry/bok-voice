"""追问链（Phase 3.3）：`then_jump` 解析宽容 / 校验严格（spec §3）。"""
from __future__ import annotations

import json

from bok_voice_core.flow_graph import STEP_MAX, parse_flow_graph, validate_flow_graph

_INTENT = {"id": "int_2b3c4d5e", "label": "退款", "keywords": ["退款"], "steps": [], "enabled": True}


def _doc(**binding_over: object) -> str:
    binding: dict = {
        "id": "bnd_c1d2e3f4",
        "intent": "int_2b3c4d5e",
        "action": "play_qa",
        "qa_id": "qa-1",
        "priority": 10,
        "once": False,
        "enabled": True,
    }
    binding.update(binding_over)
    return json.dumps({"version": 1, "intents": [_INTENT], "bindings": [binding]}, ensure_ascii=False)


def test_parse_then_jump_roundtrip_and_absent_default():
    assert parse_flow_graph(_doc(then_jump=4)).bindings[0].then_jump == 4
    assert parse_flow_graph(_doc(then_jump=STEP_MAX)).bindings[0].then_jump == STEP_MAX
    # 缺席 = None（存量图逐字节不变；旧断言面不受影响）
    assert parse_flow_graph(_doc()).bindings[0].then_jump is None


def test_parse_then_jump_tolerant_drops_bad_value_keeps_binding():
    """坏值只丢字段、绑定本体照活（宽容契约：绝不整条丢弃、绝不抛、绝不隐式转字符串）。"""
    for bad in (0, -1, STEP_MAX + 1, "4", True, False, None, 4.5, [4], {"step": 4}):
        doc = parse_flow_graph(_doc(then_jump=bad))
        assert len(doc.bindings) == 1, bad
        assert doc.bindings[0].then_jump is None, bad
        assert doc.bindings[0].qa_id == "qa-1", bad


def test_parse_then_jump_ignored_on_jump_step_binding():
    """宽容面：jump_step 绑带上带该键 → 解析期直接忽略（严格面才报错）。"""
    doc = parse_flow_graph(_doc(action="jump_step", step=4, then_jump=4))
    assert doc.bindings[0].action == "jump_step"
    assert doc.bindings[0].then_jump is None


def test_binding_without_then_jump_unchanged():
    """零变化：无链字段的绑定各字段照旧（含 step 钳制/priority 默认/once 默认）。

    `step` 期望值 1 而非 dataclass 默认 0：Phase 2 起 `_parse_binding` 恒钳制
    `max(1, min(..., STEP_MAX))`（jump_step 必填字段保可用性，flow_graph.py
    `_parse_binding`），play_qa 缺省 step 亦落到下界 1——本任务不动钳制
    （Global Constraints 零变化铁律「无 then_jump 的图解析结果逐字节同」）。
    """
    b = parse_flow_graph(_doc()).bindings[0]
    assert (b.id, b.intent, b.action, b.qa_id, b.step, b.priority, b.once, b.enabled) == (
        "bnd_c1d2e3f4", "int_2b3c4d5e", "play_qa", "qa-1", 1, 10, False, True,
    )


def test_validate_then_jump_accepted_on_play_qa():
    assert validate_flow_graph(_doc()) == []            # 缺省合法（零变化）
    assert validate_flow_graph(_doc(then_jump=1)) == []
    assert validate_flow_graph(_doc(then_jump=4)) == []
    assert validate_flow_graph(_doc(then_jump=STEP_MAX)) == []


def test_validate_then_jump_range_and_type():
    for bad in (0, -3, STEP_MAX + 1, "4", True, False, None, 4.5, [4]):
        errs = validate_flow_graph(_doc(then_jump=bad))
        assert any("then_jump" in e for e in errs), bad


def test_validate_then_jump_rejected_on_jump_step():
    errs = validate_flow_graph(_doc(action="jump_step", step=4, then_jump=4))
    assert any("then_jump" in e and "jump_step" in e for e in errs), errs
    # 连带对照：step 合法只报 then_jump 一条，不叠无关错
    assert len([e for e in errs if "then_jump" in e]) == 1
