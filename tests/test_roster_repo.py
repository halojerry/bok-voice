from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bok_voice_business_db import models
from bok_voice_business_db.repository import (
    InMemoryBusinessRepository,
    SqlAlchemyBusinessRepository,
)


def _repo() -> InMemoryBusinessRepository:
    return InMemoryBusinessRepository()


@pytest.fixture()
def sql_repo():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    models.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyBusinessRepository(session)

def test_upsert_creates_and_dedupes():
    repo = _repo()
    a = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="whatsapp", number="64320111", display_name="陈生", summary="s1",
    )
    assert a["status"] == "unclaimed"
    # 同 object+channel+number 重复捕获：更新来源，不新建
    b = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-b", object_id="obj-1",
        channel="whatsapp", number="64320111", display_name="陈生", summary="s2",
    )
    assert b["id"] == a["id"] and b["call_id"] == "call-b" and b["summary"] == "s2"
    assert len(repo.list_roster()) == 1
    # handled 后新捕获另起新行
    repo.update_roster_entry(a["id"], status="handled")
    c = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-c", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    assert c["id"] != a["id"] and c["status"] == "unclaimed"


def test_claim_flow():
    repo = _repo()
    e = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="wechat", number="12345678",
    )
    repo.update_roster_entry(e["id"], status="claimed", claimed_by="acc-001")
    got = repo.get_roster_entry(e["id"])
    assert got["status"] == "claimed" and got["claimed_by"] == "acc-001"
    assert repo.list_roster(status="claimed", channel="wechat")[0]["id"] == e["id"]
    assert repo.list_roster(channel="whatsapp") == []


def test_claimed_at_accepts_iso_string():
    """读侧 claimed_at 是 ISO 字符串，读改写把字符串传回来不能崩。"""
    repo = _repo()
    e = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    got = repo.update_roster_entry(
        e["id"], status="claimed", claimed_by="acc-001",
        claimed_at="2026-09-12T00:00:00+00:00",
    )
    assert got["status"] == "claimed"
    assert got["claimed_at"] == "2026-09-12T00:00:00+00:00"


def test_upsert_preserves_empty_display_name_and_summary():
    """重复捕获传空 display_name/summary 时保留首见值，不清空。"""
    repo = _repo()
    a = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="whatsapp", number="64320111", display_name="陈生", summary="s1",
    )
    b = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-b", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    assert b["id"] == a["id"]
    assert b["display_name"] == "陈生" and b["summary"] == "s1"
    assert b["call_id"] == "call-b"


def test_sqlalchemy_backend_parity(sql_repo):
    """真后端跑同一组语义：去重/新建/handled 后另起行/过滤/claimed_at 字符串。"""
    a = sql_repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="whatsapp", number="64320111", display_name="陈生", summary="s1",
    )
    assert a["status"] == "unclaimed" and a["created_at"]
    b = sql_repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-b", object_id="obj-1",
        channel="whatsapp", number="64320111", summary="s2",
    )
    assert b["id"] == a["id"] and b["call_id"] == "call-b"
    assert b["summary"] == "s2" and b["display_name"] == "陈生"
    assert len(sql_repo.list_roster()) == 1

    claimed = sql_repo.update_roster_entry(
        a["id"], status="claimed", claimed_by="acc-001",
        claimed_at="2026-09-12T00:00:00+00:00",
    )
    assert claimed["status"] == "claimed"
    # SQLite 的 DateTime 列不保留 tz offset（回读为 naive 串），只断言时刻本身。
    # InMemory 直存字符串故保留 offset——两后端此处渲染有别，序列化口径以 SQL 侧为准。
    from datetime import datetime
    assert datetime.fromisoformat(claimed["claimed_at"]).replace(tzinfo=None) == (
        datetime.fromisoformat("2026-09-12T00:00:00+00:00").replace(tzinfo=None))
    assert sql_repo.get_roster_entry(a["id"])["claimed_by"] == "acc-001"
    assert [r["id"] for r in sql_repo.list_roster(status="claimed", channel="whatsapp")] == [a["id"]]
    assert sql_repo.list_roster(channel="wechat") == []
    assert sql_repo.list_roster(account_id="acc-999") == []

    # claimed 行仍属去重池（规格只排除 handled）→ 重复捕获应并回同一行
    same = sql_repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-c", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    assert same["id"] == a["id"] and same["status"] == "claimed"

    # handled 后新捕获另起新行
    sql_repo.update_roster_entry(a["id"], status="handled")
    c = sql_repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-c", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    assert c["id"] != a["id"] and c["status"] == "unclaimed"
    assert sql_repo.get_roster_entry("missing") is None
    assert sql_repo.update_roster_entry("missing", status="claimed") is None


def test_update_roster_entry_empty_claimed_at(sql_repo):
    """未认领行读侧 claimed_at="", 表单读改写原样传回不能崩（SQL 后端）。"""
    e = sql_repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    assert e["claimed_at"] == ""
    got = sql_repo.update_roster_entry(
        e["id"], status="claimed", claimed_by="acc-001",
        claimed_at=e["claimed_at"],  # 空串回写
    )
    assert got["status"] == "claimed"
    assert got["claimed_at"] == ""
    # 真时间戳照常写入
    stamped = sql_repo.update_roster_entry(
        e["id"], claimed_at="2026-09-12T00:00:00+00:00")
    assert stamped["claimed_at"]


def test_update_roster_entry_ignores_unknown_keys():
    """白名单外键（id/created_at）被忽略，两后端同语义。"""
    repo = _repo()
    e = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="whatsapp", number="64320111",
    )
    got = repo.update_roster_entry(
        e["id"], status="claimed", id="hacked", created_at="1970-01-01T00:00:00",
        bogus="x",
    )
    assert got["id"] == e["id"]
    assert got["created_at"] == e["created_at"]
    assert "bogus" not in got
    assert got["status"] == "claimed"


def test_inmemory_empty_claimed_at_roundtrip():
    """InMemory 侧空串回写同样不崩且保持 ""（与 SQL 后端同契约）。"""
    repo = _repo()
    e = repo.upsert_roster_entry(
        account_id="acc-001", call_id="call-a", object_id="obj-1",
        channel="wechat", number="12345678",
    )
    assert e["claimed_at"] == ""
    got = repo.update_roster_entry(e["id"], claimed_at=e["claimed_at"])
    assert got["claimed_at"] == ""
