"""共享应答规则进 A 线静态前缀（2026-09-17，5 通×50 轮话术外问题实证）。

两条规则（flow._SHARED_RESPONSE_RULES）：
① 身份/防诈质疑——4B 流程引力绕回当前步（「係邊個平台買㗎？」5 通重复 10+ 次），
   质疑整场得不到正面处理；
② 赔偿档位数字纪律——投诉/找主管/打错电话等非赔偿语境被自由复述具体数字。

落点=FlowController.flow_overview() 尾部附加：agent 装配时 set_flow 一次定格进
ContextState 静态前缀（【话术流程总览】同段），整场字节不变 → KV-cache 稳定区、
零逐轮成本；三语装配形态都必须带上。措辞=标准书面中文（共享区禁粤语书面语，
test_agent_language_follow.test_zh_prompt_purity_no_cantonese_marks 门禁）。
装配姿势照抄 agent.py 装配路径：from_template → set_flow(overview, current) →
set_user_language → render_instruction_prefix（不 mock ContextState）。
"""

from __future__ import annotations

import json

from agent_runtime.flow import FlowController
from agent_runtime.providers.livekit_plugins import ContextState

# 复刻三语模板 6 步形态（身份确认/通知/平台/赔偿 say 步/办理/收尾）。
_STEPS_JSON = json.dumps(
    [
        {"goal": "确认身份", "ref": "您好，请问是{姓名}吗？"},
        {"goal": "致歉通知", "ref": "不好意思，您的包裹在运输途中丢失了，先跟您说一声。", "say": 1},
        {"goal": "确认平台", "ref": "请问您是在哪个平台购买的呢？"},
        {"goal": "赔偿方案", "ref": "最低300元起赔偿，您看这样可以吗？", "say": 1},
        {"goal": "引导办理", "ref": "我们安排专员联系您办理。"},
        {"goal": "收尾", "ref": "感谢您的配合。"},
    ],
    ensure_ascii=False,
)

_ANCHORS = (
    "身份与来电质疑",   # 规则1节头
    "官方渠道",         # 规则1身份重申要点
    "反复追问平台",     # 规则1禁止项
    "赔偿数字纪律",     # 规则2节头
    "一律不报具体数字",  # 规则2核心禁令
)

# 与 test_agent_language_follow._DIALECT_MARKS 同款特征字（共享区禁粤语书面语）。
_DIALECT_MARKS = "唔係嘅咗喺嚟嘢冇啲嗰㗎乜睇攞"


def _assemble_prefix(lang: str) -> str:
    """真装配路径（agent.py 装配同款）：flow 总览进 ContextState → 渲染静态前缀。"""
    fc = FlowController.from_template({"steps_json": _STEPS_JSON}, {"display_name": "林先生"})
    ctx = ContextState(account_id="acc-001")
    ctx.set_user_language(lang)
    ctx.set_flow(fc.flow_overview(), fc.current_step_text())
    return ctx.render_instruction_prefix()


def test_static_prefix_contains_objection_rules():
    """渲染后的共享前缀必须含两条规则锚文本（规则进静态前缀而非逐轮尾部）。"""
    prefix = _assemble_prefix("zh")
    for anchor in _ANCHORS:
        assert anchor in prefix, anchor
    # 规则在总览段（静态前缀区），不在逐轮尾部。
    tail = _assemble_tail()
    for anchor in _ANCHORS:
        assert anchor not in tail


def _assemble_tail() -> str:
    fc = FlowController.from_template({"steps_json": _STEPS_JSON}, {"display_name": "林先生"})
    ctx = ContextState(account_id="acc-001")
    ctx.set_user_language("zh")
    ctx.set_flow(fc.flow_overview(), fc.current_step_text())
    ctx.set_last_reply("好的，已为您登记。")
    return ctx.render_context_tail()


def test_rules_present_in_all_three_language_assemblies():
    """三语装配形态（zh/cantonese/en）各自渲染都含锚文本——规则无条件进每通通话。"""
    for lang in ("zh", "cantonese", "en"):
        prefix = _assemble_prefix(lang)
        for anchor in _ANCHORS:
            assert anchor in prefix, f"{lang}: {anchor}"


def test_rules_text_is_standard_written_chinese():
    """新规则块经 flow_overview 走 zh 装配后不得带粤语特征字（纯度门禁覆盖新块）。"""
    prefix = _assemble_prefix("zh")
    bad = sorted({ch for ch in _DIALECT_MARKS if ch in prefix})
    assert not bad, f"zh 静态前缀含粤语特征字: {bad}"


def test_rules_byte_stable_across_renders():
    """两次渲染字节一致——静态前缀契约（KV-cache 严格前缀安全）。"""
    assert _assemble_prefix("zh") == _assemble_prefix("zh")


def test_no_steps_overview_stays_empty():
    """无话术（B 线同传/开放人设）零回归：总览与规则都不注入。"""
    fc = FlowController.from_template(None, None)
    assert fc.flow_overview() == ""
