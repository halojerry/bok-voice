from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control-plane"))

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.campaign import campaign_tick, item_status_for_call

# 调度循环测试的注入时钟（2026-09-17 Task 3）：取真实 UTC now 的 naive 形态——
# 收割落库的 updated_at 是真实 UTC 墙钟（_utcnow_iso），注入 now 必须与它同一条
# 时间线，重拨间隔断言才确定（固定历史时刻会与真实钟差出半个 interval）。
# 窗口断言不依赖绝对时刻：窗的 days/起止全部由 NOW 动态构造。
NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _window_inside_now() -> list[dict]:
    """覆盖 NOW 的任务窗：[NOW 整点, 下一整点+58m)。任一秒都保证含 NOW。"""
    return [{"days": [NOW.isoweekday()], "start": f"{NOW.hour:02d}:00",
             "end": f"{(NOW.hour + 1) % 24:02d}:58"}]


def _window_outside_now() -> list[dict]:
    """同日但绝不含 NOW 的窗：[NOW+1h, NOW+2h]（模 24，含跨零点形态）。"""
    return [{"days": [NOW.isoweekday()], "start": f"{(NOW.hour + 1) % 24:02d}:00",
             "end": f"{(NOW.hour + 2) % 24:02d}:59"}]


def _ending_dispatch(repo, record: list):
    """fake dispatcher：记录派发并立刻把通话置 ENDED+no_answer（模拟未接通即挂）。

    room 即 call_id（_start_call 以 call_id 作 room 派发），直接落 repo。
    """
    async def dispatch(room: str, metadata: str) -> None:
        record.append(room)
        repo.update_call(room, status="ended", disposition="no_answer")

    return dispatch


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
    # 振铃窗同款兜底：settings 未配 → 30（CP 与 agent 双层默认一致）
    assert dial["ringing_timeout_s"] == 30
    # 通话已落库且带被叫号
    call = repo.get_call(items[0]["call_id"])
    assert call["contact_phone"] == items[0]["phone"]
    assert repo.find_item_by_call(items[0]["call_id"])["id"] == items[0]["id"]


def test_tick_dial_block_carries_configured_ringing_timeout():
    """T7/T8 接缝：settings.sip.ringing_timeout_s 必须进 dial 块（此前无人消费）。

    运营在设置页改 60s，全链路（CP dial 块 → agent dial_outbound）要真收到 60。
    """
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=5, object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    repo.save_settings({"sip": {"mode": "mock", "ringing_timeout_s": 60}})
    dispatched: list[tuple] = []

    async def fake_dispatch(room: str, metadata: str) -> None:
        dispatched.append((room, metadata))

    asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    dial = json.loads(dispatched[0][1])["dial"]
    assert dial["ringing_timeout_s"] == 60


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


def test_tick_snapshots_campaign_template_into_call():
    """战役选定的话术建单即快照——此前该字段只存不读，运营选的话术被静默忽略。"""
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="tpl-wave",
                             persona_id="", language="zh", gap_seconds=5,
                             object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    started: list[str] = []

    async def fake_dispatch(room: str, metadata: str) -> None:
        started.append(room)

    asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))
    assert len(started) == 1
    assert repo.get_call(started[0])["template_id"] == "tpl-wave"


async def _noop_dispatch(room: str, metadata: str) -> None:  # pragma: no cover
    raise AssertionError("不应派发")


# ---- 调度循环接线（2026-09-17 campaign-scheduling-dashboard Task 3）：
# 双层时段窗（全局 ∩ 任务）/ 任务级并发槽位 / 未接通自动重拨。全部注入 now。----

def test_tick_no_dispatch_outside_window():
    """任务窗不含 now → 不起拨、item 不离开 pending、campaign 不误判 done。"""
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=5,
                             call_windows=_window_outside_now(),
                             object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    dispatched: list[str] = []
    out = asyncio.run(campaign_tick(repo, dispatcher=_ending_dispatch(repo, dispatched),
                                    now=NOW))
    assert out["started"] == 0 and out["finished"] == 0
    assert dispatched == []
    assert repo.list_items(c["id"])[0]["status"] == "pending"
    assert repo.get_campaign(c["id"])["status"] == "running"


def test_tick_dispatch_inside_window():
    """同窗、now 落窗内 → 起拨。"""
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=5,
                             call_windows=_window_inside_now(),
                             object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    dispatched: list[str] = []
    out = asyncio.run(campaign_tick(repo, dispatcher=_ending_dispatch(repo, dispatched),
                                    now=NOW))
    assert out["started"] == 1
    assert len(dispatched) == 1
    assert repo.list_items(c["id"])[0]["status"] == "dialing"


def test_global_window_intersects_task_window():
    """全局窗（settings.campaign.call_windows）∩ 任务窗，两层都过才起拨。"""
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=5,
                             call_windows=_window_inside_now(),
                             object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    # 全局窗=now 窗外 → 任务窗虽全开也不起拨。（save_settings 白名单不收 campaign
    # 段——全局窗写侧端点不在本计划内——测试直接改内存仓 settings dict。）
    repo.settings["campaign"] = {"call_windows": _window_outside_now()}
    dispatched: list[str] = []
    out = asyncio.run(campaign_tick(repo, dispatcher=_ending_dispatch(repo, dispatched),
                                    now=NOW))
    assert out["started"] == 0
    assert repo.list_items(c["id"])[0]["status"] == "pending"
    # 全局窗空 → 只看任务窗（空=不限）
    repo.settings["campaign"] = {"call_windows": []}
    out2 = asyncio.run(campaign_tick(repo, dispatcher=_ending_dispatch(repo, dispatched),
                                     now=NOW))
    assert out2["started"] == 1
    assert repo.list_items(c["id"])[0]["status"] == "dialing"


