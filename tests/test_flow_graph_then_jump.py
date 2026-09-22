"""追问链（Phase 3.3）：`then_jump` 解析宽容 / 校验严格（spec §3）。"""
from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.flow import FlowController
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
    """消息前缀钉住（final-wave pin）：存在性断言会放过「报了个别的错但恰好含
    then_jump 字样」的漂移，故钉到 `bindings[<idx>].then_jump must be int in [1,999]`
    这段稳定前缀（`: {action!r}` 那类尾部修饰只在 `jump_step` 分支的合法性消息上，
    不在本条上——刻意不钉尾部，防将来加修饰破测试）。"""
    stable = f"then_jump must be int in [1,{STEP_MAX}]"
    for bad in (0, -3, STEP_MAX + 1, "4", True, False, None, 4.5, [4]):
        errs = validate_flow_graph(_doc(then_jump=bad))
        assert any(e.startswith("bindings[") and stable in e for e in errs), (bad, errs)


def test_validate_then_jump_rejected_on_jump_step():
    errs = validate_flow_graph(_doc(action="jump_step", step=4, then_jump=4))
    assert any("then_jump" in e and "jump_step" in e for e in errs), errs
    # 连带对照：step 合法只报 then_jump 一条，不叠无关错
    assert len([e for e in errs if "then_jump" in e]) == 1


# ---------------------------------------------------------------------------
# Task 2（运行时）：play_qa 罐头播完当场同步跳（位移三件套 + 记账纪律）
# ---------------------------------------------------------------------------

_TEMPLATE = {
    "steps_json": json.dumps(
        [{"goal": f"第{i}步", "ref": f"第{i}步说法"} for i in range(1, 7)], ensure_ascii=False
    ),
    "graph_json": json.dumps(
        {
            "version": 1,
            "intents": [{"id": "int_2b3c4d5e", "label": "退款", "keywords": ["退款"],
                         "steps": [], "enabled": True}],
            "bindings": [{"id": "bnd_c1d2e3f4", "intent": "int_2b3c4d5e", "action": "play_qa",
                          "qa_id": "qa-1", "then_jump": 4, "priority": 10, "once": False,
                          "enabled": True}],
        },
        ensure_ascii=False,
    ),
}


def test_apply_then_jump_moves_and_marks_entry():
    fc = FlowController.from_template(_TEMPLATE, None)
    assert fc.graph.bindings[0].then_jump == 4     # 解析面先过（T1 契约）
    assert fc.apply_then_jump(4) is True           # 实际位移
    assert fc.current == 3                         # 1-based 4 → 0-based 3
    assert fc._entered_by_jump is True             # 走 jump_to → 尾部【跳转进入】(I3)
    assert "跳转进入" in fc.current_step_text()


def test_apply_then_jump_noop_family_zero_side_effect():
    fc = FlowController.from_template(_TEMPLATE, None)
    assert fc.apply_then_jump(None) is False and fc.current == 0 and fc._entered_by_jump is False
    assert fc.apply_then_jump(1) is False and fc.current == 0   # 同位 no-op
    assert fc._entered_by_jump is False and fc._just_advanced is False
    fc.enter_closing()
    assert fc.apply_then_jump(4) is False and fc.current == 0   # closing 冻结
    empty = FlowController.from_template({"steps_json": "[]"}, None)
    assert empty.apply_then_jump(4) is False                    # 无步骤话术


def test_apply_then_jump_clamps_to_done():
    fc = FlowController.from_template(_TEMPLATE, None)
    assert fc.apply_then_jump(99) is True   # 越界钳到 len(steps)（jump_to 内既有钳制）
    assert fc.current == 6 and fc.done


def test_agent_play_branch_wires_then_jump_before_stop_response():
    """源级钉住（播放分支在 entrypoint 闭包内，离线起不了真栈；姿势同 I1 装配面测试）：
    play 成功 → 位移必须在 **本轮收尾 raise** 之前（同一轮内同步跳）；但**不渲染**目标步
    ——本分支 StopResponse 收尾、无 LLM 请求消费，渲染只会烧掉该步首渲染账本
    （_last_render_step/_just_advanced），令下一轮流程块重渲染退成分支模式，底稿与
    【跳转进入】结构性失落；渲染留给下一轮流程块首渲染（真被请求消费）。"""
    src = (Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    start = src.index("flow_ctrl.apply_then_jump(")
    stop = src.index("raise StopResponse()", start)   # 播放分支收尾 raise（注释无关锚）
    seg = src[start:stop]
    # P2.2 复核修：三臂抽 _gdispatch 闭包，绑定参数名 _b（then_jump 判据同源）
    assert "_b.then_jump" in src
    assert "_invalidate_stale_preemptive(" in seg
    assert "current_step_text()" not in seg   # 渲染推迟到下一轮（R1，勿在本分支烧首渲染账本）
    assert "via=then_jump" in src
    assert "FLOW_GRAPH jump_noop" in seg   # 无位移档不吞日志
    # 跳转块在播放成功分支内：play_miss 路径（条目/音频缺失）结构性永不跳
    assert stop < src.index("FLOW_GRAPH play_miss")
