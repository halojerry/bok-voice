from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control-plane"))

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.campaign import campaign_tick, item_status_for_call


def _manifest(session_id: str):
    from bok_voice_core.types import CallMode, SessionManifest

    return SessionManifest(
        session_id=session_id,
        account_id="acc-001",
        object_id="",
        persona_id="",
        mode=CallMode.LIVE,
        direction="outbound",
        language="zh",
        providers={},
    )


def test_item_status_for_call():
    assert item_status_for_call({"status": "ended", "disposition": ""}) == "done"
    assert item_status_for_call({"status": "ended", "disposition": "completed"}) == "done"
    assert item_status_for_call({"status": "ended", "disposition": "no_answer"}) == "no_answer"
    assert item_status_for_call({"status": "ended", "disposition": "declined"}) == "done"
    assert item_status_for_call({"status": "failed", "disposition": ""}) == "failed"


def test_tick_harvests_terminal_and_starts_next(monkeypatch):
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=5, object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    items = repo.list_items(c["id"])
    # 手工把 item 拨中并配一通已结束通话 → tick 应收割终态
    call = repo.create_call(_manifest("call-c1"))
    repo.update_call(call["id"], status="ended", disposition="no_answer")
    repo.update_item(items[0]["id"], status="dialing", call_id=call["id"])
    dispatched: list[tuple] = []

    async def fake_dispatch(room: str, metadata: str) -> None:
        dispatched.append((room, metadata))

    out = asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    assert repo.get_item(items[0]["id"])["status"] == "no_answer"
    # 名单已尽 → campaign done
    assert repo.get_campaign(c["id"])["status"] == "done"
    assert out == {"harvested": 1, "started": 0, "finished": 1}


def test_tick_starts_first_pending_serially_and_carries_dial_block():
    repo = InMemoryBusinessRepository()
    objs = [
        repo.create_object("acc-001", {"display_name": f"A{i}", "phone": f"+8521111111{i}"})
        for i in range(2)
    ]
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="cantonese", gap_seconds=5,
                             object_ids=[o["id"] for o in objs],
                             scenarios={objs[1]["id"]: "no_answer"})
    repo.update_campaign(c["id"], status="running")
    repo.save_settings({"sip": {"mode": "real", "trunk_id": "ST_x", "max_call_duration_s": 0}})
    dispatched: list[tuple] = []

    async def fake_dispatch(room: str, metadata: str) -> None:
        dispatched.append((room, metadata))

    out = asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    # 串行：只起一通（第二路仍 pending）
    assert out == {"harvested": 0, "started": 1, "finished": 0}
    assert len(dispatched) == 1
    items = repo.list_items(c["id"])
    assert items[0]["status"] == "dialing" and items[0]["call_id"]
    assert items[1]["status"] == "pending" and items[1]["call_id"] == ""
    room, metadata = dispatched[0]
    assert room == items[0]["call_id"]
    payload = json.loads(metadata)
    assert payload["call_id"] == items[0]["call_id"]
    dial = payload["dial"]
    assert dial["to"] == items[0]["phone"]
    assert dial["mode"] == "real" and dial["trunk_id"] == "ST_x"
    assert dial["campaign_item_id"] == items[0]["id"]
    assert dial["language"] == "cantonese" and dial["scenario"] == ""
    # 0 值数字字段必须兜默认（否则静默变「无保险丝」）
    assert dial["max_call_duration_s"] == 600
    # 通话已落库且带被叫号
    call = repo.get_call(items[0]["call_id"])
    assert call["contact_phone"] == items[0]["phone"]
    assert repo.find_item_by_call(items[0]["call_id"])["id"] == items[0]["id"]


def test_tick_dial_block_carries_script_for_object():
    """dial 块带 `script`（campaign scripts 按 object_id 取）：mock 客户才有台词。

    无剧本对象 → 空数组（agent 侧默认已是 []，子进程再有语言默认兜底）。
    """
    repo = InMemoryBusinessRepository()
    objs = [
        repo.create_object("acc-001", {"display_name": f"A{i}", "phone": f"+8521111111{i}"})
        for i in range(2)
    ]
    lines = ["你好", "我WhatsApp係", "六四三二零一一一"]
    # gap 置 0 实际仍兜 5s（`_gap_seconds()` 对 0/空值兜 DEFAULT_GAP_SECONDS）——
    # 第二通能起拨是靠下面把首件终态的 updated_at 手动拨回过期锚，不是靠 gap=0。
    # 冻结冷却语义另有 test_gap_seconds_delays_next_call 专测。
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=0,
                             object_ids=[o["id"] for o in objs],
                             scenarios={objs[0]["id"]: "answer",
                                        objs[1]["id"]: "no_answer"},
                             scripts={objs[0]["id"]: lines})
    repo.update_campaign(c["id"], status="running")
    dispatched: list[tuple] = []

    async def fake_dispatch(room: str, metadata: str) -> None:
        dispatched.append((room, metadata))

    out = asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    assert out["started"] == 1
    dial = json.loads(dispatched[0][1])["dial"]
    assert dial["scenario"] == "answer"
    assert dial["script"] == lines
    # 第二路（no_answer，无台词）尚未起拨；模拟收割后起第二通，script 应为空数组。
    # 收割写入的 updated_at 是 now，冷却会挡住下一轮起拨 → 把它拨回过期时间
    # （gap 冷却另有 test_gap_seconds_delays_next_call 专测）。
    from datetime import datetime, timedelta, timezone

    item0 = repo.list_items(c["id"])[0]
    repo.update_call(item0["call_id"], status="ended", disposition="completed")
    past = (datetime.now(timezone.utc) - timedelta(seconds=31)).replace(tzinfo=None).isoformat()
    asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    repo.update_item(item0["id"], updated_at=past)
    asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    dial2 = json.loads(dispatched[1][1])["dial"]
    assert dial2["scenario"] == "no_answer" and dial2["script"] == []


