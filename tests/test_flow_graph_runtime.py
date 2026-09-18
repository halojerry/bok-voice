"""FlowController 图装配与 jump_to(spec §4.2/§4.3)。"""
from __future__ import annotations

import json
from pathlib import Path

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


def test_jump_entered_tail_block_then_advance_restores_new_step():
    """I3:图跳入的步尾部讲【跳转进入】(客户从未确认被跳过嘅步),正常推进返【新一步】。"""
    fc = FlowController.from_template(_TEMPLATE, None)
    fc.jump_to(3)  # 0-based → 第 4 步;实际位移 → 置跳转标记
    assert fc._entered_by_jump is True
    txt = fc.current_step_text()
    assert "跳转进入" in txt
    assert "新一步" not in txt  # 唔准再讲「客户刚刚确认了上一步」(与事实相反)
    # 同位 no-op 跳转唔置标记(与 _just_advanced 同档)
    fc_noop = FlowController.from_template(_TEMPLATE, None)
    fc_noop.jump_to(0)
    assert fc_noop._entered_by_jump is False
    # 后续真推进 → 标记清零,尾部回到【新一步】原样
    fc.advance()
    assert fc._entered_by_jump is False
    txt2 = fc.current_step_text()
    assert "新一步" in txt2 and "跳转进入" not in txt2


def test_jump_entered_closing_clears_flag():
    """I3 对照:收尾态清跳转标记(尾部走 closing_text,标记无意义且唔准残留)。"""
    fc = FlowController.from_template(_TEMPLATE, None)
    fc.jump_to(3)
    fc.enter_closing()
    assert fc._entered_by_jump is False
    assert "跳转进入" not in fc.current_step_text()


def test_qa_index_assembly_covers_graph_play_qa():
    """I1 装配面(源级钉住):索引构建面=快路开 **或** 图有启用 play_qa 绑定,
    而轮级快路自身仍受 BOK_QA_FASTPATH 门闸——图 kill-switch 关唔可以静默掐死
    图播放,快路关也唔可以经为图所建的索引复活。装配点在 entrypoint 内,离线
    起不了真栈,故用源级断言(同 terminology 门禁姿势)。
    """
    src = (
        Path(__file__).resolve().parents[1] / "apps/agent/agent_runtime/agent.py"
    ).read_text(encoding="utf-8")
    assert "ACTION_PLAY_QA" in src
    assert "_graph_qa_bindings" in src
    assert "built_for=graph" in src
    assert "if _tts_cache is not None and (_qa_fastpath_on or _graph_qa_bindings):" in src
    assert "if _qa_fastpath_on and _qa_index is not None and _tts_cache is not None:" in src
