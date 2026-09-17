"""campaign 挂 `site_id` + dial 块 trunk 按 site 优先（spec 2026-09-13 sip-edge-thin-node-v2 §7 P1.5 Task 2）。

钉死三件事：

1. **dial 块 trunk 解析规则**——campaign.site_id 指向的站点 trunk_id 非空 → 用站点
   的 trunk；否则（无 site_id / 站点不存在 / 虚拟 `site-local` / 站点 trunk 未注册）
   回退 settings `sip.trunk_id`。单站点旧行为零变化（campaign 无 site_id 时逐字旧值）。
2. **campaign dict 带 `site_id` 键**——create 透传、update 白名单（None 不修改/空串
   清空）、未知键忽略；SQL/InMemory 双后端逐字段镜像。
3. **CP 建战役端点透传** `site_id`（body → repo）。

trunk 解析在 `campaign._start_call` 组装 dial 块处，测试经 `campaign_tick` 的注入
dispatcher 读真实 dial 块（fake 形状照 `tests/test_campaign_loop.py`）。
"""

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

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control-plane"))

from bok_voice_business_db import models  # noqa: E402
from bok_voice_business_db.repository import (  # noqa: E402
    InMemoryBusinessRepository,
    SqlAlchemyBusinessRepository,
)
from control_plane.campaign import campaign_tick  # noqa: E402

SETTINGS_TRUNK = "ST_settings_trunk"
SITE_TRUNK = "ST_site_trunk"


@pytest.fixture()
def sql_repo():
    """真 sqlite 后端：StaticPool=全线程共享同一内存库（照 test_sip_sites_repo 姿势）。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield SqlAlchemyBusinessRepository(session)
    session.close()
    engine.dispose()


class _FakeDispatch:
    """记录 dispatcher 调用（形状照 test_campaign_loop 的 fake_dispatch）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, room: str, metadata: str) -> None:
        self.calls.append((room, metadata))

    def dial(self, index: int = 0) -> dict:
        return json.loads(self.calls[index][1])["dial"]


def _running_campaign(repo, *, site_id: str = "", settings_trunk: str = SETTINGS_TRUNK) -> dict:
    """建一通名单 + running 战役 + settings sip 段（含 trunk 值）。"""
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85291112222"})
    camp = repo.create_campaign(
        "acc-001", name="t", template_id="", persona_id="", language="zh",
        gap_seconds=5, object_ids=[obj["id"]], site_id=site_id,
    )
    repo.update_campaign(camp["id"], status="running")
    repo.save_settings({"sip": {"mode": "real", "trunk_id": settings_trunk}})
    return camp


def _tick_dial(repo) -> dict:
    fake = _FakeDispatch()
    out = asyncio.run(campaign_tick(repo, dispatcher=fake))
    assert out["started"] == 1 and len(fake.calls) == 1
    return fake.dial()


# ---- dial 块 trunk 解析：site 优先 / settings 兜底 ----

def test_dial_block_trunk_prefers_site_over_settings(sql_repo):
    """site.trunk_id 非空时压过 settings `sip.trunk_id`（同一通两者都在配）。"""
    site = sql_repo.create_site(name="hk", trunk_id=SITE_TRUNK)
    _running_campaign(sql_repo, site_id=site["id"])
    assert _tick_dial(sql_repo)["trunk_id"] == SITE_TRUNK


def test_dial_block_trunk_falls_back_to_settings(sql_repo):
    """campaign 无 site_id（旧数据/旧调用）→ 逐字走 settings，单站点零变化。"""
    _running_campaign(sql_repo)
    assert _tick_dial(sql_repo)["trunk_id"] == SETTINGS_TRUNK


def test_dial_block_trunk_site_without_trunk_falls_back_to_settings(sql_repo):
    """站点存在但 trunk 未注册（trunk_id 空）→ 回退 settings 而非发空 trunk。"""
    site = sql_repo.create_site(name="hk")
    _running_campaign(sql_repo, site_id=site["id"])
    assert _tick_dial(sql_repo)["trunk_id"] == SETTINGS_TRUNK


def test_dial_block_trunk_default_site_id_falls_back_to_settings(sql_repo):
    """`site-local` 恒合成不入库：get_site 得 None → 安全回退 settings（T1 注记语义）。"""
    _running_campaign(sql_repo, site_id="site-local")
    assert sql_repo.get_site("site-local") is None  # 虚拟站点本就不该被 get_site 命中
    assert _tick_dial(sql_repo)["trunk_id"] == SETTINGS_TRUNK


def test_dial_block_trunk_site_wins_when_settings_trunk_missing(sql_repo):
    """settings 未配 trunk（单站点默认空）也要用上站点 trunk。"""
    site = sql_repo.create_site(name="hk", trunk_id=SITE_TRUNK)
    _running_campaign(sql_repo, site_id=site["id"], settings_trunk="")
    assert _tick_dial(sql_repo)["trunk_id"] == SITE_TRUNK


def test_dial_block_trunk_resolution_both_backends(sql_repo):
    """解析规则两后端同出口（SQL 上面已逐条测，InMemory 镜像同一脚本）。"""
    for repo in (InMemoryBusinessRepository(), sql_repo):
        site = repo.create_site(name="hk", trunk_id=SITE_TRUNK)
        _running_campaign(repo, site_id=site["id"])
        assert _tick_dial(repo)["trunk_id"] == SITE_TRUNK


# ---- campaign dict 带 site_id + 双后端白名单 ----

