"""SIP 外呼配置面（spec 2026-09-12 Wave2 Task 8）:settings `sip` 段。

钉死三件事:
1. 默认值形状——后续任务（CP dispatcher / agent dialer）按这个形状读,键不可漂移。
2. 双后端读写往返——SQL 列 `sip_json` 与 InMemory dict 行为一致。
3. CP `/api/settings` 的 secret 掩码——`auth_password` 与 api_key 同档,
   PUT 空值保留旧密码（web 卡片掩码回显后直接保存不丢凭据）。
"""

from __future__ import annotations

import pytest

from bok_voice_business_db.repository import (
    InMemoryBusinessRepository,
    SqlAlchemyBusinessRepository,
)
from bok_voice_business_db.models import Base

SIP_DEFAULTS = {
    "mode": "mock",
    "trunk_id": "",
    "address": "",
    "auth_username": "",
    "auth_password": "",
    "numbers": [],
    "ringing_timeout_s": 30,
    "max_call_duration_s": 600,
}


def test_default_settings_has_sip():
    s = InMemoryBusinessRepository().get_settings()
    assert s["sip"]["mode"] == "mock"
    assert s["sip"]["ringing_timeout_s"] == 30
    assert s["sip"]["max_call_duration_s"] == 600


def test_sql_default_settings_has_sip():
    s = SqlAlchemyBusinessRepository.default_settings()
    assert s["sip"] == SIP_DEFAULTS


def test_save_settings_roundtrip():
    repo = InMemoryBusinessRepository()
    s = repo.get_settings()
    s["sip"] = {**s["sip"], "mode": "real", "trunk_id": "ST_x", "auth_password": "sekret"}
    repo.save_settings(s)
    got = repo.get_settings()
    assert got["sip"]["mode"] == "real" and got["sip"]["auth_password"] == "sekret"


def test_inmemory_save_settings_keeps_sip():
    """InMemory 白名单式重建容易漏键——显式钉死 sip 不被丢弃。"""
    repo = InMemoryBusinessRepository()
    repo.save_settings({**repo.get_settings(), "sip": {**SIP_DEFAULTS, "mode": "real", "numbers": ["+8613800138000"]}})
    got = repo.get_settings()
    assert got["sip"]["mode"] == "real"
    assert got["sip"]["numbers"] == ["+8613800138000"]


@pytest.fixture()
def sql_repo():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield SqlAlchemyBusinessRepository(session)
    session.close()
    engine.dispose()


def test_sql_settings_without_row_returns_sip_defaults(sql_repo):
    """空库（尚无 global 行）走 default_settings 分支,也要带 sip。"""
    assert sql_repo.get_settings()["sip"]["mode"] == "mock"


def test_sql_roundtrip_persists_sip(sql_repo):
    s = sql_repo.get_settings()
    s["sip"] = {**s["sip"], "mode": "real", "trunk_id": "ST_x", "auth_password": "sekret",
                "numbers": ["+8613800138000"], "ringing_timeout_s": 45}
    sql_repo.save_settings(s)
    got = sql_repo.get_settings()
    assert got["sip"]["mode"] == "real"
    assert got["sip"]["trunk_id"] == "ST_x"
    assert got["sip"]["auth_password"] == "sekret"
    assert got["sip"]["numbers"] == ["+8613800138000"]
    assert got["sip"]["ringing_timeout_s"] == 45
    # 同列其它段不被牵连。
    assert got["asr"]["provider"] == "qwen3_asr"


def test_sql_missing_sip_blob_falls_back_to_defaults(sql_repo):
    """老库补列后 sip_json='' —— 读侧必须回落默认段而非空 dict（否则 dialer 读不到 mode）。"""
    from bok_voice_business_db import models

    sql_repo.save_settings(sql_repo.get_settings())
    row = sql_repo.session.get(models.GlobalSetting, "global")
    row.sip_json = ""
    sql_repo.session.commit()
    assert sql_repo.get_settings()["sip"] == SIP_DEFAULTS


def test_sql_save_without_sip_key_writes_defaults(sql_repo):
    """不带 sip 键的旧调用（老 web 构建/外部脚本）保存后不得把 sip 段清空。"""
    s = sql_repo.get_settings()
    s.pop("sip")
    sql_repo.save_settings(s)
    assert sql_repo.get_settings()["sip"] == SIP_DEFAULTS


def test_global_settings_has_sip_json_column():
    from bok_voice_business_db import models

    assert "sip_json" in models.GlobalSetting.__table__.columns


# ---- CP 端点:默认值下发 + secret 掩码 + 空密码保留 ----


def _cp_client(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    return TestClient(cp_main.app), repo


def test_api_get_settings_includes_sip(monkeypatch):
    client, _ = _cp_client(monkeypatch)
    body = client.get("/api/settings").json()
    assert body["sip"]["mode"] == "mock"
    assert body["sip"]["max_call_duration_s"] == 600


def test_api_put_settings_roundtrips_sip(monkeypatch):
    client, _ = _cp_client(monkeypatch)
    current = client.get("/api/settings").json()
    payload = {**current, "sip": {**current["sip"], "mode": "real", "trunk_id": "ST_x",
                                  "address": "sip.example.com", "numbers": ["+8613800138000"]}}
    r = client.put("/api/settings", json=payload)
    assert r.status_code == 200, r.text
    got = client.get("/api/settings").json()["sip"]
    assert got["mode"] == "real" and got["trunk_id"] == "ST_x"
    assert got["numbers"] == ["+8613800138000"]


def test_api_settings_masks_sip_password(monkeypatch):
    client, repo = _cp_client(monkeypatch)
    current = client.get("/api/settings").json()
    repo.save_settings({**repo.get_settings(), "sip": {**current["sip"], "auth_password": "sekret"}})
    got = client.get("/api/settings").json()["sip"]
    assert got["auth_password"] == ""
    assert got["has_auth_password"] is True


def test_api_put_empty_password_keeps_old_value(monkeypatch):
    """web 卡片回显掩码（空串）直接保存 → 旧密码必须保留。"""
    client, repo = _cp_client(monkeypatch)
    s = repo.get_settings()
    repo.save_settings({**s, "sip": {**s["sip"], "mode": "real", "auth_password": "sekret"}})
    current = client.get("/api/settings").json()
    assert current["sip"]["auth_password"] == ""
    r = client.put("/api/settings", json=current)
    assert r.status_code == 200, r.text
    assert repo.get_settings()["sip"]["auth_password"] == "sekret"
    # 显式改密码照常覆盖。
    r = client.put("/api/settings", json={**current, "sip": {**current["sip"], "auth_password": "newpass"}})
    assert r.status_code == 200, r.text
    assert repo.get_settings()["sip"]["auth_password"] == "newpass"


def test_api_put_without_sip_key_keeps_existing(monkeypatch):
    """老调用方（不含 sip 键）保存 —— 默认 SipSettingsModel() 会把 mode 拉回 mock,
    这是已知并接受的契约（PUT=全量替换,与 asr/llm/tts/vad 同款）;此处只钉死
    「不炸、且形状完整」,防止旧客户端 500。"""
    client, repo = _cp_client(monkeypatch)
    s = repo.get_settings()
    repo.save_settings({**s, "sip": {**s["sip"], "mode": "real"}})
    body = client.get("/api/settings").json()
    body.pop("sip")
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    got = client.get("/api/settings").json()["sip"]
    assert got["mode"] == "mock"
    assert got["max_call_duration_s"] == 600
