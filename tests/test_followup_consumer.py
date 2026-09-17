"""漏斗 v2 Task 4 消费端:工单 kind 分类/确认语/degrade 早触发纯函数。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _followup_ack_line,
    _followup_kind_from_text,
)
from agent_runtime.flow import (  # noqa: E402
    FOLLOWUP_CONF_MIN,
    degrade_boost,
)


def test_kind_complaint():
    assert _followup_kind_from_text("我要投诉件货延误") == "complaint"
    assert _followup_kind_from_text("你哋咁样呃人,我要投訴") == "complaint"


def test_kind_track_order():
    assert _followup_kind_from_text("我个单号係三七七,帮我查下") == "track_order"
    assert _followup_kind_from_text("幫我跟進下進度") == "track_order"
    assert _followup_kind_from_text("please track my parcel") == "track_order"


def test_kind_default_followup():
    assert _followup_kind_from_text("我想问下赔偿先点算") == "followup"
    assert _followup_kind_from_text("") == "followup"


def test_ack_line_three_langs():
    for lang in ("zh", "cantonese", "en"):
        line = _followup_ack_line(lang)
        assert line
        assert "24" in line  # 诚实降级话术必须带 SLA,唔装查


def test_degrade_boost_threshold():
    # 高置信 degrade_question → 抬到降级门槛(3)
    assert degrade_boost(0, "degrade_question", 0.8) == 3
    assert degrade_boost(2, "degrade_question", 0.9) == 3
    # 已过门槛唔倒退
    assert degrade_boost(5, "degrade_question", 0.8) == 5
    # 低置信/其它 route 唔动
    assert degrade_boost(2, "degrade_question", 0.5) == 2
    assert degrade_boost(2, "register_followup", 0.9) == 2
    assert degrade_boost(2, "keep", 0.9) == 2


def test_conf_threshold_value():
    assert FOLLOWUP_CONF_MIN == 0.7
