"""Task 5（2026-09-17 campaign-scheduling-dashboard）：工作台统计端点 + 通话时长落点。

契约（plan Task 5 / task-5-brief.md，修复波 T5-M1/M2/M3 修订）：
- ``GET /api/stats/dashboard?account_id=`` → concurrency/calls/duration_buckets/
  agents/tags 五段聚合；
- 口径：current=status==active 计数；answered=**ENDED** 且 disposition 不在
  {no_answer,rejected,failed}（FAILED+abandoned=reaper 振铃超时从未接通，不算
  接通）；answer_rate 分母=ENDED+FAILED 全体（拨出有结果）；today/answered_today
  =created_at ≥ **本地午夜对应的 UTC 边界**（``_local_midnight_utc_boundary``，
  测试同源现算）；duration_buckets 只统计 duration_s>0 的通话，桶界
  [0,15)/[15,30)/[30,60)/[60,90)/[90+,∞)（90+ 含 90）；agents 按 created_by
  分组（空串=战役单剔除），join users 显示名，按 calls 降序截 8；
- 时长落点：answered→ACTIVE 落 started_at（**coalesce**：重复上报不重置起点）；
  终态（ENDED/FAILED）落 ended_at，duration_s=ended-started（started_at 空→0），
  全部 UTC naive datetime（T1-M2 铁律：时间列传 datetime 对象，不传 ISO 串）。

造数口径：内存仓 fixture（``DATABASE_URL=""`` 强制，与 test_campaign_api.py
同款）；created_at 注入 **UTC 边界 ±1min 的确定性锚点** ISO 串——端点 today
判定拿 ``_parse_updated_at`` 归一后的 naive UTC 与边界比较，锚点直接以同一公式
现算的边界为基准，任意时区/任意时刻（含本地午夜附近）跑测试都稳定。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

import pytest

from bok_voice_business_db.repository import InMemoryBusinessRepository

_SEQ = iter(range(1, 10_000))


def _today_boundary() -> datetime:
    """与端点同源的今日 UTC 边界（`_local_midnight_utc_boundary` 现算）。"""
    from control_plane.main import _local_midnight_utc_boundary

    return _local_midnight_utc_boundary()


def _seed_call(
    repo: InMemoryBusinessRepository,
    *,
    status: str,
    disposition: str = "",
    duration_s: int = 0,
    created_at_utc: datetime | None = None,
    created_by: str = "",
    whatsapp_status: str = "",
) -> str:
    """repo.create_call + update_call 造一通带统计字段的通话，返回 call_id。

    created_at 注入 UTC 锚点 ISO 串（缺省=当前 UTC 墙钟，恒在今日边界之后）：
    端点 today 判定是「归一化 created_at ≥ 本地午夜 UTC 边界」，以同源边界 ±1min
    构造跨日界两侧锚点，任意时区跑都确定（T5-M3 测试锚点改造）。
    """
    from bok_voice_core.types import CallMode, SessionManifest
    from control_plane.campaign import _utcnow_naive

    cid = f"call-stats-{next(_SEQ)}"
    repo.create_call(
        SessionManifest(
            session_id=cid,
            account_id="acc-001",
            object_id="obj-1",
            persona_id="",
            mode=CallMode.LIVE,
            direction="outbound",
            language="zh",
            providers={},
        )
    )
    created = (created_at_utc or _utcnow_naive()).isoformat()
    fields: dict = {
        "status": status,
        "disposition": disposition,
        "duration_s": duration_s,
        "created_by": created_by,
        "created_at": created,
    }
    if whatsapp_status:
        fields["whatsapp_status"] = whatsapp_status
    repo.update_call(cid, **fields)
    return cid


@pytest.fixture()
def client_with_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app)
    return SimpleNamespace(client=client, repo=repo)


def test_dashboard_aggregates(client_with_repo):
    repo = client_with_repo.repo
    boundary = _today_boundary()
    _seed_call(repo, status="active", created_at_utc=boundary + timedelta(minutes=1))   # 并发 1
    _seed_call(repo, status="ended", disposition="completed", duration_s=45,
               created_at_utc=boundary + timedelta(minutes=1), created_by="u-1")        # 今日接通 30-60 桶
    _seed_call(repo, status="ended", disposition="no_answer", duration_s=0,
               created_at_utc=boundary + timedelta(minutes=1), created_by="u-1")        # 未接通
    _seed_call(repo, status="ended", disposition="completed", duration_s=120,
               created_at_utc=boundary - timedelta(minutes=1), created_by="u-2")        # 边界前不计今日、90+ 桶
    _seed_call(repo, status="ended", disposition="", whatsapp_status="captured",
               duration_s=20, created_at_utc=boundary + timedelta(minutes=1),
               created_by="u-1")
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    assert data["concurrency"] == {"current": 1}
    assert data["calls"]["today"] == 4 and data["calls"]["total"] == 5
    assert data["calls"]["answered"] == 3
    assert data["calls"]["answered_today"] == 2  # 三通接通中两通在边界后（u-2 那通在边界前）
    assert data["duration_buckets"]["30-60"] == 1 and data["duration_buckets"]["90+"] == 1
    assert data["duration_buckets"]["15-30"] == 1
    assert [a["user_id"] for a in data["agents"]] == ["u-1", "u-2"]
    assert data["agents"][0]["calls"] == 3 and data["agents"][0]["answered"] == 2
    assert data["tags"]["whatsapp"]["captured"] == 1
    assert data["tags"]["disposition"]["no_answer"] == 1
    # 口径补充断言：answer_rate=answered/ENDED=3/4；零时长通话不进 0-15 桶。
    assert data["calls"]["answer_rate"] == 0.75
    assert data["duration_buckets"]["0-15"] == 0 and data["duration_buckets"]["60-90"] == 0


def test_dashboard_empty_account_zero_division_safe(client_with_repo):
    """ENDED=0 → answer_rate=0.0（不 ZeroDivisionError）；各段形状齐整。"""
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    assert data["concurrency"] == {"current": 0}
    assert data["calls"] == {"today": 0, "total": 0, "answered": 0, "answered_today": 0,
                             "answer_rate": 0.0}
    assert set(data["duration_buckets"]) == {"0-15", "15-30", "30-60", "60-90", "90+"}
    assert data["agents"] == []
    assert data["tags"] == {"disposition": {}, "whatsapp": {}}


# ---- 修复波 T5-M2/M3（2026-09-17）：answered 排除 FAILED + today UTC 边界 ----


def test_dashboard_answered_excludes_failed_abandoned(client_with_repo):
    """T5-M2：reaper 振铃超时（FAILED+abandoned，从未接通）不计 answered，
    但计入 answer_rate 分母（拨出有结果）。"""
    repo = client_with_repo.repo
    boundary = _today_boundary()
    _seed_call(repo, status="ended", disposition="completed", duration_s=30,
               created_at_utc=boundary + timedelta(minutes=1))
    _seed_call(repo, status="failed", disposition="abandoned",
               created_at_utc=boundary + timedelta(minutes=1))
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    assert data["calls"]["answered"] == 1
    assert data["calls"]["answered_today"] == 1
    assert data["calls"]["answer_rate"] == round(1 / 2, 4)  # 分母=ENDED+FAILED 全体
    # 标记统计照记 abandoned（reaper 结果可见），不因 answered 口径丢失。
    assert data["tags"]["disposition"]["abandoned"] == 1


def test_dashboard_today_boundary_utc_cross(client_with_repo):
    """T5-M3：today/answered_today 判定=本地午夜 UTC 边界（同源公式现算）。
    边界前 1 分钟不计、边界后 1 分钟计入；全时段 answered/total 不受边界影响。"""
    repo = client_with_repo.repo
    boundary = _today_boundary()
    _seed_call(repo, status="ended", disposition="completed", duration_s=20,
               created_at_utc=boundary - timedelta(minutes=1))
    _seed_call(repo, status="ended", disposition="completed", duration_s=20,
               created_at_utc=boundary + timedelta(minutes=1))
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    assert data["calls"]["today"] == 1
    assert data["calls"]["answered_today"] == 1
    assert data["calls"]["answered"] == 2
    assert data["calls"]["total"] == 2


def test_dashboard_agent_name_joins_users(client_with_repo):
    """agents.name join users 显示名（display_name 优先，缺省回 username）。

    join 键=users.id（created_by 落的是建单人 user id，非 username）。
    """
    repo = client_with_repo.repo
    u1 = repo.create_user(
        username="ming", password_hash="x", display_name="阿明",
        role="user", org_id="org-t", account_id="acc-001",
    )
    _seed_call(repo, status="ended", disposition="completed", duration_s=45,
               created_by=u1["id"])
    _seed_call(repo, status="ended", disposition="no_answer", duration_s=0,
               created_by="nobody")
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    agents = {a["user_id"]: a for a in data["agents"]}
    assert agents[u1["id"]]["name"] == "阿明"
    # 无对应用户行 → 名字兜底 uid 本身。
    assert agents["nobody"]["name"] == "nobody"


# ---- 终态落点（ended_at/duration_s/started_at） ----


def test_dial_result_answered_stamps_started_at(client_with_repo):
    """dial-result answered→ACTIVE 落 started_at（datetime 形态）。"""
    repo = client_with_repo.repo
    cid = _seed_call(repo, status="ringing")
    r = client_with_repo.client.post(f"/api/calls/{cid}/dial-result", json={"status": "answered"})
    assert r.status_code == 200
    call = repo.get_call(cid)
    assert call["status"] == "active"
    assert call["started_at"] is not None


def test_dial_result_answered_repeat_coalesces_started_at(client_with_repo):
    """T5-M1：重复 answered 上报 coalesce started_at——已有起点不重置
    （防重复上报把 duration 口径起点推后，与 resume-agent 路径同款）。"""
    from control_plane.campaign import _utcnow_naive

    repo = client_with_repo.repo
    cid = _seed_call(repo, status="ringing")
    first = _utcnow_naive() - timedelta(seconds=30)
    repo.update_call(cid, started_at=first)
    r = client_with_repo.client.post(f"/api/calls/{cid}/dial-result", json={"status": "answered"})
    assert r.status_code == 200
    assert repo.get_call(cid)["started_at"] == first


def test_dial_result_failure_stamps_ended_and_zero_duration(client_with_repo):
    """dial-result 失败态→ENDED：ended_at 落点、started_at 空 → duration_s=0。"""
    repo = client_with_repo.repo
    cid = _seed_call(repo, status="ringing")
    r = client_with_repo.client.post(f"/api/calls/{cid}/dial-result", json={"status": "no_answer"})
    assert r.status_code == 200
    call = repo.get_call(cid)
    assert call["status"] == "ended"
    assert call["ended_at"] is not None
    assert call["started_at"] is None and call["duration_s"] == 0


def test_hangup_computes_duration_from_started_at(client_with_repo):
    """挂断→ENDED：duration_s=ended-started（≥预置的 30s 间隔）。"""
    from control_plane.campaign import _utcnow_naive

    repo = client_with_repo.repo
    cid = _seed_call(repo, status="active")
    repo.update_call(cid, started_at=_utcnow_naive() - timedelta(seconds=30))
    r = client_with_repo.client.post(f"/api/calls/{cid}/hangup")
    assert r.status_code == 200
    call = repo.get_call(cid)
    assert call["status"] == "ended"
    assert call["ended_at"] is not None
    assert call["duration_s"] >= 30


def test_supervisor_end_stamps_ended_fields(client_with_repo):
    """主管收线→ENDED：ended_at/duration_s 落点（disposition 透传不破坏）。"""
    from control_plane.campaign import _utcnow_naive

    repo = client_with_repo.repo
    cid = _seed_call(repo, status="active")
    repo.update_call(cid, started_at=_utcnow_naive() - timedelta(seconds=10))
    r = client_with_repo.client.post(f"/api/supervisor/{cid}/end?disposition=declined")
    assert r.status_code == 200
    call = repo.get_call(cid)
    assert call["status"] == "ended" and call["disposition"] == "declined"
    assert call["ended_at"] is not None and call["duration_s"] >= 10


def _seed_running_campaign(repo, *, object_count: int, redispatch: dict | None = None) -> str:
    """待办事项测试造数：running 战役 + N 个对象（每对象一条 pending item）。"""
    obj_ids = [
        repo.create_object("acc-001", {"display_name": f"T{i}", "phone": f"+8520000000{i}"})["id"]
        for i in range(object_count)
    ]
    camp = repo.create_campaign("acc-001", name="t", template_id="", persona_id="",
                                language="zh", gap_seconds=0, object_ids=obj_ids)
    updates: dict = {"status": "running"}
    if redispatch is not None:
        # repo 层白名单吃存储键 redispatch_json（dict→JSON 是 API 层
        # _clean_redispatch 的职责）；直接传 redispatch= 会被白名单静默忽略。
        updates["redispatch_json"] = json.dumps(redispatch)
    repo.update_campaign(camp["id"], **updates)
    return str(camp["id"])


def test_dashboard_todo_four_buckets(client_with_repo):
    """待办事项四桶（2026-09-18 补卡）：首拨待外呼/等重拨/重拨到期/重拨耗尽。

    updated_at 相对实钟造（5min=间隔未走完→waiting；2h=走完→due），分钟级
    差相对 30min 间隔余量充足，任意时区/任意时刻跑都稳定。
    """
    repo = client_with_repo.repo
    camp_id = _seed_running_campaign(
        repo, object_count=4,
        redispatch={"max_attempts": 2, "interval_minutes": 30, "on": ["no_answer"]},
    )
    items = repo.list_items(camp_id)
    repo.update_item(items[1]["id"], attempts=2,
                     updated_at=(datetime.utcnow() - timedelta(minutes=5)).isoformat())
    repo.update_item(items[2]["id"], attempts=2,
                     updated_at=(datetime.utcnow() - timedelta(hours=2)).isoformat())
    repo.update_item(items[3]["id"], status="no_answer", attempts=2,
                     updated_at=(datetime.utcnow() - timedelta(hours=3)).isoformat())
    # items[0] 不动 = 首拨待外呼（attempts=1 pending）。

    r = client_with_repo.client.get("/api/stats/dashboard")
    assert r.status_code == 200
    assert r.json()["todo"] == {"to_call": 1, "waiting_redispatch": 1,
                                "due_redispatch": 1, "exhausted": 1}


def test_dashboard_todo_ignores_draft_and_policyless_exhaustion(client_with_repo):
    """draft 战役不进待办；未配重拨策略时终态失败不判耗尽（无「耗尽」语义）。"""
    repo = client_with_repo.repo
    obj = repo.create_object("acc-001", {"display_name": "D", "phone": "+85200000009"})
    camp = repo.create_campaign("acc-001", name="draft", template_id="", persona_id="",
                                language="zh", gap_seconds=0, object_ids=[obj["id"]])
    item = repo.list_items(str(camp["id"]))[0]
    repo.update_item(item["id"], status="failed", attempts=2)

    r = client_with_repo.client.get("/api/stats/dashboard")
    assert r.status_code == 200
    assert r.json()["todo"] == {"to_call": 0, "waiting_redispatch": 0,
                                "due_redispatch": 0, "exhausted": 0}


def test_dashboard_todo_policyless_pending_counts_due(client_with_repo):
    """未配重拨策略的 attempts>=2 pending：下一轮 tick 就会被拨 → 落 due 桶。"""
    repo = client_with_repo.repo
    camp_id = _seed_running_campaign(repo, object_count=1, redispatch=None)
    item = repo.list_items(camp_id)[0]
    repo.update_item(item["id"], attempts=2,
                     updated_at=(datetime.utcnow() - timedelta(minutes=1)).isoformat())
    # 防空洞：attempts 必须真被 update_item 落库，否则 due 判定是真空绿。
    assert repo.list_items(camp_id)[0]["attempts"] == 2

    r = client_with_repo.client.get("/api/stats/dashboard")
    assert r.status_code == 200
    assert r.json()["todo"] == {"to_call": 0, "waiting_redispatch": 0,
                                "due_redispatch": 1, "exhausted": 0}
