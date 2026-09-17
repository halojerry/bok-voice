"""campaign 调度纯函数单测（2026-09-17）：时段窗/重拨/并发，全部注入 now，不碰真实时钟。"""
from __future__ import annotations

from datetime import datetime

from control_plane.campaign import (concurrency_cap, parse_call_windows,
                                    redispatch_due, redispatch_policy,
                                    within_call_windows)

NOW = datetime(2026, 9, 17, 10, 0, 0)  # 周四


def test_parse_call_windows_normalizes_and_caps_at_three():
    raw = [{"days": [3, 1, 1, 9], "start": "8:05", "end": "18:00"},
           {"days": [], "start": "08:00", "end": "18:00"},          # days 空 → 丢
           {"days": [1], "start": "18:00", "end": "08:00"},         # start>=end → 丢（跨零点显式不支持解析层）
           {"days": [6], "start": "09:00", "end": "12:00"},
           {"days": [7], "start": "09:00", "end": "12:00"},
           {"days": [2], "start": "09:00", "end": "12:00"}]         # 超出 3 组 → 截断
    assert parse_call_windows(raw) == [
        {"days": [1, 3], "start": "08:05", "end": "18:00"},
        {"days": [6], "start": "09:00", "end": "12:00"},
        {"days": [7], "start": "09:00", "end": "12:00"}]
    assert parse_call_windows(None) == []
    assert parse_call_windows("not-json") == []
    assert parse_call_windows([{"days": "1", "start": "08:00", "end": "09:00"}]) == []


def test_within_call_windows():
    windows = [{"days": [4], "start": "08:00", "end": "18:00"}]  # 周四
    assert within_call_windows(NOW, windows) is True
    assert within_call_windows(NOW.replace(hour=7, minute=59), windows) is False
    assert within_call_windows(NOW.replace(hour=18, minute=0, second=1), windows) is False
    assert within_call_windows(NOW.replace(hour=18, minute=0), windows) is True   # 端点含
    assert within_call_windows(NOW, []) is True                                    # 空窗=不限
    assert within_call_windows(NOW.replace(day=18), windows) is False              # 周五不在 days
    assert within_call_windows(NOW, "bad") is False                                # 非法窗=不放行


def test_redispatch_policy_and_due():
    camp = {"redispatch": {"max_attempts": 3, "interval_minutes": 30, "on": ["no_answer"]}}
    policy = redispatch_policy(camp)
    assert policy["max_attempts"] == 3 and policy["on"] == frozenset({"no_answer"})
    assert redispatch_policy({})["max_attempts"] == 0
    assert redispatch_policy({"redispatch": "junk"})["on"] == frozenset()
    # attempts=1 的首拨 item 永远不算「等重拨」
    assert redispatch_due({"attempts": 1, "status": "pending", "updated_at": NOW.isoformat()}, camp, NOW) is False


def test_redispatch_due():
    camp = {"redispatch": {"max_attempts": 3, "interval_minutes": 30, "on": ["no_answer"]}}
    item_recent = {"attempts": 2, "status": "pending",
                   "updated_at": datetime(2026, 9, 17, 9, 50).isoformat()}
    item_due = {"attempts": 2, "status": "pending",
                "updated_at": datetime(2026, 9, 17, 9, 29).isoformat()}
    assert redispatch_due(item_recent, camp, NOW) is True    # 距上次终态 10min < 30min → 还没到
    assert redispatch_due(item_due, camp, NOW) is False      # ≥30min → 到点可拨
    assert redispatch_due(item_due, {}, NOW) is False        # 无策略 → 不等（但不该被调用）
    stale = {"attempts": 2, "status": "pending", "updated_at": "garbage"}
    assert redispatch_due(stale, camp, NOW) is False         # 解析失败=放行（不卡死名单）


def test_concurrency_cap():
    assert concurrency_cap({"max_concurrency": 0}) == 0
    assert concurrency_cap({"max_concurrency": 4}) == 4
    assert concurrency_cap({}) == 1
    assert concurrency_cap({"max_concurrency": "x"}) == 1
