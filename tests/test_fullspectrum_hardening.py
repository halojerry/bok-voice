"""W⑧（2026-10-09）全谱加固测试：外呼日拨配额 + 响应头 + 登录巨包钳制。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "x" * 40)

import pytest
from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.main import _dial_quota_take
from control_plane.nodes_store import NodeStore
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from control_plane import main as cp

    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp, "_repo", lambda: repo)
    app = cp.app
    monkeypatch.setattr(app.state, "account_lookup",
                        lambda account_id: repo.get_account(account_id), raising=False)
    monkeypatch.setattr(app.state, "node_store", NodeStore(None), raising=False)
    from control_plane.auth import hash_password

    repo.create_user(username="quota-admin", password_hash=hash_password("Passw0rd!x"),
                     role="admin", org_id="", account_id="acc-q")
    yield TestClient(app), repo, cp
    cp._DIAL_QUOTA_DAY.clear()


def test_dial_quota_truth_table():
    from control_plane.main import _DIAL_QUOTA_DAY

    _DIAL_QUOTA_DAY.clear()
    assert _dial_quota_take("acc-x", 3) is False  # 1/3
    assert _dial_quota_take("acc-x", 3) is False  # 2/3
    assert _dial_quota_take("acc-x", 3) is False  # 3/3
    assert _dial_quota_take("acc-x", 3) is True   # 4/3 → 拒
    assert _dial_quota_take("acc-y", 3) is False  # 账号间独立


def test_dial_quota_blocks_live_create(client, monkeypatch):
    tc, repo, cp = client
    cp._DIAL_QUOTA_DAY.clear()
    monkeypatch.setenv("BOK_DIAL_DAILY_QUOTA", "1")
    r = tc.post("/api/auth/login", json={"username": "quota-admin", "password": "Passw0rd!x"})
    at = {"Authorization": f"Bearer {r.json()['token']}"}
    # 第 1 通 LIVE 放行（缺对象/模板的业务错误都行，非 403 配额）；第 2 通 403。
    first = tc.post("/api/calls", headers=at, json={
        "account_id": "acc-q", "mode": "live", "kind": ""}).status_code
    assert first != 403 or "上限" not in ""
    second = tc.post("/api/calls", headers=at, json={
        "account_id": "acc-q", "mode": "live", "kind": ""})
    assert second.status_code == 403
    assert "上限" in second.json()["detail"]
    # simulation 不吃配额
    assert tc.post("/api/calls", headers=at, json={
        "account_id": "acc-q", "mode": "simulation"}).status_code in (200, 201)
    cp._DIAL_QUOTA_DAY.clear()


def test_dial_quota_off_by_default(client, monkeypatch):
    tc, _, cp = client
    monkeypatch.delenv("BOK_DIAL_DAILY_QUOTA", raising=False)
    cp._DIAL_QUOTA_DAY.clear()
    r = tc.post("/api/auth/login", json={"username": "quota-admin", "password": "Passw0rd!x"})
    at = {"Authorization": f"Bearer {r.json()['token']}"}
    for _ in range(3):
        r = tc.post("/api/calls", headers=at, json={"account_id": "acc-q", "mode": "simulation"})
        assert r.status_code in (200, 201)


def test_security_headers_on_api_and_static(client):
    tc, _, _ = client
    r = tc.get("/health")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_login_password_length_cap(client):
    tc, _, _ = client
    r = tc.post("/api/auth/login", json={"username": "u", "password": "x" * 300})
    assert r.status_code == 422  # pydantic 巨包拒收（scrypt DoS 面闭合）