def test_concurrency_two_slots():
    """max_concurrency=2：一轮至多补一通、下一轮巡检再补位到 2 槽；在途满员第 3 条不起；
    释放一槽后下一轮补位。"""
    repo = InMemoryBusinessRepository()
    objs = [repo.create_object("acc-001", {"display_name": f"A{i}",
                                           "phone": f"+8521111111{i}"})
            for i in range(3)]
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=0, max_concurrency=2,
                             object_ids=[o["id"] for o in objs])
    repo.update_campaign(c["id"], status="running")
    dispatched: list[str] = []
    dispatch = _ending_dispatch(repo, dispatched)

    # 第一轮：在途 0 < cap 2 → 起 1 通（fake dispatcher 把通话置 ENDED，item=dialing）
    out1 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    assert out1["started"] == 1
    items = repo.list_items(c["id"])
    assert items[0]["status"] == "dialing" and items[1]["status"] == "pending"

    # 手工把已拨 item 置 in_call（通话同步回 active，否则下一轮会被收割）
    repo.update_item(items[0]["id"], status="in_call")
    repo.update_call(items[0]["call_id"], status="active")

    # 第二轮：在途 1 < 2 → 再补 1 通 → 在途=2
    out2 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    assert out2["started"] == 1
    items = repo.list_items(c["id"])
    assert sum(1 for i in items if i["status"] in ("dialing", "in_call")) == 2

    repo.update_item(items[1]["id"], status="in_call")
    repo.update_call(items[1]["call_id"], status="active")

    # 第三轮：在途 2 >= cap 2 → 第 3 条不起
    out3 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    assert out3["started"] == 0
    assert repo.list_items(c["id"])[2]["status"] == "pending"

    # 释放一槽：第一通挂断 → 同轮收割；gap 冷却挡同轮补位（gap_seconds=0 兜 5s）
    repo.update_call(items[0]["call_id"], status="ended", disposition="completed")
    out4 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    assert out4["harvested"] == 1 and out4["started"] == 0
    past = (datetime.now(timezone.utc) - timedelta(seconds=31)).replace(tzinfo=None).isoformat()
    repo.update_item(items[0]["id"], updated_at=past)
    # 冷却过后 → 补位起第 3 条
    out5 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    assert out5["started"] == 1
    assert repo.list_items(c["id"])[2]["status"] == "dialing"


def test_redispatch_cycle():
    """未接通自动重拨全周期：收割回 pending（attempts 预约=2）→ 间隔内不起拨 →
    到点重拨（attempts=2）→ 再 no_answer（attempts=max）终态不回 pending → done。"""
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=0,
                             redispatch={"max_attempts": 2, "interval_minutes": 30,
                                         "on": ["no_answer"]},
                             object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    dispatched: list[str] = []
    dispatch = _ending_dispatch(repo, dispatched)

    # 首拨（attempts=1）
    out1 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    assert out1["started"] == 1
    item = repo.list_items(c["id"])[0]
    # 未接通收割 → 回 pending 等重拨；attempts 不重置、预约下一次尝试编号=2
    out2 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    item = repo.get_item(item["id"])
    assert out2["harvested"] == 1
    assert item["status"] == "pending" and item["attempts"] == 2

    # 30 分钟内：等待重拨，不起拨、不判 done
    repo.update_item(item["id"], updated_at=(NOW - timedelta(minutes=10)).isoformat())
    out3 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    assert out3["started"] == 0 and out3["finished"] == 0
    assert repo.get_campaign(c["id"])["status"] == "running"

    # 把 updated_at 拨早 31 分钟 → 到点重拨；attempts 保持 2（本轮起的就是第 2 次）
    repo.update_item(item["id"], updated_at=(NOW - timedelta(minutes=31)).isoformat())
    out4 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    assert out4["started"] == 1
    item = repo.get_item(item["id"])
    assert item["status"] == "dialing" and item["attempts"] == 2

    # 再次 no_answer：attempts 已=2=max → 终态不回 pending；名单尽 → done
    out5 = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    item = repo.get_item(item["id"])
    assert out5["harvested"] == 1
    assert item["status"] == "no_answer" and item["attempts"] == 2
    assert repo.get_campaign(c["id"])["status"] == "done"


def test_waiting_redispatch_keeps_campaign_open():
    """名单全在等重拨期间：campaign 不置 done（等钟到点继续拨）。"""
    repo = InMemoryBusinessRepository()
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85211111111"})
    c = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                             language="zh", gap_seconds=0,
                             redispatch={"max_attempts": 2, "interval_minutes": 30,
                                         "on": ["no_answer"]},
                             object_ids=[obj["id"]])
    repo.update_campaign(c["id"], status="running")
    dispatched: list[str] = []
    dispatch = _ending_dispatch(repo, dispatched)

    asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))          # 首拨
    asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))          # 收割回 pending
    item = repo.list_items(c["id"])[0]
    assert item["status"] == "pending" and item["attempts"] == 2
    # 等待重拨的 tick：finished==0、campaign 保持 running
    out = asyncio.run(campaign_tick(repo, dispatcher=dispatch, now=NOW))
    assert out["finished"] == 0
    assert repo.get_campaign(c["id"])["status"] == "running"
    assert repo.list_items(c["id"])[0]["status"] == "pending"


