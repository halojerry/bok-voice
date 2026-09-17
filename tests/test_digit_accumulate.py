"""B2 数字句跨步累积(2026-09-17):非 WA 步的快递单号/电话拆段自愈。

拆轮裸露口:join-hold 只认「≥2 数字/系词尾/词表前缀」续接,半截单号句 ≥10 字
照成轮 → AI 答半句、后半截再打断一次。同 WA 姿势暂存攒齐;金额句与疑问句豁免。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _digit_accum_should_stash,
    _digit_confirm_line,
    _digit_reask_line,
    _tracking_numberish,
)


def test_tracking_numberish_context_word():
    # 号码语境词命中:半截单号句进累积。
    assert _tracking_numberish("單號係八六五三二")
    assert _tracking_numberish("我的電話係六四三二")
    assert _tracking_numberish("快递单号是二七一")  # 简体语境词+汉字数字


def test_tracking_numberish_bare_digits():
    # 纯数字续段 ≥4 位:唔使语境词都收(「报出就收」同款下限)。
    assert _tracking_numberish("六四三二五")
    assert _tracking_numberish("8653")


def test_tracking_numberish_amounts_not_matched():
    # 金额/日常短句(3 位、无语境词)唔准吞成累积——「賠三百」係要答的正事。
    assert not _tracking_numberish("賠三百蚊")
    assert not _tracking_numberish("賠二百三")
    assert not _tracking_numberish("你好")
    assert not _tracking_numberish("訂單三百蚊")


def test_tracking_numberish_long_residual_not_matched():
    # 剩余实质字符 >6 = 正常句子夹数字,唔係报号碎片。
    assert not _tracking_numberish("我買咗二百三十蚊嘅嘢")
    assert not _tracking_numberish("我想問下幾時可以先賠一百")


def test_stash_questionish_exempt():
    # 疑问/算式句豁免(call-c76832ac 同族):「一加一等于几」唔准静音两轮。
    assert not _digit_accum_should_stash("一加一等于几", "一加一等于几", 3)


def test_stash_announce_head_zero_digits():
    # 自报头半句(「我的單號係。」零数字)暂存等续段。
    assert _digit_accum_should_stash("我的單號係。", "我的單號係。", 0)
    assert _digit_accum_should_stash("单号是", "单号是", 0)


def test_stash_digit_fragments():
    assert _digit_accum_should_stash("單號係八六五", "單號係八六五", 3)
    assert _digit_accum_should_stash("六四三二五", "六四三二五", 5)


def test_stash_plain_talk_rejected():
    assert not _digit_accum_should_stash("好嘅", "好嘅", 0)
    assert not _digit_accum_should_stash("唔记得呀", "唔记得呀", 0)


def test_full_eight_digits_not_stashed():
    # 攒齐 8 位 → 唔再暂存,放行交侦测/沉淀(合并分支处理)。
    assert not _digit_accum_should_stash("單號係八六五三二七四零", "單號係八六五三二七四零", 8)


def test_confirm_and_reask_lines():
    langs = ("cantonese", "zh", "en")
    confirms = [_digit_confirm_line(l) for l in langs]
    reasks = [_digit_reask_line(l) for l in langs]
    assert all(confirms) and len(set(confirms)) == 3
    assert all(reasks) and len(set(reasks)) == 3
