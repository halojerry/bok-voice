"""I3 跳步话面二修（2026-09-19）：跳步轮话面被流程总览逐字引力拉回线性剧本
（call-790fd558：跳第 4 步回复原样复排第 2/3 步台词并补问「哪个平台购买」；
call-f13c3c06：先讲对本步内容尾句又回头补问）——两层修：

1. 【跳转进入】标记加硬：点名被跳步号 + 明令「绝不按总览顺序从头重新开始」
   + 禁再问被跳步骤问题（单句「不要追问」实测压不过总览引力）。
2. 总览（flow_overview）尾部加图模板专用「跳转係常态」规则行——非图模板
   零渲染字节不变；图模板随静态前缀整场生效（KV 安全）。
"""
from __future__ import annotations

import json

from agent_runtime.flow import FlowController

_STEPS = json.dumps(
    [
        {"goal": "确认身份", "ref": "你好，请问是{姓名}吗？"},
        {"goal": "说明来电原因", "ref": "我们这边有一件快递需要跟您确认一下。"},
        {"goal": "询问购买平台", "ref": "请问这件商品是在哪个平台购买的呢？"},
        {"goal": "说明理赔方案", "ref": "如果确认丢件，我们会按平台规则赔付。"},
    ],
    ensure_ascii=False,
)
_GRAPH = json.dumps(
    {
        "version": 1,
        "intents": [
            {"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉"],
             "steps": [], "enabled": True},
        ],
        "bindings": [
            {"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d",
             "action": "jump_step", "step": 4},
        ],
    },
    ensure_ascii=False,
)


def _fc(graph: bool = True) -> FlowController:
    tpl = {"steps_json": _STEPS}
    if graph:
        tpl["graph_json"] = _GRAPH
    return FlowController.from_template(tpl, None)


# ---------------------------------------------------------------------------
# 标记加硬（跳步轮尾部）
# ---------------------------------------------------------------------------
def test_forward_jump_marker_names_skipped_steps():
    """正向跳 1→4：点名「第2、3步已被跳过」+ 禁按总览从头开始 + 禁补问。"""
    fc = _fc()
    fc.jump_to(3)  # 0-based → 第 4 步
    txt = fc.current_step_text()
    assert "【跳转进入】" in txt
    assert "第2、3步已被跳过" in txt
    assert "第 4 步" in txt and "共 4 步" in txt
    assert "绝不按流程总览的顺序从头重新开始" in txt
    assert "绝不再问被跳过步骤里的任何问题" in txt
    assert "只准问本步的问题" in txt  # 替代提问出口（防借被跳步问句提问）
    # 底稿照进首轮（跳步轮手上有本步正稿——引力竞争的本钱）
    assert "按平台规则赔付" in txt


def test_forward_jump_forbidden_quotes_last_line():
    """禁讲清单：被跳步原话照录（=总览事实行同文，复制源点名）、且是尾部最后一行
    （正稿在前禁句在后——生成前最后看到的是禁令）。仅跳转首轮渲染。"""
    fc = _fc()
    fc.jump_to(3)
    txt = fc.current_step_text()
    assert "【禁讲清单】" in txt
    assert "「我们这边有一件快递需要跟您确认一下。」" in txt  # 第 2 步原话
    assert "「请问这件商品是在哪个平台购买的呢？」" in txt  # 第 3 步原话
    # 清单是最后一个非空行块（禁令收尾）
    last_line = [l for l in txt.splitlines() if l.strip()][-1]
    assert last_line.startswith("【禁讲清单】")
    # 二轮渲染（同步再取）不再带清单（只跳转首轮）
    txt2 = fc.current_step_text()
    assert "【禁讲清单】" not in txt2


def test_backward_jump_marker_uses_return_wording():
    """后退跳无「被跳过」语义（那些步客户早已听过）——「回到本步」措辞，不点名。

    既有门 `_new_step` 要求 `current > 0`：跳回第 1 步不渲染标记（身份步有
    开场白/【开场已念】自己的处理面，刻意不动）——这里跳回中段步验证措辞。"""
    fc = _fc()
    fc.jump_to(3)   # 到第 4 步
    fc.current_step_text()  # 消费掉第一跳的 _just_advanced
    fc.jump_to(1)   # 后退回第 2 步
    txt = fc.current_step_text()
    assert "流程已直接回到本步" in txt
    assert "已被跳过" not in txt
    assert "绝不按流程总览的顺序从头重新开始" in txt  # 禁重启语义同样适用


def test_marker_is_jump_turn_only():
    """标记只在跳转轮渲染（_just_advanced 消费后消失）；线性推进仍走【新一步】原样。"""
    fc = _fc()
    fc.jump_to(3)
    first = fc.current_step_text()
    assert "【跳转进入】" in first
    second = fc.current_step_text()  # 同步再渲染（无新推进）
    assert "【跳转进入】" not in second
    # 线性推进：标记清零、【新一步】原样（既有行为零变化）
    fc2 = _fc()
    fc2.on_user_turn("好的")
    txt2 = fc2.current_step_text()
    assert "【新一步】" in txt2 and "跳转进入" not in txt2
    assert fc2._jump_skipped == []


def test_jump_skipped_bookkeeping():
    """_jump_skipped 记账：正向=起点后到目标前全部；advance/enter_closing 清零。"""
    fc = _fc()
    fc.jump_to(3)
    assert fc._jump_skipped == [2, 3]  # 1-based：第 2、3 步
    fc.jump_to(0)  # 后退
    assert fc._jump_skipped == []
    fc.jump_to(2)
    assert fc._jump_skipped == [2]
    fc.enter_closing()
    assert fc._jump_skipped == [] and fc._entered_by_jump is False


# ---------------------------------------------------------------------------
# 总览规则行（图模板专用，静态前缀）
# ---------------------------------------------------------------------------
def test_overview_rule_only_for_graph_templates():
    fc = _fc(graph=True)
    ov = fc.flow_overview()
    assert "允许按客户话题直接跳入" in ov
    assert "被跳过的步骤不再补讲、不再追问" in ov
    # 规则行在共享应答规则之前（事实行后紧跟，语义上属流程说明）
    assert ov.index("允许按客户话题直接跳入") < ov.index("身份与来电质疑") if "身份与来电质疑" in ov else True
    # 非图模板：规则行零渲染（字节面不添行）
    fc2 = _fc(graph=False)
    assert "允许按客户话题直接跳入" not in fc2.flow_overview()
    # 图模板总览仍逐场静态（KV 安全不变量）
    assert fc.flow_overview() == ov
