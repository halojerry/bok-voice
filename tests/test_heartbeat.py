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
    yue = _silence_farewell_instruction("陳先生", "cantonese")
    assert "陣間再搵你" in yue and "拜拜" in yue
    zh = _silence_farewell_instruction("", "zh")
    assert "稍后再联系" in zh and "再见" in zh
    en = _silence_farewell_instruction("Mr. Chan", "en")
    assert "goodbye" in en.lower()
