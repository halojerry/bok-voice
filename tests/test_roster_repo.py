from __future__ import annotations

from bok_voice_business_db.repository import InMemoryBusinessRepository


def _repo() -> InMemoryBusinessRepository:
    return InMemoryBusinessRepository()


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
