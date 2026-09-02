"""对话流程控制器:分步话术推进 + 对象变量渲染 + 每轮"只走一步"约束。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import (  # noqa: E402
    FlowController,
    decide_advance,
    facts_line,
    parse_steps,
    render_template_text,
)


OBJ = {
    "display_name": "林先生",
    "tracking_no": "SF1234567890",
    "courier": "顺丰",
    "phone": "13800000000",
}


def test_render_template_vars():
    text = "你好{姓名},你的{快递单号}已到,尾号{快递尾号},由{物流公司}派送。"
    out = render_template_text(text, {"姓名": "林先生", "快递单号": "SF1234567890", "快递尾号": "7890", "物流公司": "顺丰"})
    assert "林先生" in out and "SF1234567890" in out and "7890" in out and "顺丰" in out
    assert "{" not in out


def test_missing_var_keeps_placeholder():
    # 缺失变量保留占位 → LLM 向客户询问而非编造
    out = render_template_text("你好{姓名}", {"姓名": ""})
    assert "{姓名}" in out


def test_facts_line_marks_unknown():
    line = facts_line(OBJ)
    assert "林先生" in line and "SF1234567890" in line and "顺丰" in line
    line2 = facts_line({"display_name": "", "tracking_no": "", "courier": ""})
    assert "待确认" in line2


def test_parse_steps_and_controller():
    steps_json = '[{"goal":"确认包裹是否本人的","ref":"你好{姓名}，{快递单号}是你的吗？"},{"goal":"说明理赔方案","ref":"以一赔二赔付"},{"goal":"引导办理理赔","ref":"加专员QQ办理"}]'
    tpl = {"steps_json": steps_json}
    fc = FlowController.from_template(tpl, OBJ)
    assert fc.has_steps and len(fc.steps) == 3
    assert fc.current == 0
    cur = fc.current_step_text()
    assert "第 1/3 步" in cur
    assert "SF1234567890" in cur or "7890" in cur  # 变量已渲染


def test_advance_only_on_confirm():
    fc = FlowController.from_template({"steps_json": '[{"goal":"g1","ref":"r1"},{"goal":"g2","ref":"r2"}]'}, OBJ)
    fc.on_user_turn("是我的，请问怎么处理？")  # 确认(是我的) → 推进
    assert fc.current == 1
    assert "第 2/2 步" in fc.current_step_text()
    # 有提问(怎么处理)但确认在前——确认信号已推进;回到第2步后再遇提问不推进
    fc.on_user_turn("为什么要赔这么多？")  # 提问 → 停留
    assert fc.current == 1
    fc.on_user_turn("好，可以")  # 确认第2步 → 完成
    assert fc.done
    assert fc.current_step_text() == ""  # 流程完成不再注入当前步


def test_deny_objection_stays():
    fc = FlowController.from_template({"steps_json": '[{"goal":"g1","ref":"r1"},{"goal":"g2","ref":"r2"}]'}, OBJ)
    fc.on_user_turn("不是我，我没买过这个快递")  # 否认 → 停留
    assert fc.current == 0


def test_decide_advance_cases():
    assert decide_advance("是我的") == "confirm"
    assert decide_advance("不是我") == "objection"
    assert decide_advance("怎么赔？") == "question"
    assert decide_advance("你们是骗子吧") == "objection"
    assert decide_advance("随便") == "unclear"


def test_no_steps_no_flow():
    fc = FlowController.from_template({}, OBJ)
    assert not fc.has_steps
    assert fc.current_step_text() == ""
    assert fc.flow_overview() == ""