def test_tick_dispatch_failure_marks_item_failed():
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=5, object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")

    async def boom(room: str, metadata: str) -> None:
        raise RuntimeError("dispatch down")

    out = asyncio.run(campaign_tick(repo, dispatcher=boom))
    assert out == {"harvested": 0, "started": 1, "finished": 0}
    item = repo.list_items(c["id"])[0]
    assert item["status"] == "failed" and "dispatch down" in item["last_error"]
    assert item["call_id"]  # 通话仍建了，便于排查
    # 无 pending / 无进行中 → 下一轮判 done
    out2 = asyncio.run(campaign_tick(repo, dispatcher=boom))
    assert out2 == {"harvested": 0, "started": 0, "finished": 1}
    assert repo.get_campaign(c["id"])["status"] == "done"


def test_tick_skips_inflight_and_is_idempotent_on_harvest():
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=5, object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    item = repo.list_items(c["id"])[0]
    call = repo.create_call(_manifest("call-c9"))
    repo.update_call(call["id"], status="ended", disposition="")
    repo.update_item(item["id"], status="in_call", call_id=call["id"])

    async def noop(room: str, metadata: str) -> None:  # pragma: no cover
        raise AssertionError("不应再起新通话")

    out = asyncio.run(campaign_tick(repo, dispatcher=noop))
    # 收割与收尾同轮完成：最后一个 item 落终态后名单已尽
    assert out == {"harvested": 1, "started": 0, "finished": 1}
    assert repo.get_item(item["id"])["status"] == "done"
    assert repo.get_campaign(c["id"])["status"] == "done"
    # 收割幂等：campaign 已 done，后续 tick 不再重复计数
    out2 = asyncio.run(campaign_tick(repo, dispatcher=noop))
    assert out2 == {"harvested": 0, "started": 0, "finished": 0}


def test_tick_ignores_non_running_and_unknown_disposition():
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=5, object_ids=[obj["id"]])
    # 仍 draft → tick 不碰
    out = asyncio.run(campaign_tick(repo, dispatcher=_noop_dispatch))
    assert out == {"harvested": 0, "started": 0, "finished": 0}
    assert repo.get_campaign(c["id"])["status"] == "draft"
    assert repo.list_items(c["id"])[0]["status"] == "pending"
    # declined（告别自动收线）算 done
    assert item_status_for_call({"status": "ended", "disposition": "declined"}) == "done"
    assert item_status_for_call({"status": "ended", "disposition": "abandoned"}) == "done"
    assert item_status_for_call({"status": "failed", "disposition": "no_answer"}) == "failed"
    assert item_status_for_call({"status": "ended", "disposition": "rejected"}) == "rejected"


def test_gap_seconds_delays_next_call():
    """I-1：上一通终态后必须等满 gap_seconds 才起下一通（同轮收割+起拨不得压成 0s）。"""
    from datetime import datetime, timedelta, timezone

    repo = InMemoryBusinessRepository()
    objs = [
        repo.create_object("acc-001", {"display_name": f"A{i}", "phone": f"+8521111111{i}"})
        for i in range(2)
    ]
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=30, object_ids=[o["id"] for o in objs])
    repo.update_campaign(c["id"], status="running")
    items = repo.list_items(c["id"])
    call = repo.create_call(_manifest("call-gap1"))
    repo.update_call(call["id"], status="ended", disposition="completed")
    repo.update_item(items[0]["id"], status="dialing", call_id=call["id"])
    dispatched: list[tuple] = []

    async def fake_dispatch(room: str, metadata: str) -> None:
        dispatched.append((room, metadata))

    # item 刚终态（收割写 now）→ 冷却未满，只收割不起拨
    out = asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    assert out == {"harvested": 1, "started": 0, "finished": 0}
    assert repo.get_item(items[0]["id"])["status"] == "done"
    assert dispatched == []
    assert repo.list_items(c["id"])[1]["status"] == "pending"

    # 把终态 item 的 updated_at 拨回 31s 前（> gap=30）→ 下一轮起拨
    past = (datetime.now(timezone.utc) - timedelta(seconds=31)).replace(tzinfo=None).isoformat()
    repo.update_item(items[0]["id"], updated_at=past)
    out2 = asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    assert out2 == {"harvested": 0, "started": 1, "finished": 0}
    assert len(dispatched) == 1
    assert repo.list_items(c["id"])[1]["status"] == "dialing"


def test_gap_cooldown_ignores_skipped_anchor():
    """T10 遗留修复：建仓即 skipped 的 item 不得门控首通（锚=真实拨过的终态 item）。"""
    from bok_voice_business_db.repository import InMemoryBusinessRepository as Repo

    repo = Repo()
    no_phone = repo.create_object("acc-001", {"display_name": "无号", "phone": ""})
    runner = repo.create_object("acc-001", {"display_name": "有号", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=30,
                             object_ids=[no_phone["id"], runner["id"]])
    repo.update_campaign(c["id"], status="running")
    items = repo.list_items(c["id"])
    # 建仓顺序：skipped 在前（其 updated_at 即建仓 now）→ 首通不得被它挡住
    assert items[0]["status"] == "skipped" and items[1]["status"] == "pending"
    dispatched: list[tuple] = []

    async def fake_dispatch(room: str, metadata: str) -> None:
        dispatched.append((room, metadata))

    out = asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    assert out == {"harvested": 0, "started": 1, "finished": 0}
    assert len(dispatched) == 1
    assert repo.list_items(c["id"])[1]["status"] == "dialing"


async def _noop_dispatch(room: str, metadata: str) -> None:  # pragma: no cover
    raise AssertionError("不应派发")


