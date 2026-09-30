"""心跳 nudge 软化（P3.2，spec 2026-09-29 v2 §6）。

Ethan 拍板（2026-09-29「情绪太大很吓人必须换」）：三语软陪伴型文案、去
「喂」开头、间隔 8→12s。新文案=新 TTS 缓存 key，旧吓人条目自然失效。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import _nudge_line  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


def test_cantonese_ear_tested_variants():
    """耳测定稿（2026-09-30 Ethan 逐句评）：①旧第一句原样（评「最好」）；
    ②第二句只去「喂，」；③软陪伴第三句保留（评「还不错」——等待语义末位合适）。"""
    assert _nudge_line("林先生", "cantonese", 0) == "林先生，你仲喺度嗎？"
    assert _nudge_line("林先生", "cantonese", 1) == "林先生，聽唔聽到我講嘢？"
    assert _nudge_line("林先生", "cantonese", 2) == "林先生，唔好意思，你可能喺度谂紧，我等你。"


def test_no_hello_prefix_and_rotation_wraps():
    """「喂，」开头全部退役；count 超界轮换回绕；无 name 时零前缀不炸。"""
    for lang in ("cantonese", "zh", "en"):
        for c in range(6):
            line = _nudge_line("陳", lang, c)
            assert not line.lstrip("陳 ,").startswith(("喂，", "喂,", "Hello?")), f"{lang}/{c}: {line}"
            assert line  # 恒有内容
    assert _nudge_line("", "cantonese", 0).startswith("你仲喺度") is True


def test_zh_and_en_variants():
    assert _nudge_line("陈先生", "zh", 0) == "陈先生，您还在吗？"
    assert _nudge_line("陈先生", "zh", 1) == "陈先生，能听到我说话吗？"
    assert _nudge_line("陈先生", "zh", 2) == "陈先生，不好意思，您可能在想事情，我等您。"
    assert _nudge_line("Mr. Lin", "en", 0) == "Mr. Lin, are you still there?"
    assert _nudge_line("Mr. Lin", "en", 1) == "Mr. Lin, can you hear me?"
    assert _nudge_line("Mr. Lin", "en", 2) == "Mr. Lin, sorry — take your time, I'll wait."


def test_default_interval_12s_pinned():
    """SILENCE_NUDGE_SECONDS 默认 8→12（源级 pin，防回退）。"""
    assert 'os.environ.get("SILENCE_NUDGE_SECONDS", "12")' in AGENT_SRC
