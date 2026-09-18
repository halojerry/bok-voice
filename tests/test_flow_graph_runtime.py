"""FlowController 图装配与 jump_to(spec §4.2/§4.3)。"""
from __future__ import annotations

import json

from agent_runtime.flow import FlowController

_TEMPLATE = {
    "steps_json": json.dumps(
        [{"goal": f"第{i}步", "ref": f"第{i}步说法"} for i in range(1, 7)],
        ensure_ascii=False,
    ),
    "graph_json": json.dumps(
        {
            "version": 1,
            "intents": [{"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉"], "steps": [], "enabled": True}],
            "bindings": [{"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d", "action": "jump_step", "step": 4}],
        },
        ensure_ascii=False,
    ),
}


def test_from_template_parses_graph():
    fc = FlowController.from_template(_TEMPLATE, None)
    assert len(fc.graph.intents) == 1
    assert fc.graph_fired == set()
    assert fc.current == 0


def test_from_template_garbage_graph_is_empty():
    fc = FlowController.from_template({"steps_json": "[]", "graph_json": "garbage"}, None)
    assert fc.graph.intents == []
    fc2 = FlowController.from_template({"steps_json": "[]"}, None)
    assert fc2.graph.intents == []


def test_jump_to_clamps_and_respects_closing():
    fc = FlowController.from_template(_TEMPLATE, None)
    fc.jump_to(3)  # 0-based → 第 4 步
    assert fc.current == 3
    # 钳制:越界跳到 done(== len)
    fc.jump_to(99)
    assert fc.current == 6
    assert fc.done
    # closing 冻结
    fc2 = FlowController.from_template(_TEMPLATE, None)
    fc2.enter_closing()
    fc2.jump_to(3)
    assert fc2.current == 0
    # 同位 no-op 不置 _just_advanced
    fc3 = FlowController.from_template(_TEMPLATE, None)
    fc3.jump_to(0)
    assert fc3.current == 0 and not fc3._just_advanced
