"""SaaS 客户生命周期（W① 2026-10-09）：accounts CRUD / root 专属面 / 到期缓存件。

契约：
- ``POST /api/accounts`` root 专属：建 Account（acc-<hex8>）+ 同账号 admin 用户
  （默认章=页键默认集+管理键「员工管理」）；密码 ≥12 位；duration_days 0~3650。
- ``POST /api/accounts/{id}/renew``：未到期顺延、已过期/永久从当下起算。
- ``PATCH /api/accounts/{id}``：改显示名 / expires_at ISO / "permanent" 清期限。
- repo 双后端同契约（本文件钉内存后端；SQL 侧由 _account_to_dict 形状钉）。
- auth.parse_expires_at / account_expired_lookup：ISO→aware、坏值=None（=永久）、
  TTL 缓存 + clear_account_expiry_cache。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-accounts")

import datetime
import json

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import (
    account_expired_lookup,
    clear_account_expiry_cache,
    hash_password,
    parse_expires_at,
)

ADMIN_PW = "Customer-Pw-" + "12chars"  # 夹具口令（≥12 位）


def _client_and_repo(monkeypatch):
    from control_plane.main import app
    from fastapi.testclient import TestClient

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app), repo


def _mk_user(repo, username, role="user", account="acc-001", password="Passw0rd!x"):
    return repo.create_user(
        username=username, password_hash=hash_password(password),
        role=role, org_id="org-t", account_id=account,
    )


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---- parse_expires_at / 到期缓存件（auth 层单点） ----


def test_parse_expires_at_shapes():
    assert parse_expires_at(None) is None
    assert parse_expires_at("") is None
    assert parse_expires_at("garbage") is None
    dt = parse_expires_at("2030-01-01T00:00:00+00:00")
    assert dt is not None and dt.tzinfo is not None
    naive = parse_expires_at(datetime.datetime(2030, 1, 1))
    assert naive is not None and naive.tzinfo is not None  # naive→按 UTC 补


def test_account_expired_lookup_cache_and_clear(monkeypatch):
    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    # 裸 app（无 TestClient lifespan）——手动接线 startup 同款 account_lookup。
    monkeypatch.setattr(app.state, "account_lookup", lambda account_id: repo.get_account(account_id),
                        raising=False)
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    repo.create_account(account_id="acc-exp", expires_at=past)
    repo.create_account(account_id="acc-forever")  # expires_at=None

    assert account_expired_lookup(_req(app), "acc-exp") is True
    assert account_expired_lookup(_req(app), "acc-forever") is False
    assert account_expired_lookup(_req(app), "acc-missing") is False  # 行缺失=零执法

    # renew 路径清缓存后立刻翻绿
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    repo.update_account("acc-exp", expires_at=future)
    clear_account_expiry_cache("acc-exp")
    assert account_expired_lookup(_req(app), "acc-exp") is False


def _req(app):
    """构造带 app.state 的最小 Request（account_lookup 经 app.state 注入）。"""
    from fastapi import Request

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "app": app}
    return Request(scope)


# ---- 端点：root 专属 + 建号链 ----


def test_accounts_root_only(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    _mk_user(repo, "cust-admin", role="admin", account="acc-x", password="Passw0rd!x")
    _mk_user(repo, "peon", role="user", account="acc-x", password="Passw0rd!x")
    at = _login(client, "cust-admin", "Passw0rd!x")
    ut = _login(client, "peon", "Passw0rd!x")

    assert client.get("/api/accounts", headers=_auth(at)).status_code == 403
    assert client.get("/api/accounts", headers=_auth(ut)).status_code == 403
    assert (client.post("/api/accounts", headers=_auth(at),
                       json={"admin_username": "n", "admin_password": ADMIN_PW}).status_code == 403)


def test_create_account_and_default_admin_stamp(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    r = client.post("/api/accounts", json={
        "display_name": "客户甲", "admin_username": "jia-admin",
        "admin_password": ADMIN_PW, "duration_days": 90,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("acc-") and body["expired"] is False
    assert body["admin_username"] == "jia-admin" and body["user_count"] == 1
    assert body["expires_at"]  # 90 天 → 有期限

    row = repo.get_account(body["id"])
    assert row is not None and row["expires_at"]
    admin = repo.get_user_by_username("jia-admin")
    assert admin and admin["account_id"] == body["id"] and admin["role"] == "admin"
    # 默认章：页键默认集 + 管理键 users（开箱自建员工；其余管理键全关）
    grants = json.loads(admin["permissions_json"])
    assert "users" in grants and "settings" not in grants and "audit" not in grants
    assert "calls" in grants

    # 列表带 admin 用户名/员工数
    lst = client.get("/api/accounts").json()
    mine = [a for a in lst if a["id"] == body["id"]]
    assert mine and mine[0]["admin_username"] == "jia-admin" and mine[0]["user_count"] == 1


def test_create_account_validation(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    base = {"display_name": "", "admin_username": "v-admin", "admin_password": ADMIN_PW,
            "duration_days": 30}
    assert client.post("/api/accounts", json={**base, "admin_password": "short"}).status_code == 400
    assert client.post("/api/accounts", json={**base, "duration_days": 9999}).status_code == 400
    assert client.post("/api/accounts", json={**base, "duration_days": -1}).status_code == 400
    assert client.post("/api/accounts", json={**base, "admin_username": ""}).status_code == 400
    assert client.post("/api/accounts", json=base).status_code == 201
    # 重名 → 409（用户名全局唯一约束）
    assert client.post("/api/accounts", json=base).status_code == 409
    # duration=0 → 永久（expires_at 空）
    body = client.post("/api/accounts", json={
        "admin_username": "perm-admin", "admin_password": ADMIN_PW,
        "duration_days": 0}).json()
    assert body["expires_at"] == "" and body["expired"] is False


def test_renew_semantics(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    acc = client.post("/api/accounts", json={
        "admin_username": "r-admin", "admin_password": ADMIN_PW,
        "duration_days": 30}).json()
    aid = acc["id"]

    now = datetime.datetime.now(datetime.timezone.utc)
    # 未到期：顺延（base=现值）→ 新期限 ≈ now+30d+90d
    repo.update_account(aid, expires_at=now + datetime.timedelta(days=30))
    clear_account_expiry_cache(aid)
    body = client.post(f"/api/accounts/{aid}/renew", json={"days": 90}).json()
    exp = parse_expires_at(body["expires_at"])
    assert exp is not None
    assert exp > now + datetime.timedelta(days=100)
    assert exp < now + datetime.timedelta(days=125)

    # 已过期：从当下起算（不倒贴）
    repo.update_account(aid, expires_at=now - datetime.timedelta(days=10))
    clear_account_expiry_cache(aid)
    body = client.post(f"/api/accounts/{aid}/renew", json={"days": 30}).json()
    exp2 = parse_expires_at(body["expires_at"])
    assert exp2 is not None and now < exp2 < now + datetime.timedelta(days=35)
    assert body["expired"] is False

    # 校验：days 越界/账号不存在
    assert client.post(f"/api/accounts/{aid}/renew", json={"days": 0}).status_code == 400
    assert client.post("/api/accounts/acc-none/renew", json={"days": 30}).status_code == 404


def test_patch_account_display_and_permanent(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    acc = client.post("/api/accounts", json={
        "admin_username": "p-admin", "admin_password": ADMIN_PW,
        "duration_days": 30}).json()
    aid = acc["id"]
    assert client.patch(f"/api/accounts/{aid}", json={"display_name": "新名字"}).json()["display_name"] == "新名字"
    body = client.patch(f"/api/accounts/{aid}", json={"expires_at": "permanent"}).json()
    assert body["expires_at"] == "" and body["expired"] is False
    assert client.patch(f"/api/accounts/{aid}", json={"expires_at": "not-a-date"}).status_code == 400
    assert client.patch("/api/accounts/acc-none", json={"display_name": "x"}).status_code == 404