def test_campaign_site_id_roundtrip_both_backends(sql_repo):
    for repo in (InMemoryBusinessRepository(), sql_repo):
        site = repo.create_site(name="hk", trunk_id=SITE_TRUNK)
        obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85291112222"})
        camp = repo.create_campaign(
            "acc-001", name="t", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[obj["id"]], site_id=site["id"],
        )
        assert camp["site_id"] == site["id"]
        assert repo.get_campaign(camp["id"])["site_id"] == site["id"]
        # 不传 site_id 的旧调用 → 空串（不得是 None/缺键）
        plain = repo.create_campaign(
            "acc-001", name="t2", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[obj["id"]],
        )
        assert plain["site_id"] == ""
        assert repo.get_campaign(plain["id"])["site_id"] == ""
        by_id = {c["id"]: c["site_id"] for c in repo.list_campaigns("acc-001")}
        assert by_id == {camp["id"]: site["id"], plain["id"]: ""}


def test_update_campaign_site_id_whitelist_both_backends(sql_repo):
    """白名单照 update_roster_entry：未知键忽略、None 不修改、空串清空、双后端镜像。"""
    for repo in (InMemoryBusinessRepository(), sql_repo):
        obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85291112222"})
        camp = repo.create_campaign(
            "acc-001", name="t", template_id="", persona_id="", language="zh",
            gap_seconds=5, object_ids=[obj["id"]], site_id="site-1",
        )
        # 改挂另一站点
        repo.update_campaign(camp["id"], site_id="site-2")
        assert repo.get_campaign(camp["id"])["site_id"] == "site-2"
        # 未知键忽略（含 id/created_at）
        repo.update_campaign(camp["id"], id="hacked", bogus="x")
        assert repo.get_campaign(camp["id"])["site_id"] == "site-2"
        # None 不修改
        repo.update_campaign(camp["id"], site_id=None)
        assert repo.get_campaign(camp["id"])["site_id"] == "site-2"
        # 空串清空（回到 settings 兜底）
        repo.update_campaign(camp["id"], site_id="")
        assert repo.get_campaign(camp["id"])["site_id"] == ""


def test_sql_campaign_site_id_persists_column(sql_repo):
    """真后端：site_id 落库（不只是 dict 出口的默认值）。"""
    obj = sql_repo.create_object("acc-001", {"display_name": "A", "phone": "+8521"})
    camp = sql_repo.create_campaign(
        "acc-001", name="t", template_id="", persona_id="", language="zh",
        gap_seconds=5, object_ids=[obj["id"]], site_id="site-x",
    )
    row = sql_repo.session.get(models.Campaign, camp["id"])
    assert row.site_id == "site-x"


# ---- CP 建战役端点透传 site_id ----

def test_create_campaign_endpoint_passes_site_id(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    site = repo.create_site(name="hk", trunk_id=SITE_TRUNK)
    obj = repo.create_object("acc-001", {"display_name": "A", "phone": "+85291112222"})
    client = TestClient(app)

    r = client.post("/api/campaigns", json={
        "name": "波次一", "object_ids": [obj["id"]], "language": "zh",
        "site_id": site["id"],
    })
    assert r.status_code == 200
    camp = r.json()
    assert camp["site_id"] == site["id"]
    assert repo.get_campaign(camp["id"])["site_id"] == site["id"]
    # 不传 site_id → 空串（旧前端零变化）
    r2 = client.post("/api/campaigns", json={
        "name": "波次二", "object_ids": [obj["id"]], "language": "zh",
    })
    assert r2.status_code == 200 and r2.json()["site_id"] == ""


# ---- 存量库补列（deps.py `_ensure_column`，幂等）----

def test_migration_adds_campaign_site_id_column(tmp_path, monkeypatch):
    """存量库 campaigns 无 site_id 列 → CP 启动补列；二启幂等不报错。"""
    import sqlite3

    db = tmp_path / "campaigns_site.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE campaigns (id VARCHAR(64) PRIMARY KEY, "
        "account_id VARCHAR(64) DEFAULT '', name VARCHAR(255) DEFAULT '', "
        "template_id VARCHAR(64) DEFAULT '', persona_id VARCHAR(64) DEFAULT '', "
        "language VARCHAR(16) DEFAULT 'zh', status VARCHAR(16) DEFAULT 'draft', "
        "gap_seconds INTEGER DEFAULT 5, scripts_json TEXT DEFAULT '', "
        "created_at DATETIME, finished_at DATETIME);"
    )
    conn.close()

    from control_plane.deps import build_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    build_engine()  # 首启：补列
    build_engine()  # 二启：幂等不报错

    c = sqlite3.connect(db)
    cols = [r[1] for r in c.execute("PRAGMA table_info(campaigns)")]
    c.close()
    assert "site_id" in cols, cols


def test_migration_adds_global_settings_campaign_json_column(tmp_path, monkeypatch):
    """T3b：存量库 global_settings 无 campaign_json 列 → CP 启动补列；二启幂等不报错。"""
    import sqlite3

    db = tmp_path / "global_settings_campaign.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE global_settings (id VARCHAR(64) PRIMARY KEY, "
        "asr_json TEXT DEFAULT '{}', llm_json TEXT DEFAULT '{}', "
        "tts_json TEXT DEFAULT '{}', vad_json TEXT DEFAULT '{}', "
        "sip_json TEXT DEFAULT '', policy VARCHAR(64) DEFAULT 'offline_first', "
        "updated_at DATETIME);"
    )
    conn.close()

    from control_plane.deps import build_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    build_engine()  # 首启：补列
    build_engine()  # 二启：幂等不报错

    c = sqlite3.connect(db)
    cols = [r[1] for r in c.execute("PRAGMA table_info(global_settings)")]
    c.close()
    assert "campaign_json" in cols, cols
