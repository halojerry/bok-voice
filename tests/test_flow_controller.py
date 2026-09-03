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
    "address": "香港湾仔活道 1 号",
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
    assert "香港湾仔活道" in line  # 收货地址也在已知事实里
    line2 = facts_line({"display_name": "", "tracking_no": "", "courier": "", "address": ""})
    assert "待确认" in line2


def test_address_var():
    # {收货地址}/{地址} 变量替换
    out = render_template_text("你嘅包裹会寄去{收货地址},确认係{地址}吗?",
                               {"收货地址": "香港湾仔活道 1 号", "地址": "香港湾仔活道 1 号"})
    assert "香港湾仔活道" in out and "{" not in out


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


# ---- 旧式四段模板自动转分步(不一口气念完) ----
LEGACY_TPL = {
    "name": "粤语客服·产品咨询",
    "opening": "你好请问你{姓名}咩？我哋呢边系{快递公司}快递…",
    "core": "你嘅包裹运输途中丢失,我哋有买运费保险,会一赔二补俾你;你可以重新下单",
    "objection": "如果你担心真假,可以加线上专员核实",
    "closing": "好,唔该晒你,有咩问题随时搵我。拜拜!",
}


def test_legacy_four_sections_become_steps():
    # 旧式四段模板无 steps_json:应转成 4 个分步,逐轮推进,而不是整段塞给 LLM 念。
    from agent_runtime.flow import FlowController, template_to_steps

    steps = template_to_steps(LEGACY_TPL)
    assert len(steps) == 4
    assert steps[0].goal.startswith("开场")
    assert steps[-1].goal.startswith("收尾")

    fc = FlowController.from_template(LEGACY_TPL, OBJ)
    assert fc.has_steps
    assert "第 1/4 步" in fc.current_step_text()
    # 开场步的 ref 是 opening 全文
    assert "你好请问" in fc.current_step_text()


def test_legacy_steps_advance_one_by_one():
    # 四段转分步后:客户确认才推进,不会一口气念完。
    from agent_runtime.flow import FlowController

    fc = FlowController.from_template(LEGACY_TPL, OBJ)
    # 开场后:客户确认 → 才进第2步(core)
    fc.on_user_turn("係呀,係我嘅")
    assert fc.current == 1
    assert "第 2/4 步" in fc.current_step_text()
    # 第2步(core)客户再确认 → 进第3步(objection/异议)
    fc.on_user_turn("好嘅,可以")
    assert fc.current == 2
    # 未确认不会跳:流程到第3步就停,不会自己讲完收尾
    assert fc.current == 2
    assert "收尾" not in fc.current_step_text()


def test_current_step_explicit_no_leak_instruction():
    # 当前步注入须明确区分"参考要点(内部)"与"对客户说的话",禁止复述分支指示。
    from agent_runtime.flow import FlowController

    fc = FlowController.from_template(
        {"steps_json": '[{"goal":"确认包裹","ref":"你好{姓名}。\\n如果客户唔记得 → 提佢地址帮佢回忆"}]'},
        OBJ,
    )
    txt = fc.current_step_text()
    assert "勿念给客户" in txt
    assert "绝不把「如果" in txt
    assert "参考要点(内部指示" in txt
