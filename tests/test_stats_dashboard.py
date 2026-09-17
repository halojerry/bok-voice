"""Task 5（2026-09-17 campaign-scheduling-dashboard）：工作台统计端点 + 通话时长落点。

契约（plan Task 5 / task-5-brief.md）：
- ``GET /api/stats/dashboard?account_id=`` → concurrency/calls/duration_buckets/
  agents/tags 五段聚合；
- 口径：current=status==active 计数；answered=ENDED/FAILED 且 disposition 不在
  {no_answer,rejected,failed}；answer_rate=answered/ENDED 总数（ENDED=0→0.0）；
  today=created_at 落**本地**自然日；duration_buckets 只统计 duration_s>0 的
  通话，桶界 [0,15)/[15,30)/[30,60)/[60,90)/[90+,∞)（90+ 含 90）；agents 按
  created_by 分组（空串=战役单剔除），join users 显示名，按 calls 降序截 8；
- 时长落点：answered→ACTIVE 落 started_at；终态（ENDED/FAILED）落 ended_at，
  duration_s=ended-started（started_at 空→0），全部 UTC naive datetime
  （T1-M2 铁律：时间列传 datetime 对象，不传 ISO 串）。

造数口径：内存仓 fixture（``DATABASE_URL=""`` 强制，与 test_campaign_api.py
同款）；created_at 经 ``update_call`` 注入**本地**墙钟 ISO 串——端点的 today
判定拿存储串与本地日期前缀做 startswith，注入本地时域串即「本地自然日」语义
最直接的 fixture 形态（内存仓 dict 直通写入；SQL 仓走 ORM 才能改 created_at，
本测不覆盖 SQL 腿，与既有 session_reports/campaign_api 测试同界）。
"""
from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

import pytest

from bok_voice_business_db.repository import InMemoryBusinessRepository

_SEQ = iter(range(1, 10_000))


def datetime_local_now():
    """本地墙钟 naive datetime（today 判定同域，见模块 docstring 造数口径）。"""
    import datetime as dt

    return dt.datetime.now().astimezone().replace(tzinfo=None)


def _seed_call(
    repo: InMemoryBusinessRepository,
    *,
    status: str,
    disposition: str = "",
    duration_s: int = 0,
    created_days_ago: int = 0,
    created_by: str = "",
    whatsapp_status: str = "",
) -> str:
    """repo.create_call + update_call 造一通带统计字段的通话，返回 call_id。

    created_at 注入本地墙钟（now - created_days_ago）ISO 串：端点 today 判定
    是「存储串 startswith 本地今日前缀」，以本地 now 为基准保证任意时区/任意
    时刻跑测试都稳定（UTC 深夜跑时 UTC now 会落在「本地明天/昨天」）。
    """
    from bok_voice_core.types import CallMode, SessionManifest

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
    created_local = (datetime_local_now() - timedelta(days=created_days_ago)).isoformat()
    fields: dict = {
        "status": status,
        "disposition": disposition,
        "duration_s": duration_s,
        "created_by": created_by,
        "created_at": created_local,
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
    _seed_call(repo, status="active", created_days_ago=0)                      # 并发 1
    _seed_call(repo, status="ended", disposition="completed", duration_s=45,
               created_days_ago=0, created_by="u-1")                           # 今日接通 30-60 桶
    _seed_call(repo, status="ended", disposition="no_answer", duration_s=0,
               created_days_ago=0, created_by="u-1")                           # 未接通
    _seed_call(repo, status="ended", disposition="completed", duration_s=120,
               created_days_ago=2, created_by="u-2")                           # 昨天不计今日、90+ 桶
    _seed_call(repo, status="ended", disposition="", whatsapp_status="captured",
               duration_s=20, created_days_ago=0, created_by="u-1")
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    assert data["concurrency"] == {"current": 1}
    assert data["calls"]["today"] == 4 and data["calls"]["total"] == 5
    assert data["calls"]["answered"] == 3
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
    assert data["calls"] == {"today": 0, "total": 0, "answered": 0, "answer_rate": 0.0}
    assert set(data["duration_buckets"]) == {"0-15", "15-30", "30-60", "60-90", "90+"}
    assert data["agents"] == []
    assert data["tags"] == {"disposition": {}, "whatsapp": {}}


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
               created_days_ago=0, created_by=u1["id"])
    _seed_call(repo, status="ended", disposition="no_answer", duration_s=0,
               created_days_ago=0, created_by="nobody")
    data = client_with_repo.client.get("/api/stats/dashboard").json()
    agents = {a["user_id"]: a for a in data["agents"]}
    assert agents[u1["id"]]["name"] == "阿明"
    # 无对应用户行 → 名字兜底 uid 本身。
    assert agents["nobody"]["name"] == "nobody"


# ---- 终态落点（ended_at/duration_s/started_at） ----


def test_dial_result_answered_stamps_started_at(client_with_repo):
    """dial-result answered→ACTIVE 落 started_at（datetime 形态）。"""
    repo = client_with_repo.repo
    cid = _seed_call(repo, status="ringing", created_days_ago=0)
    r = client_with_repo.client.post(f"/api/calls/{cid}/dial-result", json={"status": "answered"})
    assert r.status_code == 200
    call = repo.get_call(cid)
    assert call["status"] == "active"
    assert call["started_at"] is not None


def test_dial_result_failure_stamps_ended_and_zero_duration(client_with_repo):
    """dial-result 失败态→ENDED：ended_at 落点、started_at 空 → duration_s=0。"""
    repo = client_with_repo.repo
    cid = _seed_call(repo, status="ringing", created_days_ago=0)
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
    cid = _seed_call(repo, status="active", created_days_ago=0)
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
    cid = _seed_call(repo, status="active", created_days_ago=0)
    repo.update_call(cid, started_at=_utcnow_naive() - timedelta(seconds=10))
    r = client_with_repo.client.post(f"/api/supervisor/{cid}/end?disposition=declined")
    assert r.status_code == 200
    call = repo.get_call(cid)
    assert call["status"] == "ended" and call["disposition"] == "declined"
    assert call["ended_at"] is not None and call["duration_s"] >= 10
