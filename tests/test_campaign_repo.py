from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bok_voice_business_db import models
from bok_voice_business_db.repository import (
    InMemoryBusinessRepository,
    SqlAlchemyBusinessRepository,
)
from bok_voice_core.policies import select_session_manifest
from bok_voice_core.types import CallMode


def _repo_with_object():
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "陈生", "phone": "+85264320111"})
    return repo, obj


def test_create_campaign_with_items():
    repo, obj = _repo_with_object()
    c = repo.create_campaign(
        "acc-001", name="催件第一波", template_id="", persona_id="",
        language="cantonese", gap_seconds=5, object_ids=[obj["id"]],
        scenarios={obj["id"]: "no_answer"},
    )
    assert c["status"] == "draft" and c["gap_seconds"] == 5
    items = repo.list_items(c["id"])
    assert len(items) == 1
    assert items[0]["phone"] == "+85264320111"
    assert items[0]["status"] == "pending" and items[0]["scenario"] == "no_answer"
    repo.update_item(items[0]["id"], status="dialing", call_id="call-z")
    assert repo.find_item_by_call("call-z")["id"] == items[0]["id"]
    repo.update_campaign(c["id"], status="running")
    assert repo.list_campaigns(status="running")[0]["id"] == c["id"]


@pytest.fixture()
def sql_repo():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    models.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyBusinessRepository(session)


def test_sql_create_campaign_with_items(sql_repo):
    repo = sql_repo
    obj = repo.create_object("acc-001", {"display_name": "陈生", "phone": "+85264320111"})
    c = repo.create_campaign(
        "acc-001", name="催件第一波", template_id="tpl-1", persona_id="per-1",
        language="cantonese", gap_seconds=7, object_ids=[obj["id"]],
        scenarios={obj["id"]: "no_answer"},
    )
    assert c["status"] == "draft" and c["gap_seconds"] == 7
    assert c["finished_at"] == "" and c["created_at"]
    items = repo.list_items(c["id"])
    assert len(items) == 1 and items[0]["seq"] == 0
    assert items[0]["phone"] == "+85264320111" and items[0]["scenario"] == "no_answer"
    assert items[0]["status"] == "pending" and items[0]["attempts"] == 1
    repo.update_item(items[0]["id"], status="dialing", call_id="call-z")
    assert repo.find_item_by_call("call-z")["id"] == items[0]["id"]
    repo.update_campaign(c["id"], status="running", finished_at="2026-09-12T00:00:00+00:00")
    got = repo.get_campaign(c["id"])
    assert got["status"] == "running"
    assert got["finished_at"].startswith("2026-09-12T00:00:00")
    assert repo.list_campaigns(status="running")[0]["id"] == c["id"]


def test_missing_phone_item_is_skipped():
    repo, _ = _repo_with_object()
    no_phone = repo.create_object("acc-001", {"display_name": "李太"})
    runner = repo.create_object("acc-001", {"display_name": "张生", "phone": "+8529"})
    c = repo.create_campaign(
        "acc-001", name="c", template_id="", persona_id="", language="zh",
        gap_seconds=5, object_ids=[no_phone["id"], runner["id"]],
    )
    items = repo.list_items(c["id"])
    assert [i["seq"] for i in items] == [0, 1]
    assert items[0]["status"] == "skipped" and items[0]["last_error"] == "对象无电话"
    assert items[0]["phone"] == ""
    assert items[1]["status"] == "pending" and items[1]["phone"] == "+8529"


def test_missing_phone_item_is_skipped_sql(sql_repo):
    no_phone = sql_repo.create_object("acc-001", {"display_name": "李太"})
    c = sql_repo.create_campaign(
        "acc-001", name="c", template_id="", persona_id="", language="zh",
        gap_seconds=5, object_ids=[no_phone["id"]],
    )
    items = sql_repo.list_items(c["id"])
    assert items[0]["status"] == "skipped" and items[0]["last_error"] == "对象无电话"


# ---- mock 演练台词（scripts：object_id → [句子]）存取 roundtrip ----

def test_campaign_scripts_roundtrip_both_backends(sql_repo):
    """台词存 campaign 级（item.scenario 16 字符装不下）：两后端读出口一致。"""
    for repo in (InMemoryBusinessRepository(), sql_repo):
        objs = [repo.create_object("acc-001", {"display_name": f"O{i}", "phone": f"+852{i}"})
                for i in range(2)]
        scripts = {objs[0]["id"]: ["你好", "我WhatsApp係"], objs[1]["id"]: ["好的再见"]}
        c = repo.create_campaign(
            "acc-001", name="c", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[o["id"] for o in objs], scripts=scripts,
        )
        assert repo.get_campaign_scripts(c["id"]) == scripts
        # 无战役 → {}（不抛）
        assert repo.get_campaign_scripts("nope") == {}


def test_campaign_scripts_sanitize_and_default(sql_repo):
    """空白句剔除、空数组丢弃；不传 scripts 时两后端都回 {}。"""
    for repo in (InMemoryBusinessRepository(), sql_repo):
        obj = repo.create_object("acc-001", {"display_name": "O", "phone": "+8521"})
        plain = repo.create_campaign(
            "acc-001", name="c", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[obj["id"]],
        )
        assert repo.get_campaign_scripts(plain["id"]) == {}
        c = repo.create_campaign(
            "acc-001", name="c2", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[obj["id"]],
            scripts={obj["id"]: ["你好", "  ", ""]},
        )
        assert repo.get_campaign_scripts(c["id"]) == {obj["id"]: ["你好"]}


