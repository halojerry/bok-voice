"""话术-引擎契约修复(2026-09-13 二轮审计):

① EN 模板分支/注意行头("If the customer…"/"Note:")此前 100% 不被解析——
  18 条分支+5 条注意(含金额不许自创/收号方向两条合规线)从未进渐进披露;
② 未知指令行(行内含「→」但行头不识别)静默丢弃 → 加一次性告警;
③ 词表误伤:裸「挂」/「投诉」/「唔使喇+唔方便」/EN 拒绝词缺失/粤语 DEFER 缺失/
  「多少/几多」问句漏判/step3 寒暄词被 CONFIRM 劫持/平台词在赔偿步误推进;
④ OBJECTION 指引注入旧话术「运费险、一赔二」与现行三档矛盾 → 单源化到模板。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import (  # noqa: E402
    CONFIRM,
    DEFER,
    OBJECTION,
    QUESTION,
    REFUSE,
    FlowController,
    decide_advance,
    parse_step_ref,
    parse_steps,
    should_auto_advance,
)


EN_STEP_REF = (
    "First, we'll verify the order value of your item. Does this plan sound acceptable to you?\n"
    "If the customer asks why → briefly say it follows the courier regulations and insurance standard.\n"
    "If the customer says it's too little → don't promise extra amounts, say we'll seek the highest standard.\n"
    "Note: quote exactly these three tiers, never invent amounts.\n"
    "Note: this step only explains compensation, don't collect any number yet.\n"
)


# ---- ① EN 分支/注意行头解析 ----


def test_parse_step_ref_english_branch_and_note_lines():
    parts = parse_step_ref(EN_STEP_REF)
    assert parts.script.startswith("First, we'll verify")
    assert len(parts.branches) == 2
    assert "asks why" in parts.branches[0][0]
    assert "insurance standard" in parts.branches[0][1]
    assert "too little" in parts.branches[1][0]
    assert len(parts.notes) == 2
    assert "never invent amounts" in parts.notes[0]


def test_parse_step_ref_zh_branch_still_works():
    parts = parse_step_ref(
        "您看这个方案可以接受吗？\n"
        "如果客户问为什么这样赔 → 简单说是按香港速递条例和快递保险标准。\n"
        "注意:三档金额跟足这个标准讲,不要自创金额。\n"
    )
    assert parts.script == "您看这个方案可以接受吗？"
    assert parts.branches[0][0] == "问为什么这样赔"
    assert len(parts.notes) == 1


# ---- ② 未知指令行告警 ----


def test_unparsed_directive_line_warns_once(capsys):
    ref = "您好，请问是{姓名}吗？\n客户报出号码(数字串) → 复述确认一次,然后推进下一步交代办理要求。\n"
    parse_step_ref(ref)
    out = capsys.readouterr().out
    assert "ref_directive_unparsed" in out
    # 第二次解析同一行不再刷告警(去重)
    parse_step_ref(ref)
    out2 = capsys.readouterr().out
    assert "ref_directive_unparsed" not in out2


# ---- ③ 词表误伤 ----


def test_bare_gua_not_refuse():
    # 「我挂住做嘢」(我忙着)≠ 挂线——旧裸「挂」误判 REFUSE 直接收线。
    assert decide_advance("我挂住做嘢啊，迟啲先。") != REFUSE


def test_complaint_routes_to_objection_not_refuse():
    # 客户问「投诉点处理」要走 OBJECTION 答疑,唔係一句再见收线。
    assert decide_advance("我要投诉点处理？") == OBJECTION


def test_m_sai_la_plus_inconvenient_not_refuse():
    # 「唔使喇,唔方便」=而家唔得闲(脚本要求约好再跟进),唔係拒绝。
    assert decide_advance("唔使喇，我而家唔方便。") != REFUSE
    # 但真拒绝「唔使喇」仍然 REFUSE(唔好矫枉过正)。
    assert decide_advance("唔使喇，拜拜。") == REFUSE


def test_english_refuse_words():
    assert decide_advance("I'm not interested") == REFUSE
    assert decide_advance("No thanks") == REFUSE
    assert decide_advance("I know, I know") != OBJECTION  # 旧裸 no 令 "know" 误判否认


def test_cantonese_defer_words():
    assert decide_advance("等我睇下先") == DEFER
    assert decide_advance("我諗一下先") == DEFER


def test_how_much_question_without_mark():
    assert decide_advance("赔几多") == QUESTION
    assert decide_advance("能赔多少") == QUESTION


def test_confirm_not_hijacked_by_turn_word():
    # 「好的,不过我觉得赔太少」主体係异议:CONFIRM 唔抢判,落 judge+嫌少分支。
    assert decide_advance("好的，不过我觉得赔太少。") != CONFIRM
    # 纯应承回归:CONFIRM 照旧。
    assert decide_advance("好的") == CONFIRM


def test_platform_advance_not_fired_in_compensation_step():
    # 赔偿步 ctx 只有「核实」冇「平台」——客户顺口讲平台名唔应该跳过承诺确认。
    auto = should_auto_advance(
        current=3,
        goal="讲清赔偿方案",
        ref="首先，我们会先核实您这件货品的订单金额：如果金额不足 100 元…",
        user_text="我淘宝买的，咁可以赔几多？",
        verdict=CONFIRM,
    )
    assert auto is False
    # 平台步(问「边个平台买」)照旧答到即推。
    auto2 = should_auto_advance(
        current=2,
        goal="核实购买平台",
        ref="那您是在拼多多、淘宝还是京东买的？\n如果客户说不记得哪个平台 → 提示看订单。",
        user_text="淘宝",
        verdict=CONFIRM,
    )
    assert auto2 is True


# ---- ④ OBJECTION 指引单源化 ----


def test_objection_guidance_no_stale_facts():
    fc = FlowController(steps=parse_steps('[{"goal": "通知货件遗失", "ref": "我哋係集運中轉倉…", "say": true}]'))
    fc.last_verdict = OBJECTION
    g = fc._verdict_guidance()
    assert "运费险" not in g and "一赔二" not in g
    assert "话术模板" in g  # 指向模板事实,唔再写死第二套金额
