"""SIP 站点（`sip_sites`，spec 2026-09-13 sip-edge-thin-node-v2 §7 P1.5 Task 1）。

钉死三件事：
1. 双后端 CRUD 同契约——create/list/get/update 形状一致（numbers 走 JSON
   存取、datetime→ISO 串），单站点旧行为零变化。
2. update 白名单镜像（照 `update_roster_entry`）——未知键忽略、None 不修改、
   空串清空；两后端逐字段对齐，防静默分叉。
3. 默认站点合成——`site-local` 是虚拟站点：不入库、不进 `list_sites`，已有
   站点也不影响合成；livekit_url 取 env `LIVEKIT_URL`，缺省
   `ws://127.0.0.1:7880`。
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bok_voice_business_db import models
from bok_voice_business_db.repository import (
    InMemoryBusinessRepository,
    SqlAlchemyBusinessRepository,
)

SITE_KEYS = {
    "id", "account_id", "name", "livekit_url", "sip_edge",
    "trunk_id", "numbers", "region", "created_at", "updated_at",
}


def _repo() -> InMemoryBusinessRepository:
    return InMemoryBusinessRepository()


@pytest.fixture()
def sql_repo():
    """真 sqlite 后端：StaticPool=全线程共享同一内存库（照 test_sip_settings 姿势）。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield SqlAlchemyBusinessRepository(session)
    session.close()
    engine.dispose()


# ---- 默认站点合成 ----

def test_default_site_synth():
    repo = _repo()
    s = repo.get_default_site("acc-001")
    assert s["id"] == "site-local" and s["sip_edge"] == "none" and s["trunk_id"] == ""
    assert s["name"] == "local" and s["numbers"] == [] and s["region"] == ""
    assert s["account_id"] == "acc-001"
    assert set(s) == SITE_KEYS
    # 虚拟站点不入库：合成不产生行
    assert repo.list_sites("acc-001") == []


def test_default_site_livekit_url_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://vps.example:7880")
    assert _repo().get_default_site()["livekit_url"] == "wss://vps.example:7880"
    monkeypatch.delenv("LIVEKIT_URL")
    assert _repo().get_default_site()["livekit_url"] == "ws://127.0.0.1:7880"


def test_default_site_synth_sql_backend_parity(sql_repo, monkeypatch):
    """两后端同出口：默认站点合成不走库/不走行，形状逐键一致。"""
    monkeypatch.setenv("LIVEKIT_URL", "wss://vps.example:7880")
    got = sql_repo.get_default_site("acc-001")
    assert got == _repo().get_default_site("acc-001")
    assert sql_repo.list_sites("acc-001") == []


# ---- CRUD ----

def test_create_site_defaults():
    repo = _repo()
    s = repo.create_site(name="local-site")
    assert s["id"].startswith("site-") and s["account_id"] == "acc-001"
    assert s["name"] == "local-site" and s["livekit_url"] == ""
    assert s["sip_edge"] == "local" and s["trunk_id"] == "" and s["region"] == ""
    assert s["numbers"] == []
    assert datetime.fromisoformat(s["created_at"]) and datetime.fromisoformat(s["updated_at"])


def test_site_crud_and_trunk_update(sql_repo):
    """Task 1 brief 核心断言：建站 → trunk 回填 → numbers 更新 → 默认站点不受影响。"""
    repo = sql_repo
    s = repo.create_site(name="hk-edge", livekit_url="wss://vps.example:7880",
                         sip_edge="cloud", numbers=["+12025550123"])
    assert s["numbers"] == ["+12025550123"] and s["trunk_id"] == ""
    repo.update_site(s["id"], trunk_id="ST_abc123")
    assert repo.get_site(s["id"])["trunk_id"] == "ST_abc123"
    repo.update_site(s["id"], numbers=["+12025550123", "+12025550124"])
    assert len(repo.get_site(s["id"])["numbers"]) == 2
    assert repo.get_default_site("acc-001")["id"] == "site-local"  # 有行也不影响默认合成


def test_site_crud_inmemory(sql_repo):
    """InMemory 逐字段镜像同一脚本（真后端在上面单独跑）。"""
    for repo in (_repo(), sql_repo):
        s = repo.create_site(name="hk-edge", livekit_url="wss://vps.example:7880",
                             sip_edge="cloud", numbers=["+12025550123"])
        assert s["numbers"] == ["+12025550123"] and s["trunk_id"] == ""
        repo.update_site(s["id"], trunk_id="ST_abc123")
        assert repo.get_site(s["id"])["trunk_id"] == "ST_abc123"
        repo.update_site(s["id"], numbers=["+12025550123", "+12025550124"])
        assert len(repo.get_site(s["id"])["numbers"]) == 2
        assert repo.get_default_site("acc-001")["id"] == "site-local"


