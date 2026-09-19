"""2026-09-19 审计修复（CP 面 Wave1）回归钉：

- web_logs 角色闸（settings 键，对齐同族 diag 读面——任意 user 曾可长期写盘）；
- session-report 双 worker CAS 合并（fwd/rev 并发上报，旧读-改-写整列互相吞）；
- /api/token 跨账号 call_id 不得借道翻 ACTIVE（same_account 归属校验）。

测试口令走模块常量 PW（测试夹具，非真实凭据）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-cp-wave1-32bytes-min")

sys.path.insert(0, str(ROOT / "packages" / "business-db"))

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from control_plane.auth import hash_password  # noqa: E402

PW = "Passw0rd!x"  # 测试夹具口令，非真实凭据
MACHINE = {"Authorization": "Bearer machine-token-cp-wave1"}


@pytest.fixture()
def env(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    # 加固态只在本 fixture 作用域（monkeypatch 还原）——模块顶层 setdefault
    # 会把整仓测试推入 auth-on、mass 401（2026-09-19 首跑实证）。JWT secret
    # 显式钉强值:别处模块级 setdefault 的短 secret 会让 startup fail-closed。
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_JWT_SECRET", "cp-wave1-fixture-secret-0123456789abcdef-32B")
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token-cp-wave1")
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    # with 形态触发 lifespan：/api/calls 摸 app.state.node_store（启动装配）。
    with TestClient(app) as client:
        yield client, repo


def _mk_user(repo, username, role="user", account="acc-001"):
    return repo.create_user(
        username=username,
        password_hash=hash_password(PW),
        role=role,
        org_id="org-t",
        account_id=account,
    )


def _login(client, username, password=PW):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _mk_call(client, account="acc-001", room="room-w1", headers=None):
    r = client.post(
        "/api/calls",
        json={"room_name": room, "account_id": account, "kind": "interpret"},
        headers=headers or MACHINE,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body.get("call_id") or body.get("id")


# ---- web_logs 角色闸 ----


def test_web_logs_user_role_forbidden(env, monkeypatch):
    client, repo = env
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    _mk_user(repo, "wl-user")
    tok = _login(client, "wl-user")
    r = client.post("/api/web_logs", json={"events": [{"k": "v"}]}, headers=_auth(tok))
    assert r.status_code == 403, r.text


def test_web_logs_machine_channel_ok(env, monkeypatch):
    client, _repo = env
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    r = client.post("/api/web_logs", json={"events": [{"k": "v"}]}, headers=MACHINE)
    assert r.status_code == 200, r.text


# ---- session-report 双 worker 合并 ----


def test_session_report_two_workers_merged(env):
    client, repo = env
    call_id = _mk_call(client)
    for worker, tag in (("interp-fwd", "fwd"), ("interp-rev", "rev")):
        r = client.post(
            f"/api/calls/{call_id}/session-report",
            json={"worker": worker, "summary": tag},
            headers=MACHINE,
        )
        assert r.status_code == 200, r.text
    arr = json.loads(repo.get_call(call_id)["session_reports_json"])
    assert sorted(e["worker"] for e in arr) == ["interp-fwd", "interp-rev"]


def test_session_report_same_worker_retry_replaces(env):
    client, repo = env
    call_id = _mk_call(client)
    for n in (1, 2):
        r = client.post(
            f"/api/calls/{call_id}/session-report",
            json={"worker": "interp-fwd", "attempt": n},
            headers=MACHINE,
        )
        assert r.status_code == 200, r.text
        assert r.json()["replaced"] == (n == 2)
    arr = json.loads(repo.get_call(call_id)["session_reports_json"])
    assert len(arr) == 1 and arr[0]["report"]["attempt"] == 2


def test_session_report_unknown_call_404(env):
    client, _repo = env
    r = client.post(
        "/api/calls/nope/session-report",
        json={"worker": "interp-fwd"},
        headers=MACHINE,
    )
    assert r.status_code == 404


# ---- token 跨账号 call_id 不翻状态 ----


def test_token_cross_account_call_not_activated(env, monkeypatch):
    client, repo = env
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    _mk_user(repo, "ops-1", account="acc-001")
    _mk_user(repo, "ops-2", account="acc-002")
    tok1 = _login(client, "ops-1")
    tok2 = _login(client, "ops-2")
    own_id = _mk_call(
        client, account="acc-001", room="room-acc1", headers=_auth(tok1)
    )
    other_id = _mk_call(
        client, account="acc-002", room="room-acc2", headers=_auth(tok2)
    )

    # acc-001 的 user 借道 acc-002 的 call_id 取 token：激活必须被跳过。
    rt = client.post(
        "/api/token",
        json={"room_name": "room-acc1", "call_id": other_id, "role": "me"},
        headers=_auth(tok1),
    )
    assert rt.status_code in (200, 201), rt.text
    assert repo.get_call(other_id)["status"] != "active"

    # 本人通话照常翻 ACTIVE（同账号语义零变化）。
    rt2 = client.post(
        "/api/token",
        json={"room_name": "room-acc1", "call_id": own_id, "role": "me"},
        headers=_auth(tok1),
    )
    assert rt2.status_code in (200, 201), rt2.text
    assert repo.get_call(own_id)["status"] == "active"
