from __future__ import annotations

"""campaign tick 连接池泄漏回归（2026-09-18 生产实证）。

根因：SQL 仓读路径（list_campaigns/list_items/get_call…）从不 commit，
autobegin 事务把一条池连接占住到 Session 关闭为止；campaign tick 5s 一轮
每轮 `_repo()` 新建 Session 却从不 close，只靠 GC 归还——DB 抖动（生产
psycopg SSL EOF / server closed connection）时钉住的死连接在 GC 间隔内持续
计入 QueuePool 容量（5+10），直至每轮 30s 池超时 `campaign_tick_failed`，
且巡检的同步 DB 调用跑在事件循环上，30s 阻塞连带 /health 超时。

修复契约（三断言面）：
1. `SqlAlchemyBusinessRepository.close()` / 上下文管理器 → 连接确定性归还；
2. `campaign_tick` 自建 repo 在 finally 收口（正常轮 + list_campaigns 抛错轮）；
3. 注入 repo 不被收口（所有权归调用方，既有单测跨轮复用注入 repo 的行为不变）。
"""

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo default for main import
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

import asyncio
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control-plane"))

from bok_voice_business_db import models
from bok_voice_business_db.repository import SqlAlchemyBusinessRepository
from control_plane.campaign import campaign_tick


def _queue_engine(tmp_path, *, size: int = 2, overflow: int = 1):
    """prod Postgres 同款 QueuePool 池形（容量刻意压小让泄漏显形）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path}/pool.db",
        poolclass=QueuePool,
        pool_size=size,
        max_overflow=overflow,
        pool_timeout=0.5,
        connect_args={"check_same_thread": False},
    )
    models.create_all(engine)
    return engine


def _repo(engine) -> SqlAlchemyBusinessRepository:
    return SqlAlchemyBusinessRepository(
        sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    )


def _seed_running_campaign(repo) -> str:
    obj = repo.create_object("acc-001", {"display_name": "陈生", "phone": "+85264320111"})
    camp = repo.create_campaign(
        "acc-001", name="催件第一波", template_id="", persona_id="",
        language="zh", gap_seconds=5, object_ids=[obj["id"]],
    )
    repo.update_campaign(camp["id"], status="running")
    return camp["id"]


def _recording_dispatch(record: list):
    async def dispatch(room: str, metadata: str) -> None:
        record.append(room)

    return dispatch


class _SpyRepo:
    """包真 SQL 仓：委托全部方法，只记录 close() 是否被 campaign_tick 收口。"""

    def __init__(self, inner):
        self._inner = inner
        self.close_calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        self.close_calls += 1
        self._inner.close()


class _BoomRepo:
    """list_campaigns 即抛（模拟 DB 抖动轮），记录 close 是否仍收口。"""

    close_calls = 0

    def list_campaigns(self, account_id: str = "acc-001", status: str = "") -> list[dict]:
        raise RuntimeError("pool exhausted (simulated)")

    def close(self) -> None:
        self.close_calls += 1


def test_repo_close_returns_pool_connection(tmp_path):
    engine = _queue_engine(tmp_path)
    repo = _repo(engine)
    repo.list_campaigns("", status="running")  # 读路径 autobegin：连接被占住
    assert engine.pool.checkedout() == 1
    repo.close()
    assert engine.pool.checkedout() == 0  # 确定性归还，不等 GC


def test_repo_context_manager_closes_on_exception(tmp_path):
    engine = _queue_engine(tmp_path)
    with pytest.raises(RuntimeError):
        with _repo(engine) as repo:
            repo.list_campaigns("", status="running")
            assert engine.pool.checkedout() == 1
            raise RuntimeError("boom")
    assert engine.pool.checkedout() == 0  # 异常路径也归还


def test_campaign_tick_closes_self_created_repo(tmp_path, monkeypatch):
    engine = _queue_engine(tmp_path)
    spy = _SpyRepo(_repo(engine))
    _seed_running_campaign(spy)
    import control_plane.main as cp_main

    monkeypatch.setattr(cp_main, "_repo", lambda: spy)
    dispatched: list[str] = []
    out = asyncio.run(campaign_tick(dispatcher=_recording_dispatch(dispatched)))
    assert out["started"] == 1 and len(dispatched) == 1
    assert spy.close_calls == 1  # 修复点：自建 repo 一轮收口
    assert engine.pool.checkedout() == 0  # 连接归还，不等 GC


def test_campaign_tick_closes_repo_on_tick_error(monkeypatch):
    boom = _BoomRepo()
    import control_plane.main as cp_main

    monkeypatch.setattr(cp_main, "_repo", lambda: boom)
    # list_campaigns 的异常仍向上抛（_campaign_loop 兜底，行为不变），但 close 必须已收口
    with pytest.raises(RuntimeError):
        asyncio.run(campaign_tick(dispatcher=_recording_dispatch([])))
    assert boom.close_calls == 1


def test_campaign_tick_does_not_close_injected_repo(tmp_path):
    engine = _queue_engine(tmp_path)
    spy = _SpyRepo(_repo(engine))
    _seed_running_campaign(spy)
    asyncio.run(campaign_tick(spy, dispatcher=_recording_dispatch([])))
    assert spy.close_calls == 0  # 所有权在调用方：campaign_tick 不替注入 repo 收口
    # 注入 repo 跨轮复用仍可用（既有单测的行为契约）
    assert spy.list_campaigns("", status="running")


def test_pool_survives_repeated_ticks(tmp_path, monkeypatch):
    """集成回归：池容量 1 连跑 8 轮不枯竭（旧实现靠 GC 撞运气，新实现每轮确定性归还）。"""
    engine = _queue_engine(tmp_path, size=1, overflow=0)
    spy = _SpyRepo(_repo(engine))
    _seed_running_campaign(spy)
    import control_plane.main as cp_main

    monkeypatch.setattr(cp_main, "_repo", lambda: spy)
    for _ in range(8):
        asyncio.run(campaign_tick(dispatcher=_recording_dispatch([])))
    assert spy.close_calls == 8
    assert engine.pool.checkedout() == 0