def test_list_campaigns_filters_and_orders():
    repo, obj = _repo_with_object()
    c1 = repo.create_campaign(
        "acc-001", name="a", template_id="", persona_id="", language="zh",
        gap_seconds=5, object_ids=[obj["id"]],
    )
    c2 = repo.create_campaign(
        "acc-002", name="b", template_id="", persona_id="", language="en",
        gap_seconds=5, object_ids=[],
    )
    assert [c["id"] for c in repo.list_campaigns()] == [c1["id"]]
    assert [c["id"] for c in repo.list_campaigns(account_id="acc-002")] == [c2["id"]]
    assert repo.list_campaigns(status="running") == []
    repo.update_campaign(c1["id"], status="running")
    assert [c["id"] for c in repo.list_campaigns(status="running")] == [c1["id"]]


def test_update_whitelist_mirror_both_backends(sql_repo):
    """未知键与 None 忽略；白名单两后端一致（照 update_roster_entry 姿势）。"""
    for repo in (InMemoryBusinessRepository(), sql_repo):
        obj = repo.create_object("acc-001", {"display_name": "陈生"})
        c = repo.create_campaign(
            "acc-001", name="a", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[obj["id"]],
        )
        repo.update_campaign(c["id"], status="paused", id="hacked", nope=1)
        got = repo.get_campaign(c["id"])
        assert got["status"] == "paused" and got["id"] == c["id"]
        item = repo.list_items(c["id"])[0]
        repo.update_item(item["id"], status="done", attempts=3, id="hacked")
        got_item = repo.get_item(item["id"])
        assert got_item["status"] == "done" and got_item["attempts"] == 3
        assert got_item["id"] == item["id"]
        # None = 不修改
        repo.update_item(item["id"], status=None)
        assert repo.get_item(item["id"])["status"] == "done"


def test_get_missing_returns_none():
    repo, _ = _repo_with_object()
    assert repo.get_campaign("nope") is None
    assert repo.get_item("nope") is None
    assert repo.find_item_by_call("nope") is None
    assert repo.update_campaign("nope", status="done") is None
    assert repo.update_item("nope", status="done") is None
    assert repo.list_items("nope") == []


def test_find_item_by_call_sql(sql_repo):
    obj = sql_repo.create_object("acc-001", {"display_name": "陈生", "phone": "+8521"})
    c = sql_repo.create_campaign(
        "acc-001", name="a", template_id="", persona_id="", language="zh",
        gap_seconds=5, object_ids=[obj["id"]],
    )
    item = sql_repo.list_items(c["id"])[0]
    assert sql_repo.find_item_by_call("call-x") is None
    sql_repo.update_item(item["id"], status="in_call", call_id="call-x")
    assert sql_repo.find_item_by_call("call-x")["id"] == item["id"]


# ---- 战役调度三字段 + 通话时长列（2026-09-17 campaign-scheduling-dashboard Task 1）----

def _manifest():
    """最小建通话 manifest（本文件此前无建通话用例，按 test_business_repo 姿势补）。"""
    return select_session_manifest(
        session_id="call-sched-1",
        account_id="acc-001",
        object_id="obj-1",
        persona_id="",
        mode=CallMode.LIVE,
    )


def _dt(offset_s: int) -> datetime:
    """brief 口径：naive UTC datetime（与 created_at 同域）。"""
    return datetime(2026, 9, 17, 0, 0, 0) + timedelta(seconds=offset_s)


@pytest.fixture(params=["memory", "sql"])
def repo(request, sql_repo):
    """双仓参数化（沿用本文件两既有风格：内存直造 + sql_repo fixture）。"""
    if request.param == "memory":
        return InMemoryBusinessRepository()
    return sql_repo


class TestCampaignSchedulingFields:
    """战役调度三字段（2026-09-17）：双仓 roundtrip + 白名单 + 缺省旧行为。"""

    def test_create_with_scheduling_fields(self, repo):
        camp = repo.create_campaign(
            "acc-001", name="t", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[], call_windows=[{"days": [1, 2], "start": "08:00", "end": "18:00"}],
            max_concurrency=3, redispatch={"max_attempts": 2, "interval_minutes": 30, "on": ["no_answer"]},
        )
        assert camp["call_windows"] == [{"days": [1, 2], "start": "08:00", "end": "18:00"}]
        assert camp["max_concurrency"] == 3
        assert camp["redispatch"] == {"max_attempts": 2, "interval_minutes": 30, "on": ["no_answer"]}

    def test_create_defaults_are_legacy(self, repo):
        camp = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                                    language="zh", gap_seconds=5, object_ids=[])
        assert camp["call_windows"] == []
        assert camp["max_concurrency"] == 1
        assert camp["redispatch"] == {}

    def test_update_whitelist_accepts_scheduling_fields(self, repo):
        camp = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                                    language="zh", gap_seconds=5, object_ids=[])
        updated = repo.update_campaign(camp["id"], max_concurrency=0,
                                       call_windows_json='[{"days":[6],"start":"09:00","end":"12:00"}]',
                                       redispatch_json='{"max_attempts":2,"interval_minutes":15,"on":["no_answer"]}')
        assert updated["max_concurrency"] == 0
        assert updated["call_windows"] == [{"days": [6], "start": "09:00", "end": "12:00"}]
        assert updated["redispatch"]["max_attempts"] == 2

    def test_update_call_time_columns(self, repo):
        call = repo.create_call(_manifest())  # 沿用文件内既有建通话 helper/fixture
        updated = repo.update_call(call["id"], started_at=_dt(0), ended_at=_dt(95), duration_s=95)
        assert updated["duration_s"] == 95 and updated["started_at"] is not None
