"""/api/calls 分页契约（2026-10-02 UX 根因修复）。

旧形状：GET /api/calls 全量返回 + web 端排序再切 50——payload 随通话历史单调涨
（生产库 1570 行时 /calls 页 4s 轮询 = 每分钟 15 次全量回传）。

新契约：``limit>0`` = 服务端 created_at 倒序 + 截断；``limit`` 缺省 0 = 旧档全量
零漂移（dispatch/monitor/reports 等既有消费点不传 limit 不受影响）。

零端口零服务：内存仓 + TestClient（test_call_logs_endpoint 同款夹具）。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "")  # 强制内存仓
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from control_plane import main as cp_main  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    yield


def _mk_call(repo, call_id: str, account_id: str = "acc-001", created_at: str = "") -> dict:
    from bok_voice_core.policies import select_session_manifest
    from bok_voice_core.types import CallMode

    row = repo.create_call(select_session_manifest(
        session_id=call_id, account_id=account_id, object_id="",
        persona_id="", mode=CallMode.SIMULATION,
    ))
    if created_at:
        repo.update_call(call_id, created_at=created_at)
        row = repo.get_call(call_id) or row
    return row


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    return TestClient(cp_main.app), repo


def test_limit_orders_desc_and_truncates(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _mk_call(repo, "call-old", created_at="2026-10-01T10:00:00+00:00")
    _mk_call(repo, "call-newest", created_at="2026-10-03T10:00:00+00:00")
    _mk_call(repo, "call-mid", created_at="2026-10-02T10:00:00+00:00")

    res = client.get("/api/calls", params={"limit": 2})
    assert res.status_code == 200
    rows = res.json()
    assert [r["id"] for r in rows] == ["call-newest", "call-mid"]  # 倒序+截断


def test_limit_zero_keeps_legacy_full_shape(monkeypatch):
    """limit 缺省/0 = 旧档：全量返回、不做排序承诺（web 旧消费点零漂移）。"""
    client, repo = _client_and_repo(monkeypatch)
    _mk_call(repo, "call-a")
    _mk_call(repo, "call-b")
    _mk_call(repo, "call-c")

    res = client.get("/api/calls")
    assert res.status_code == 200
    assert len(res.json()) == 3
    res0 = client.get("/api/calls", params={"limit": 0})
    assert len(res0.json()) == 3


def test_limit_respects_status_and_account_filters(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    _mk_call(repo, "call-live", created_at="2026-10-03T10:00:00+00:00")
    _mk_call(repo, "call-done", created_at="2026-10-04T10:00:00+00:00")
    repo.update_call("call-done", status="ended")
    _mk_call(repo, "call-other-acct", account_id="acc-002", created_at="2026-10-05T10:00:00+00:00")

    res = client.get("/api/calls", params={"limit": 10, "status": "ended"})
    assert [r["id"] for r in res.json()] == ["call-done"]

    res2 = client.get("/api/calls", params={"account_id": "acc-002", "limit": 10})
    assert [r["id"] for r in res2.json()] == ["call-other-acct"]


def test_turn_count_attached_under_limit(monkeypatch):
    """分页行照常带 turn_count/avg_latency_ms（聚合值跟随行本身，不因截断丢失）。"""
    from bok_voice_core.types import TurnEvent

    client, repo = _client_and_repo(monkeypatch)
    _mk_call(repo, "call-with-turns", created_at="2026-10-02T10:00:00+00:00")
    _mk_call(repo, "call-newer-bare", created_at="2026-10-03T10:00:00+00:00")
    for i in range(3):
        repo.create_turn(TurnEvent(
            trace_id=f"tr-{i}", call_id="call-with-turns", turn_id=f"t{i}", role="user",
            transcript=f"hello {i}", latency_ms=100,
        ))

    res = client.get("/api/calls", params={"limit": 2})
    rows = {r["id"]: r for r in res.json()}
    assert rows["call-with-turns"]["turn_count"] == 3
    assert rows["call-newer-bare"]["turn_count"] == 0


def test_repo_turn_stats_scoping_inmemory():
    """内存仓 call_ids 收窄：None=全量（旧档），给定=只聚合该集合。"""
    from bok_voice_core.types import TurnEvent

    repo = InMemoryBusinessRepository()
    for cid in ("call-a", "call-b"):
        repo.create_turn(TurnEvent(
            trace_id="tr-0", call_id=cid, turn_id="t0", role="user", transcript="x", latency_ms=50,
        ))
    assert set(repo.turn_stats()) == {"call-a", "call-b"}
    assert set(repo.turn_stats(["call-a"])) == {"call-a"}
    assert repo.turn_stats(["call-a"])["call-a"]["turns"] == 1
    assert repo.turn_stats([]) == {}
    assert repo.turn_stats(["call-不存在"]) == {}


def _mk_call_sql(repo, call_id: str, account_id: str, created_at: str) -> None:
    from datetime import datetime

    from bok_voice_core.policies import select_session_manifest
    from bok_voice_core.types import CallMode

    repo.create_call(select_session_manifest(
        session_id=call_id, account_id=account_id, object_id="",
        persona_id="", mode=CallMode.SIMULATION,
    ))
    # SQL 仓 DateTime 列只吃 datetime 对象（内存仓才收 ISO 串）。
    repo.update_call(call_id, created_at=datetime.fromisoformat(created_at))


def test_turn_stats_for_calls_sqlite_join_scoping():
    """SQL 仓当页聚合（2026-10-02 收窄·列对列 JOIN 子查询实现）：与 list_calls
    同口径——account 过滤 + created_at 倒序 + limit 截断，只聚合当页通话的 turns。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from bok_voice_business_db import models
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository
    from bok_voice_core.types import TurnEvent

    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine)())
    try:
        for cid, ts in (("call-old", "2026-10-01T10:00:00+00:00"),
                        ("call-newest", "2026-10-03T10:00:00+00:00"),
                        ("call-other-acct", "2026-10-04T10:00:00+00:00")):
            acct = "acc-002" if cid == "call-other-acct" else "acc-001"
            _mk_call_sql(repo, cid, acct, ts)
        for cid in ("call-old", "call-newest", "call-other-acct"):
            for i in range(2):
                repo.create_turn(TurnEvent(
                    trace_id=f"tr-{cid}-{i}", call_id=cid, turn_id=f"t{i}",
                    role="user", transcript="x", latency_ms=100,
                ))

        # 全量（旧档）：acc-001 两通都在
        full = repo.turn_stats_for_calls("acc-001", "", 0)
        assert set(full) == {"call-old", "call-newest"}
        # limit=1（最新一页）：只剩 call-newest，turns=2
        page = repo.turn_stats_for_calls("acc-001", "", 1)
        assert set(page) == {"call-newest"}
        assert page["call-newest"]["turns"] == 2
        # 跨账号隔离：acc-002 只有自己的
        other = repo.turn_stats_for_calls("acc-002", "", 0)
        assert set(other) == {"call-other-acct"}
    finally:
        repo.close()
