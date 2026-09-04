"""沉默心跳(_nudge_instruction):一句确认「仲喺度嗎」带返当前步,按语言出牌。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import _nudge_instruction  # noqa: E402


def test_nudge_cantonese_with_name():
    out = _nudge_instruction("陳先生", "cantonese")
    assert "陳先生，你仲喺度嗎" in out
    assert "带返当前这一步" in out or "帶返" not in out  # 指令本身中文,粤语化由 LLM 执行
    assert "绝不重复" in out or "绝不" in out


def test_nudge_mandarin_without_name():
    out = _nudge_instruction("", "zh")
    assert "您还在吗" in out
    assert "一句" in out


def test_nudge_english():
    out = _nudge_instruction("Mr. Chan", "en")
    assert "Mr. Chan" in out and "still there" in out
