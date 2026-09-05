"""沉默心跳(_nudge_instruction/_silence_farewell_instruction):电话节奏的沉默跟进与收线指令。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import _nudge_instruction, _silence_farewell_instruction  # noqa: E402


def test_nudge_cantonese_with_name():
    out = _nudge_instruction("陳先生", "cantonese")
    assert "陳先生，你仲喺度嗎" in out
    assert "绝不重复" in out


def test_nudge_mandarin_without_name():
    out = _nudge_instruction("", "zh")
    assert "您还在吗" in out
    assert "一句" in out


def test_nudge_english():
    out = _nudge_instruction("Mr. Chan", "en")
    assert "Mr. Chan" in out and "still there" in out


def test_farewell_three_languages():
    # 兩次心跳都冇回應 → 禮貌收線指令(多謝+陣間再搵+拜拜,一句講完就停)。
    out = _silence_farewell_instruction("陳先生", "cantonese")
    assert "陣間再搵你" in out and "拜拜" in out
    zh = _silence_farewell_instruction("", "zh")
    assert "稍后再联系" in zh and "再见" in zh
    en = _silence_farewell_instruction("Mr. Chan", "en")
    assert "goodbye" in en.lower()


def test_nudge_should_fire_guard_windows():
    from agent_runtime.agent import _nudge_should_fire

    d = 8.0
    # 全新会话(无任何时间戳)→ 允许(等 greeting 播完的 listening 已 arm)
    assert _nudge_should_fire(100.0, 0.0, 0.0, d)
    # AI 啱講完 < delay → 唔跳(俾客戶反應)
    assert not _nudge_should_fire(100.0, 97.0, 90.0, d)
    # AI 講完夠耐、客戶久未開聲 → 跳
    assert _nudge_should_fire(100.0, 90.0, 50.0, d)
    # 客戶啱講完而答案未出(用戶新過回覆,<2×delay)→ 唔跳(唔好頂替真答案)
    assert not _nudge_should_fire(100.0, 90.0, 96.0, d)
    # 同上但超 2×delay 仍無聲 → 兜底跳(答案可能失敗)
    assert _nudge_should_fire(100.0, 90.0, 73.0, d)
