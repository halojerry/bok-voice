from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bok_voice_business_db import models
from bok_voice_business_db.repository import (
    InMemoryBusinessRepository,
    SqlAlchemyBusinessRepository,
)


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
