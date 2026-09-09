"""沉默心跳(_nudge_line/_farewell_line):电话节奏的沉默跟进与收线直念文本。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _effective_nudge_max,
    _farewell_line,
    _is_test_object_name,
    _nudge_line,
    _nudge_should_fire,
)


def test_nudge_cantonese_with_name():
    out = _nudge_line("陳先生", "cantonese", 0)
    assert out == "陳先生，你仲喺度嗎？"
    # 輪換骨架:同一通內連續心跳唔重樣(舊 LLM 版兩連發一字不差,2026-09-06 實證)
    assert _nudge_line("陳先生", "cantonese", 1) != out
    assert _nudge_line("陳先生", "cantonese", 2) not in {out, _nudge_line("陳先生", "cantonese", 1)}


def test_nudge_mandarin_without_name():
    out = _nudge_line("", "zh", 0)
    assert "您还在吗" in out
    assert "，" not in out.split("？")[0] or out.startswith("您")  # 无名不带称呼逗号


def test_nudge_english():
    out = _nudge_line("Mr. Chan", "en", 0)
    assert "Mr. Chan" in out and "still there" in out


def test_nudge_count_clamped():
    # count 超界钳到最后一个骨架,唔会 IndexError
    assert _nudge_line("X", "zh", 99) == _nudge_line("X", "zh", 2)


def test_farewell_three_languages():
    # 兩次心跳都冇回應 → 禮貌收線直念(多謝+陣間再搵+拜拜,一句講完就停)。
    out = _farewell_line("陳先生", "cantonese")
    assert "陣間再搵你" in out and "拜拜" in out
    zh = _farewell_line("", "zh")
    assert "稍后再联系" in zh and "再见" in zh
    en = _farewell_line("Mr. Chan", "en")
    assert "goodbye" in en.lower()


def test_nudge_should_fire_guard_windows():
    d = 8.0
    # 全新会话(无任何时间戳)→ 允许(等 greeting 播完的 listening 已 arm)
    assert _nudge_should_fire(100.0, 0.0, 0.0, d)
    # AI 啱講完 < delay → 唔跳(俾客戶反應)
    assert not _nudge_should_fire(100.0, 97.0, 90.0, d)
    # AI 講完夠耐、客戶久未開聲 → 跳
    assert _nudge_should_fire(100.0, 90.0, 50.0, d)
    # 客戶啱講完而答案未出(用戶新過回覆,≤2×delay)→ 唔跳(唔好頂替真答案)
    assert not _nudge_should_fire(100.0, 90.0, 96.0, d)
    # 恰 2×delay 邊界都唔跳(≤ 收緊:2026-09-06 call-03a3295c 恰 16.0s 心跳
    # 頂替真答案「你把微信号」實證)
    assert not _nudge_should_fire(116.0, 90.0, 100.0, d)
    # 超 2×delay 仍無聲 → 兜底跳(答案可能失敗)
    assert _nudge_should_fire(100.0, 90.0, 73.0, d)


def test_test_object_name_family():
    # 与 tools/bok.py clean-testdata 同一套前缀族
    assert _is_test_object_name("E2E-zh-1788952794")
    assert _is_test_object_name("E2E-cantonese-1")
    assert _is_test_object_name("soak1")
    assert _is_test_object_name("并发-4路-1")
    assert _is_test_object_name("LOAD-20260909")
    assert _is_test_object_name("边角-e1")
    assert _is_test_object_name("多轮-对话3")
    assert _is_test_object_name("probe-filler")
    # 真实客户对象名不能误伤
    assert not _is_test_object_name("陳先生")
    assert not _is_test_object_name("E2E客服")  # 人设名以「客服」结尾,对象名才是门
    assert not _is_test_object_name("")


def test_effective_nudge_max_test_object_immunity(monkeypatch):
    monkeypatch.setenv("BOK_E2E_NUDGE_IMMUNE", "1")
    # 测试对象 → 心跳整条关(0:arm 注册/farewell/12s 自动收线全部不挂)
    assert _effective_nudge_max("2", "E2E-zh-1") == 0
    assert _effective_nudge_max("2", "边角-e1") == 0
    # 真实对象照常
    assert _effective_nudge_max("2", "陳先生") == 2
    # 显式关闭豁免 → 测试对象恢复心跳(逃生口)
    monkeypatch.setenv("BOK_E2E_NUDGE_IMMUNE", "0")
    assert _effective_nudge_max("2", "E2E-zh-1") == 2
    # env 本来就关心跳 → 保持 0,豁免逻辑不碍事
    monkeypatch.setenv("BOK_E2E_NUDGE_IMMUNE", "1")
    assert _effective_nudge_max("0", "E2E-zh-1") == 0
