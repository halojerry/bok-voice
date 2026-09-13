"""话术分支渐进披露（2026-09-12 P0「会说话」）。

call-8fa17d2b 实证：QA miss 落到 LLM 后，尾部把整段 ref（正稿+全部分支）以
台词形态注入 → 4B 复制引力压过「勿念原文」指引 → 「你说什么东西？」换来
11.9s 整段重念、两轮回复一字不差。本组测试钉死三件事：
① parse_step_ref 把 ref 拆成 正稿/分支(如果客户X→就Y)/注意 三件；
② match_step_branch 按 verdict+用户话命中单分支（命中不了返回 None，不退全量）；
③ current_step_text 渐进披露——进入本步首轮给底稿，此后只给命中分支，
   正稿不再常驻（say 步恒不给正稿——直念原文已在历史）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import (  # noqa: E402
    FlowController,
    FlowStep,
    match_step_branch,
    parse_step_ref,
)

# 真实 zh 六步模板的 ref 形态（b0d50586a040，变量/分支/注意行照抄结构）。
STEP1_REF = (
    "您好，请问是{姓名}吗？\n"
    "如果客户听不清、要你再讲一次 → 放慢语速再问一次，不要自报家门。\n"
    "如果客户说打错电话/不是本人 → 礼貌道歉收线，不要继续说下去。"
)
STEP2_REF = (
    "我们是集运中转仓，这次致电是想通知您，您有一件货件在中转仓打包期间遗失了。"
    "由于我们工作上的失误，给您带来不便和损失，我们一定会向您提供合理的赔偿。"
    "想问下您还记不记得当时买的是什么货品呢？\n"
    "如果客户说不记得 → 没关系，我们会帮您核对订单，直接帮您查是哪一件。\n"
    "如果客户问为什么打电话 → 就说核对到一件他的货件出了问题，需要他配合确认。"
)
STEP4_REF = (
    "首先，我们会先核实您这件货品的订单金额：如果金额不足 100 元，我们会根据香港速递条例，"
    "帮您申请 300 到 600 元的赔偿；金额大于 200 元的，就按货价 2 至 3 倍赔偿。"
    "您看这个方案可以接受吗？\n"
    "如果客户问为什么这样赔 → 简单说是按香港速递条例和快递保险标准。\n"
    "如果客户嫌少/不接受 → 不要承诺额外金额,说会帮客户争取最高标准,记录意见。\n"
    "如果客户问什么时候收到 → 核实好订单金额之后就办理。\n"
    "注意:三档金额跟足这个标准讲,不要自创金额。\n"
    "注意:这步只讲赔偿方案,不用收号码,号码在下一步办理才收。"
)


def _tpl(ref: str, say: bool = False) -> dict:
    import json

    return {"steps_json": json.dumps([{"goal": "测试步", "ref": ref, "say": say}])}


# ---- ① parse_step_ref ----


def test_parse_step_ref_script_branches_notes():
    parts = parse_step_ref(STEP4_REF)
    assert parts.script.startswith("首先，我们会先核实")
    assert not parts.script.startswith("如果客户")
    assert [c for c, _ in parts.branches] == ["问为什么这样赔", "嫌少/不接受", "问什么时候收到"]
    assert parts.branches[0][1].startswith("简单说是按香港速递条例")
    assert parts.notes == [
        "三档金额跟足这个标准讲,不要自创金额。",
        "这步只讲赔偿方案,不用收号码,号码在下一步办理才收。",
    ]


def test_parse_step_ref_no_branches():
    parts = parse_step_ref("直接讲一赔二赔付。")
    assert parts.script == "直接讲一赔二赔付。"
    assert parts.branches == []
    assert parts.notes == []


def test_parse_step_ref_blank_and_dunder_lines_ignored():
    parts = parse_step_ref("正稿。\n\n如果客户问 X → 就 Y。\n无关行不进分支。")
    assert parts.script == "正稿。"
    assert parts.branches == [("问 X", "就 Y。")]
    assert parts.notes == []


# ---- ② match_step_branch：verdict 家族优先、关键词 bigram 兜底 ----


def test_match_question_branch_by_keyword():
    parts = parse_step_ref(STEP4_REF)
    m = match_step_branch(parts, "为什么赔这么多？", "question")
    assert m is not None and m[0] == "问为什么这样赔"


def test_match_question_branch_disambiguates_by_overlap():
    parts = parse_step_ref(STEP4_REF)
    m = match_step_branch(parts, "什么时候能收到钱呢", "question")
    assert m is not None and m[0] == "问什么时候收到"


def test_match_objection_branch():
    parts = parse_step_ref(STEP4_REF)
    m = match_step_branch(parts, "太少了，我不接受。", "objection")
    assert m is not None and m[0] == "嫌少/不接受"


def test_match_unclear_family_branch():
    parts = parse_step_ref(STEP2_REF)
    m = match_step_branch(parts, "啊，不记得了。", "unclear")
    assert m is not None and "不记得" in m[0]


def test_match_repeat_branch():
    parts = parse_step_ref(STEP1_REF)
    m = match_step_branch(parts, "你说什么？再说一次。", "repeat")
    assert m is not None and "听不清" in m[0]


def test_match_no_hit_returns_none_not_full_ref():
    # 「你说什么东西？」在无问族分支的步上无命中——必须返回 None,
    # 调用方因此不再把正稿/全分支递给模型(call-8fa17d2b 整段重念根因)。
    parts = parse_step_ref(STEP1_REF)
    assert match_step_branch(parts, "什么东西？你说什么东西？", "question") is None


# ---- ③ current_step_text 渐进披露 ----


def _fc(ref: str, say: bool = False) -> FlowController:
    import json

    tpl = {
        "steps_json": json.dumps(
            [{"goal": "开场", "ref": "您好。"}, {"goal": "测试步", "ref": ref, "say": say}]
        )
    }
    fc = FlowController.from_template(tpl, {"姓名": "普哥"})
    fc.current = 1  # 跳过开场步,模拟流程已推进到目标步
    fc.opening_played = True
    return fc


def test_first_turn_at_step_includes_script_not_branches():
    fc = _fc(STEP4_REF)
    fc.last_verdict = "question"
    fc.last_user_text = "好的"
    fc._just_advanced = True  # 进入本步首轮
    text = fc.current_step_text()
    assert "首先，我们会先核实" in text  # 底稿在
    assert "如果客户" not in text and "问为什么这样赔" not in text  # 分支不在
    assert "注意:" in text  # 操作性事实恒在


def test_later_turn_replaces_script_with_matched_branch():
    fc = _fc(STEP4_REF)
    fc._just_advanced = True
    fc.current_step_text()  # 消耗首轮
    fc.last_verdict = "question"
    fc.last_user_text = "为什么赔这么多？"
    text = fc.current_step_text()
    assert "首先，我们会先核实" not in text  # 正稿退场(已入对话史)
    assert "按香港速递条例和快递保险标准" in text  # 命中分支的应对在
    assert "嫌少/不接受" not in text  # 未命中分支不进
    assert "注意:" in text


def test_later_turn_no_branch_hit_drops_script_entirely():
    fc = _fc(STEP2_REF)
    fc._just_advanced = True
    fc.current_step_text()
    fc.last_verdict = "question"
    fc.last_user_text = "什么东西？你说什么东西？"
    text = fc.current_step_text()
    assert "我们是集运中转仓" not in text  # 不再把正稿递回去
    assert "客户在提问" in text  # verdict 指引仍在


def test_say_step_never_injects_script_even_first_turn():
    fc = _fc(STEP2_REF, say=True)
    fc.said_steps.add(0)
    fc._just_advanced = True
    fc.last_verdict = "unclear"
    fc.last_user_text = "啊，我不记得了。"
    text = fc.current_step_text()
    assert "我们是集运中转仓" not in text
    assert "核对订单" in text  # 命中的不记得分支在


def test_vars_rendered_in_branch_response():
    ref = (
        "正稿行。\n"
        "如果客户问联系方式 → 就请客户提供{聯絡方式}号码。"
    )
    fc = FlowController.from_template(_tpl(ref), {"聯絡方式": "微信"})
    fc.current = 0
    fc.opening_played = True
    fc.last_verdict = "question"
    fc.last_user_text = "怎么联系你们"
    text = fc.current_step_text()
    assert "微信号码" in text
    assert "{聯絡方式}" not in text


# ---- ④ DEFER 短应承 verdict ----


def test_defer_verdict_phrases():
    from agent_runtime.flow import DEFER, decide_advance

    for t in [
        "那我查一下。下先。",
        "我先查一下，有了我再通知你。",
        "稍等，我看看。",
        "回头再联系你。",
        "let me check and get back to you",
    ]:
        assert decide_advance(t, facts={}) == DEFER, t


def test_defer_not_triggered_by_normal_talk():
    from agent_runtime.flow import decide_advance

    assert decide_advance("好的", facts={}) == "confirm"
    assert decide_advance("什么东西？", facts={}) == "question"
    assert decide_advance("我买的手机壳丢了", facts={}) != "defer"


def test_defer_never_advances_any_step():
    from agent_runtime.flow import DEFER, should_auto_advance

    assert not should_auto_advance(current=0, goal="身份", ref="r", user_text="等一下", verdict=DEFER)
    assert not should_auto_advance(
        current=1, goal="通知", ref="r", user_text="我先查一下", verdict=DEFER, say_step=True
    )
    assert not should_auto_advance(current=2, goal="核实", ref="r", user_text="稍等", verdict=DEFER)


def test_defer_ack_line_three_languages():
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
    from agent_runtime.agent import _defer_ack_line

    assert "等您" in _defer_ack_line("zh")
    assert "等您" in _defer_ack_line("cantonese")
    assert "stay on the line" in _defer_ack_line("en")