def test_sql_numbers_persist_as_json(sql_repo):
    s = sql_repo.create_site(name="hk", numbers=["+12025550123", "+12025550124"])
    raw = sql_repo.session.get(models.SipSite, s["id"]).numbers_json
    assert json.loads(raw) == ["+12025550123", "+12025550124"]
    # 坏 JSON 不炸读侧（防脏行拖垮站点列表）
    row = sql_repo.session.get(models.SipSite, s["id"])
    row.numbers_json = "{not json"
    sql_repo.session.commit()
    assert sql_repo.get_site(s["id"])["numbers"] == []


def test_list_sites_created_at_asc_and_account_scoped(sql_repo):
    for repo in (_repo(), sql_repo):
        a = repo.create_site(name="a")
        time.sleep(0.002)
        b = repo.create_site(name="b")
        time.sleep(0.002)
        repo.create_site("acc-999", name="other")
        assert [r["name"] for r in repo.list_sites("acc-001")] == ["a", "b"]
        assert [r["name"] for r in repo.list_sites("acc-999")] == ["other"]
        assert repo.get_site(a["id"])["name"] == "a"
        assert repo.get_site(b["id"])["name"] == "b"


def test_get_and_update_missing_site_return_none(sql_repo):
    for repo in (_repo(), sql_repo):
        assert repo.get_site("site-nope") is None
        assert repo.update_site("site-nope", name="x") is None


# ---- update 白名单（照 update_roster_entry）----

def test_update_site_whitelist_none_and_empty(sql_repo):
    for repo in (_repo(), sql_repo):
        s = repo.create_site(name="hk-edge", livekit_url="wss://vps.example:7880",
                             sip_edge="cloud", trunk_id="ST_1", region="hk",
                             numbers=["+12025550123"])
        # 未知键（含 id/account_id/created_at/updated_at）一律忽略——两后端不分叉
        repo.update_site(s["id"], id="site-hacked", account_id="acc-999",
                         created_at="2000-01-01T00:00:00+00:00", bogus="x")
        after = repo.get_site(s["id"])
        assert after["id"] == s["id"] and after["account_id"] == "acc-001"
        # SQLite DateTime 列不回读 tz offset（InMemory 直存 ISO 串保留）——按既有
        # 口径归一后再比时刻本身。
        assert datetime.fromisoformat(after["created_at"]).replace(tzinfo=None) == (
            datetime.fromisoformat(s["created_at"]).replace(tzinfo=None))
        # None 不修改
        repo.update_site(s["id"], name=None, trunk_id=None, numbers=None, region=None)
        kept = repo.get_site(s["id"])
        assert kept["name"] == "hk-edge" and kept["trunk_id"] == "ST_1"
        assert kept["numbers"] == ["+12025550123"] and kept["region"] == "hk"
        # 空串/空列表清空
        repo.update_site(s["id"], name="", trunk_id="", numbers=[], region="")
        cleared = repo.get_site(s["id"])
        assert cleared["name"] == "" and cleared["trunk_id"] == ""
        assert cleared["numbers"] == [] and cleared["region"] == ""
        # 值改了才刷新 updated_at（SQL onupdate / InMemory 同款账本）
        before = cleared["updated_at"]
        time.sleep(0.002)
        repo.update_site(s["id"], sip_edge="none")
        assert repo.get_site(s["id"])["sip_edge"] == "none"
        assert repo.get_site(s["id"])["updated_at"] > before


def test_update_site_numbers_isolation(sql_repo):
    """读侧 numbers 是副本：调用方原地改不污染库（SQL 天然，InMemory 对齐）。"""
    for repo in (_repo(), sql_repo):
        s = repo.create_site(name="hk", numbers=["+12025550123"])
        got = repo.get_site(s["id"])
        got["numbers"].append("+19999999999")
        assert repo.get_site(s["id"])["numbers"] == ["+12025550123"]


def test_update_site_non_list_numbers_falls_back_to_empty(sql_repo):
    """畸形 numbers（字符串/标量）不写坏行——两后端同归一。"""
    for repo in (_repo(), sql_repo):
        s = repo.create_site(name="hk", numbers=["+12025550123"])
        repo.update_site(s["id"], numbers="+19999999999")  # type: ignore[arg-type]
        assert repo.get_site(s["id"])["numbers"] == []
