"""罐头/分支文本内部指令守卫（M-22①,2026-09-23 修复波#4）。

判据钉死在 task-4 全批轮 M1 实弹教练文案上（zh/en/粤全部正例取自
reports/prod-readiness/04-quality/calls/*.turns.txt 被逐字念给客户的原文）；
反例=同批正常出声的分支应答（「没关系，我们会帮您核对订单…」10 通级复用行）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("packages/core",):
    sp = str(ROOT / p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from bok_voice_core.canned_guard import (  # noqa: E402
    coach_hits,
    is_internal_instruction,
    strip_branch_action_prefix,
)

# ---- 正例:task-4 M1 实弹被出声的教练文案(逐字) ----

T4_COACH_LINES = [
    # zh/s6 防诈轮(「说要核对订单…」×6)
    "说要核对订单才能确认到，去下一步问平台。",
    # zh/s3 异议轮(「说明是集运仓…语气诚恳不争辩；答完带回…」)
    "说明是集运仓按收件登记信息来电核实，语气诚恳不争辩；答完带回货品问题。",
    # zh/s1 报号轮(「提示客户看手机里最近的购物订单。」)
    "提示客户看手机里最近的购物订单。",
    # 粤 s3(「话係集运仓…语气诚恳唔好驳；答完带返…」)
    "话係集运仓按收件登记资料来电核实，语气诚恳唔好驳；答完带返货品问题。",
    "话要核对订单先可以确认到，去下一步问平台。",
    # en/s4+en/s6(admit/say/suggest/listen/briefly say 五族)
    "admit the packing mistake, apologise sincerely, don't shift blame; "
    "then bring the conversation back.",
    "say we're calling on the registered recipient details to verify, "
    "stay sincere, don't argue; then bring the conversation back.",
    "suggest checking recent orders in the shopping apps.",
    "listen carefully, acknowledge briefly, then go to the next step (platform).",
    "briefly say it follows the Hong Kong courier regulations, starting from "
    "300 dollars; details come at the compensation step.",
    "say we need to verify the orders to confirm, go to the next step (platform).",
]


def test_t4_coach_lines_all_blocked():
    for line in T4_COACH_LINES:
        assert is_internal_instruction(line), f"应判内部指令: {line!r}"
        assert coach_hits(line), f"应给出命中信号: {line!r}"


# ---- 反例:同批正常出声/常见分支应答不得误伤 ----

T4_SPEAKABLE_LINES = [
    # zh 罐头复用行(10 通级):首(person)人称台词,去下一步是话面对客户讲的
    "没关系，我们会帮您核对订单，然后去下一步问平台。",
    # en 同族
    "no problem, we'll help verify the orders, go to the next step (platform).",
    # 拒收线台词(test_branch_actions._REF_WRONG_NUMBER)
    "唔好意思打搅咗，我哋再核对下资料，拜拜",
    # 常规分支应答(test_branch_canned/_REF_PLATFORM)
    "我哋會按平台規則盡量幫您爭取。",
    "唔緊要，打開訂單看看就有平台名。",
    "您的运费是三十元。",
    "好的，不着急，您慢慢看，我在电话这边等您。",
    # 粤 customer-facing 「话你知/话俾」顶真用法不得误伤
    "话你知，我哋係集運倉庫。",
    "话俾你知，件貨兩日內賠到。",
    # en "Don't worry" 合法台词(刻意不收 don't 头锚的理由)
    "Don't worry, we'll check it for you right away.",
    # 说不定(说 头锚的豁免位)
    "说不定可以帮您争取到赔偿。",
]


def test_speakable_lines_not_blocked():
    for line in T4_SPEAKABLE_LINES:
        assert not is_internal_instruction(line), f"误伤可念台词: {line!r}"


# ---- 残留指令括号族 ----


def test_residual_bracket_marker_blocked():
    # 动作标记经 parse_branch_action 消费后仍见【…】=内部标记
    assert is_internal_instruction("【语气诚恳】你好，请问係边位？")
    assert is_internal_instruction("您好【先核实身份】再问平台。")
    assert "bracket_marker" in coach_hits("您好【先核实身份】再问平台。")


def test_empty_and_plain_text_clean():
    assert coach_hits("") == []
    assert coach_hits("   ") == []
    assert not is_internal_instruction("你好")


# ---- 动作前缀镜像(strip_branch_action_prefix) ----


def test_strip_branch_action_prefix_mirror():
    # 与 flow.parse_branch_action 同款:识别到标记一律消费,余下文本才受检
    assert strip_branch_action_prefix("【收线】唔好意思打搅咗") == "唔好意思打搅咗"
    assert strip_branch_action_prefix("【挂断】拜拜") == "拜拜"
    assert strip_branch_action_prefix("【跳第 3 步】好的我们看下一项") == "好的我们看下一项"
    assert strip_branch_action_prefix("【留本步】我再讲一次") == "我再讲一次"
    assert strip_branch_action_prefix("【转人工】请稍等") == "请稍等"
    # 无标记/无前缀原样
    assert strip_branch_action_prefix("普通台词") == "普通台词"
    # 消费后余文仍係教练文案 → 被检测(收线标记+指令体)
    body = strip_branch_action_prefix("【收线】礼貌收线，交代安慰话术")
    assert is_internal_instruction(body)
