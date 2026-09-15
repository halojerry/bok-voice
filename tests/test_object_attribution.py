"""2026-09-16 深测修复回归：object_id 跨账号归属（P2-3）与 campaign 全账号循环。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")

from fastapi.testclient import TestClient

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!"


def _make(monkeypatch, users=()):
    from control_plane import main as cp_main

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    for u in users:
        repo.create_user(username=u["username"], password_hash=hash_password(PW),
                         role=u.get("role", "user"), org_id="org-t",
                         account_id=u.get("account", "acc-001"))
    return TestClient(cp_main.app), repo


def _object(repo, account, phone="13800001111"):
    return repo.create_object(account, {"display_name": f"obj-{account}", "phone": phone})


def _login(client, username="peon"):
    # 归属闸是身份闸（identity is not None 才收紧），匿名=auth-off 本机形态直通，
    # 须以建号用户登录走 Bearer 才能驱动闸（与 test_scope.py 同姿势）。
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_campaign_rejects_cross_account_objects(monkeypatch):
    client, repo = _make(monkeypatch, users=[{"username": "peon", "role": "user"}])
    headers = _login(client)
    foreign = _object(repo, "acc-002")
    r = client.post("/api/campaigns", headers=headers, json={
        "account_id": "acc-001", "name": "exfil", "object_ids": [foreign["id"]],
    })
    assert r.status_code == 404, r.text  # 旧版 201，items[].phone 回显他账号手机号


def test_create_call_rejects_cross_account_object(monkeypatch):
    client, repo = _make(monkeypatch, users=[{"username": "peon", "role": "user"}])
    headers = _login(client)
    foreign = _object(repo, "acc-002")
    r = client.post("/api/calls", headers=headers,
                    json={"account_id": "acc-001", "object_id": foreign["id"]})
    assert r.status_code == 404  # 旧版 200 且 template_id 泄露他账号话术绑定
    # 本账号对象照常
    mine = _object(repo, "acc-001")
    assert client.post("/api/calls", headers=headers,
                       json={"account_id": "acc-001", "object_id": mine["id"]}).status_code == 200


def test_campaign_loop_covers_all_accounts(monkeypatch):
    repo = InMemoryBusinessRepository()
    camp_a = repo.create_campaign("acc-001", name="a", template_id="", persona_id="",
                                  language="zh", gap_seconds=5, object_ids=[])
    camp_b = repo.create_campaign("acc-002", name="b", template_id="", persona_id="",
                                  language="zh", gap_seconds=5, object_ids=[])
    repo.update_campaign(camp_a["id"], status="running")
    repo.update_campaign(camp_b["id"], status="running")
    running = repo.list_campaigns("", status="running")
    assert {c["id"] for c in running} == {camp_a["id"], camp_b["id"]}


def test_settle_docs_path_sanitizes_object_id(monkeypatch, tmp_path):
    from control_plane import main as cp_main

    # brief 原文期望值 ".._.._acc-002_knowledge_evil" 与其自身实现矛盾：
    # 白名单 [A-Za-z0-9_-] 不含 `.`，`../../` 六字符全收成下划线（`.` 漏过会让
    # 纯 ".." 原样成段=遍历仍在）。以实现/docstring/commit message 一致的收敛为准。
    assert cp_main._safe_segment("../../acc-002/knowledge/evil", "unknown") == "______acc-002_knowledge_evil"
    assert cp_main._safe_segment("..", "unknown") == "__"  # 纯 .. 不成遍历段
    assert cp_main._safe_segment("", "unknown") == "unknown"
    assert cp_main._safe_segment("obj-abc123", "unknown") == "obj-abc123"
