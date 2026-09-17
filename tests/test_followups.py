"""CP 跟进工单端点(漏斗 v2 P1,spec §3.3):幂等防叠/kind 白名单/404/审计/RBAC。

姿势照现有 CP 测试:fixture 用 test_roster_api 的 _client_and_repo(monkeypatch)
helper 风格;auth-on 变体照 test_access_gates(登录换 JWT/机器通道 BOK_CP_TOKEN)。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from fastapi.testclient import TestClient

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

# 与 BOK_JWT_SECRET 异值(两把密钥同值会被 startup fail-closed 拒启)。
CP_TOKEN = "unit-test-cp-token-0123456789abcdef"
JWT_SECRET = "unit-test-jwt-secret-0123456789abcdef"
PW = "Passw0rd!x"


def _client_and_repo(monkeypatch):
    from control_plane.main import _repo, app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def _make_call(client) -> str:
    """先造一通 call 拿 CID(auth-off 直调建单端点)。"""
    r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "simulation"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_call(repo, call_id: str, account_id: str = "acc-001") -> str:
    """auth-on 下绕端点直接落一通 call(登录换 token 的姿势照 test_access_gates)。"""
    from bok_voice_core.policies import select_session_manifest
    from bok_voice_core.types import CallMode

    repo.create_call(select_session_manifest(
        session_id=call_id, account_id=account_id, object_id="",
        persona_id="", mode=CallMode.SIMULATION))
    return call_id


def test_create_and_idempotent(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    cid = _make_call(client)
    r1 = client.post(f"/api/calls/{cid}/followups", json={"kind": "complaint", "note": "货损"})
    assert r1.status_code == 200 and r1.json()["created"] is True
    assert r1.json()["kind"] == "complaint" and r1.json()["note"] == "货损"
    assert r1.json()["status"] == "open" and r1.json()["call_id"] == cid
    r2 = client.post(f"/api/calls/{cid}/followups", json={"kind": "complaint"})
    assert r2.status_code == 200 and r2.json()["created"] is False
    assert r2.json()["id"] == r1.json()["id"]  # 幂等返回同一 open 单
    # 同 call 不同 kind 照建新单(幂等按 (call, kind) 维度,不互斥)
    r3 = client.post(f"/api/calls/{cid}/followups", json={"kind": "track_order"})
    assert r3.status_code == 200 and r3.json()["created"] is True
    assert r3.json()["id"] != r1.json()["id"]
    assert len(repo.list_followups(call_id=cid)) == 2


def test_kind_whitelist(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    cid = _make_call(client)
    assert client.post(f"/api/calls/{cid}/followups", json={"kind": "nonsense"}).status_code == 400
    assert client.post(f"/api/calls/{cid}/followups", json={"kind": ""}).status_code == 400


def test_missing_call_404(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    assert client.post("/api/calls/nope/followups", json={"kind": "followup"}).status_code == 404


def test_audit_followup_create(monkeypatch):
    client, _ = _client_and_repo(monkeypatch)
    with client:  # startup 挂 audit tap:事件落 repo 才可经 /api/audit 查
        cid = _make_call(client)
        r = client.post(f"/api/calls/{cid}/followups", json={"kind": "track_order", "note": "查单"})
        assert r.status_code == 200
        fu_id = r.json()["id"]
        rows = client.get("/api/audit", params={"action": "followup.create", "call_id": cid}).json()
        assert any(
            row.get("subject_id") == cid and (row.get("detail") or {}).get("followup_id") == fu_id
            for row in rows
        )


def test_machine_channel_passes(monkeypatch):
    """agent 机器通道(BOK_CP_TOKEN)过闸建单;无 user 盖章(created_by='')。"""
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("BOK_CP_TOKEN", CP_TOKEN)
    client, repo = _client_and_repo(monkeypatch)
    _seed_call(repo, "call-fu-m")
    with client:  # startup 注入 user_lookup + identity_gate 打 machine 标
        h = {"Authorization": "Bearer " + CP_TOKEN}
        r = client.post("/api/calls/call-fu-m/followups", json={"kind": "followup"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["created"] is True and r.json()["created_by"] == ""


def test_cross_account_user_scoped(monkeypatch):
    """跨账号 user 404(不泄露存在性);本账号 user 可建单并盖 created_by。"""
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_JWT_SECRET", JWT_SECRET)
    client, repo = _client_and_repo(monkeypatch)
    repo.create_user(username="peon", password_hash=hash_password(PW), role="user",
                     org_id="org-t", account_id="acc-001")
    _seed_call(repo, "call-fu-other", account_id="acc-002")
    _seed_call(repo, "call-fu-mine", account_id="acc-001")
    with client:
        r = client.post("/api/auth/login", json={"username": "peon", "password": PW})
        assert r.status_code == 200, r.text
        h = {"Authorization": "Bearer " + r.json()["token"]}
        # 他账号通话 → 404(与 /api/calls/{id} 族口径一致)
        assert client.post("/api/calls/call-fu-other/followups", json={"kind": "followup"}, headers=h).status_code == 404
        # 本账号通话 → 可建单,盖本人 user_id
        r = client.post("/api/calls/call-fu-mine/followups", json={"kind": "followup"}, headers=h)
        assert r.status_code == 200 and r.json()["created"] is True
        assert r.json()["account_id"] == "acc-001"
        assert r.json()["created_by"] == repo.get_user_by_username("peon")["id"]


def test_followups_sql_backend_parity(monkeypatch):
    """SQL 后端同契约(生产真路径):幂等/白名单/404 与内存仓一致。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from bok_voice_business_db import models
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository
    from control_plane.main import app

    # TestClient 请求跑在 anyio 工作线程:StaticPool 令全线程共享同一连接
    # (内存库唯一),姿势照 test_roster_claim_unclaim_sql_backend_parity。
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.create_all(engine)
    repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine, expire_on_commit=False, future=True)())
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app)
    cid = _seed_call(repo, "call-fu-sql")

    r1 = client.post(f"/api/calls/{cid}/followups", json={"kind": "complaint", "note": "货损"})
    assert r1.status_code == 200 and r1.json()["created"] is True
    assert r1.json()["status"] == "open" and r1.json()["call_id"] == cid
    r2 = client.post(f"/api/calls/{cid}/followups", json={"kind": "complaint"})
    assert r2.status_code == 200 and r2.json()["created"] is False
    assert r2.json()["id"] == r1.json()["id"]
    assert client.post(f"/api/calls/{cid}/followups", json={"kind": "nonsense"}).status_code == 400
    assert client.post("/api/calls/nope/followups", json={"kind": "followup"}).status_code == 404
    assert len(repo.list_followups(account_id="acc-001")) == 1
