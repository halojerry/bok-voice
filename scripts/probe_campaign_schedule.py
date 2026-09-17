"""战役调度循环集成探针（2026-09-17 campaign-scheduling-dashboard Task 3）。

真 SQL 仓（tempfile sqlite + deps.build_engine 幂等迁移）+ 真 campaign_tick 循环 +
fake dispatcher（记录派发并立刻把通话置 ENDED/no_answer），不依赖 LiveKit/HTTP。

断言组（brief Step 4 规格 + 窗外断言）：
  1. 窗内 tick 起拨第 1 通；
  2. 未接通收割回 pending（attempts 预约=2，等 3s 重拨间隔）；
  3. 等 4s 后 tick 重拨且 attempts=2；
  4. 再 no_answer 后（attempts=2=max）保持终态不回 pending；
  5. campaign 最终 done；
  6. 窗外 campaign（窗不含当前时刻）tick started==0。

每次运行新建临时库（幂等可重复跑）。退出码 0=全 PASS，1=有 FAIL。
运行：.venv312/bin/python scripts/probe_campaign_schedule.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-plane"))

from sqlalchemy.orm import Session

from bok_voice_business_db.repository import SqlAlchemyBusinessRepository
from control_plane.campaign import campaign_tick

REDISPATCH_INTERVAL_MIN = 0.05  # 3 秒（brief 规格：重拨间隔用分钟小数配合实钟等待）
GAP_SLEEP_S = 5.5  # > gap 冷却 5s（gap_seconds=0 兜底档）+ 重拨间隔 3s 的余量


def _window_covering_now() -> list[dict]:
    """覆盖当前本地时刻的任务窗（±2min 余量；跨零点时两侧起点日都进 days）。"""
    now = datetime.now().astimezone()
    start_dt = now - timedelta(minutes=2)
    end_dt = now + timedelta(minutes=2)
    start, end = start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M")
    days = {now.isoweekday()}
    if start_dt.date() != now.date():  # start 跨零点：昨日起点日也要在 days
        days.add((now.isoweekday() - 2) % 7 + 1)
    return [{"days": sorted(days), "start": start, "end": end}]


def _window_excluding_now() -> list[dict]:
    """绝不含当前时刻的窗：days=明天（今天永不命中，跨零点分支也够不着）。"""
    tomorrow = (datetime.now().astimezone().isoweekday() % 7) + 1
    return [{"days": [tomorrow], "start": "00:00", "end": "23:59"}]


def _make_sql_repo() -> tuple[SqlAlchemyBusinessRepository, str]:
    """tempfile sqlite + DATABASE_URL → build_engine（create_all + 幂等补列）→ SQL 仓。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False, prefix="bok-probe-camp-")
    tmp.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"
    from control_plane.deps import build_engine

    engine = build_engine()
    if engine is None:
        raise RuntimeError("build_engine() 返回 None（DATABASE_URL 未生效）")
    return SqlAlchemyBusinessRepository(Session(engine)), tmp.name


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, bool(cond), detail))
        mark = "PASS" if cond else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    repo, db_path = _make_sql_repo()
    try:
        objs = [repo.create_object("acc-001", {"display_name": f"探针对象{i}",
                                               "phone": f"+8526432000{i}"})
                for i in range(3)]
        camp = repo.create_campaign(
            "acc-001", name="探针-窗内", template_id="", persona_id="", language="zh",
            gap_seconds=0, max_concurrency=2, object_ids=[o["id"] for o in objs],
            call_windows=_window_covering_now(),
            redispatch={"max_attempts": 2, "interval_minutes": REDISPATCH_INTERVAL_MIN,
                        "on": ["no_answer"]},
        )
        repo.update_campaign(camp["id"], status="running")
        dispatched: list[str] = []

        async def fake_dispatch(room: str, metadata: str) -> None:
            dispatched.append(room)  # room 即 call_id（_start_call 契约）
            repo.update_call(room, status="ended", disposition="no_answer")

        def tick() -> dict:
            return asyncio.run(campaign_tick(repo, dispatcher=fake_dispatch))

        # ① 窗内 tick 起拨第 1 通
        out1 = tick()
        item1 = repo.list_items(camp["id"])[0]
        check("窗内 tick 起拨第 1 通",
              out1["started"] == 1 and item1["status"] == "dialing"
              and len(dispatched) == 1,
              f"started={out1['started']} item0={item1['status']} dispatched={len(dispatched)}")

        # ② 未接通收割回 pending（attempts 预约=2，等 3s 重拨间隔）
        out2 = tick()
        item1 = repo.get_item(item1["id"])
        check("收割回 pending（等 3s 间隔）",
              out2["harvested"] == 1 and item1["status"] == "pending"
              and item1["attempts"] == 2,
              f"harvested={out2['harvested']} item0={item1['status']}/attempts={item1['attempts']}")

        # ③ 等 4s（>3s 间隔）后 tick 重拨且 attempts=2
        time.sleep(4.0)
        out3 = tick()
        item1 = repo.get_item(item1["id"])
        check("等 4s 后 tick 重拨且 attempts=2",
              out3["started"] == 1 and item1["status"] == "dialing"
              and item1["attempts"] == 2 and len(dispatched) >= 3,
              f"started={out3['started']} item0={item1['status']}/attempts={item1['attempts']} "
              f"dispatched={len(dispatched)}")

        # ④ 再 no_answer：attempts=2=max → 保持终态不回 pending
        out4 = tick()
        item1 = repo.get_item(item1["id"])
        check("再 no_answer 后（attempts=2=max）保持终态",
              out4["harvested"] == 1 and item1["status"] == "no_answer"
              and item1["attempts"] == 2,
              f"item0={item1['status']}/attempts={item1['attempts']}")

        # ⑤ campaign 最终 done（名单余量逐通重拨耗尽；每轮等间隔+gap 余量）
        deadline = time.time() + 90
        ticks = 0
        while time.time() < deadline and ticks < 30:
            if repo.get_campaign(camp["id"])["status"] == "done":
                break
            tick()
            ticks += 1
            time.sleep(GAP_SLEEP_S)
        terminal = [i["status"] for i in repo.list_items(camp["id"])]
        check("campaign 最终 done",
              repo.get_campaign(camp["id"])["status"] == "done"
              and all(s in ("no_answer", "done", "rejected", "failed", "skipped")
                      for s in terminal),
              f"status={repo.get_campaign(camp['id'])['status']} items={terminal} "
              f"total_dispatched={len(dispatched)} drain_ticks={ticks}")

        # ⑥ 窗外 campaign：窗不含当前时刻 → tick 不起拨、不误判 done
        obj2 = repo.create_object("acc-001", {"display_name": "探针-窗外",
                                              "phone": "+85264320999"})
        camp2 = repo.create_campaign(
            "acc-001", name="探针-窗外", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[obj2["id"]],
            call_windows=_window_excluding_now(),
        )
        repo.update_campaign(camp2["id"], status="running")
        before = len(dispatched)
        out6 = tick()  # 战役 A 已 done（非 running），本 tick 只处理战役 B
        item_b = repo.list_items(camp2["id"])[0]
        check("窗外 tick 不起拨",
              out6["started"] == 0 and len(dispatched) == before
              and item_b["status"] == "pending"
              and repo.get_campaign(camp2["id"])["status"] == "running",
              f"started={out6['started']} item={item_b['status']}")
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

    failed = [r for r in results if not r[1]]
    print(f"\n===== 汇总：{len(results) - len(failed)}/{len(results)} PASS =====")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
