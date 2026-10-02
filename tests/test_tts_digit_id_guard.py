"""标识符数字槽逐位读（2026-09-27，MiniMax 数值读法事故修复）。

取证：缓存罐头音频过 ASR 复核——MiniMax 把 ≤4 位阿拉伯数字串按**数值**读
（"1459"(尾号)→一千四百五十九、"6699"(tracking ending)→六千六百九十九），
而标识符槽该逐位读（一四五九 / 六六九九）；≥5 位顺串本就逐位。金额（三百蚊）
与数量读数值才对，一律不碰。

判据本体=`_digitize_id_slots`（纯函数，livekit_plugins.py 模块级），接线在
`MiniMaxTTS._prep_outbound`（数字槽逐位化先做，再走标记剥离）。
"""

from __future__ import annotations

import pytest

from agent_runtime.providers.livekit_plugins import MiniMaxTTS, _digitize_id_slots

# ---- 正例：标识符语境 3/4 位串 → 逐位汉字（含真实 turn 取证形状） ----

_DIGITIZE_CASES = [
    # 尾号/单号（zh 前缀）
    ("尾号1459", "尾号一四五九"),
    ("尾號：1459", "尾號：一四五九"),
    ("单号 9156", "单号 九一五六"),
    ("編號是 6699。", "編號是 六六九九。"),
    ("号码 1459 对不对", "号码 一四五九 对不对"),
    # 电话/热线（zh 前缀 + 连字符号段）
    ("客服熱線係 852-1234-5678", "客服熱線係 八五二-一二三四-五六七八"),
    ("电话是 6699", "电话是 六六九九"),
    # en 引导词（"tracking ending 6699" 真实取证形状）
    ("tracking ending 6699", "tracking ending 六六九九"),
    ("order number 1459 is on the way", "order number 一四五九 is on the way"),
    # 连字符/字母数字码（"E2E-陳小明-2170嗎" 真实取证形状）
    ("E2E-陳小明-2170嗎", "E2E-陳小明-二一七零嗎"),
    # 多点混合
    ("尾号1459，热线 852-1234-5678", "尾号一四五九，热线 八五二-一二三四-五六七八"),
]

# ---- 反例：金额/数量/其它语境一律不动 ----

_UNCHANGED_CASES = [
    "最低 300 蚊",              # 金额：读三百蚊才对
    "賠償 300元",               # 金额（3 位 + 元）
    "1000元",                   # 金额（4 位 + 元，无标识符语境）
    "申请 300 到 600 元赔偿",   # 金额区间，无标识符语境
    "大概 2000 元左右",         # 金额
    "3天后再联系",              # 数量（1 位，本就不动）
    "1500 个订单",              # 数量后缀
    "12345",                    # ≥5 位：本就逐位读，不动
    "单号123456",               # 标识符语境但 ≥5 位，不动（已逐位）
    "订单号 12345",             # 同上
    "12",                       # 2 位：数值读==逐位读，不动
    "一共 1500",                # 3/4 位但无标识符语境
    "第 2026 步",               # 无标识符语境
    "您好，请问係边位？",       # 无数字
    "",                         # 空串
]


@pytest.mark.parametrize("src, expected", _DIGITIZE_CASES)
def test_digitize_identifier_slots(src, expected):
    assert _digitize_id_slots(src) == expected


@pytest.mark.parametrize("src", _UNCHANGED_CASES)
def test_digitize_never_touches_money_qty_or_non_id(src):
    assert _digitize_id_slots(src) == src


def test_run_regex_does_not_truncate_5plus_digit_runs():
    """≥5 位顺串不得被 run 正则截成前 4 位改写（边界 lookaround 钉死）。"""
    assert _digitize_id_slots("尾号123456") == "尾号123456"
    assert _digitize_id_slots("尾号12345") == "尾号12345"


def test_qty_guard_beats_id_prefix():
    """标识符前缀 + 金额后缀同轮时，金额语境优先（不误改数值读法）。"""
    assert _digitize_id_slots("单号 1000元") == "单号 1000元"


def test_prep_outbound_applies_digitize_on_both_model_tiers(monkeypatch):
    """接线钉死：_prep_outbound 两种档位都过数字槽逐位化（且标记剥离仍生效）。"""
    monkeypatch.delenv("MINIMAX_MODEL", raising=False)
    t28 = MiniMaxTTS(voice="v", api_key="k")  # 缺省 speech-2.8-hd
    assert t28._prep_outbound("尾号1459") == "尾号一四五九"
    assert t28._prep_outbound("(emm)尾号1459<#0.3#>") == "(emm)尾号一四五九<#0.3#>"

    t26 = MiniMaxTTS(voice="v", api_key="k", model_override="speech-2.6-turbo")
    out = t26._prep_outbound("(emm)尾号1459<#0.3#>")
    assert "一四五九" in out and "(emm)" not in out and "<#" not in out
