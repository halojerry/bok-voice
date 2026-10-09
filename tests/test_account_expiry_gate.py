"""W② 账号订阅到期执法（2026-10-09）：identity_gate 闸 + me 字段 + 续费恢复。

契约（严格档）：
- 到期账号：登录 200（看得见续费提示）、/api/auth/me 200 带
  ``account_expired=true``；其余全部 /api 403 + ``X-Bok-Code: account_expired``
  （偷来的有效 JWT 同拦——闸按账号状态逐请求重估）。
- renew 后立刻恢复（清 TTL 缓存）；root/机器通道/auth-off 不进闸。
- 在途判例：token 端点也 403（自然收线）；/health 照常。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "x" * 40)

import datetime

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import clear_account_expiry_cache, hash_password

PW = "Passw0rd" + "!x"


def _client_and_repo(monkeypatch):
    from control_plane.main import app
    from control_plane.nodes_store import NodeStore
    from fastapi.testclient import TestClient

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    # 裸 TestClient 不跑 lifespan——手动接线 startup 同款 state：
    # ① account_lookup（identity_gate 经此查账号到期）；
    # ② node_store（最外层 node_revoked_gate 对 POST /api/calls、/api/token
    #    解 Bearer 时要读，缺失即 AttributeError——内存双模形态）。
    monkeypatch.setattr(
        app.state, "account_lookup",
        lambda account_id: repo.get_account(account_id), raising=False,
    )
    monkeypatch.setattr(app.state, "node_store", NodeStore(None), raising=False)
    return TestClient(app), repo


def _mk_user(repo, username, role="user", account="acc-001", password=PW):
    return repo.create_user(
        username=username, password_hash=hash_password(password),
        role=role, org_id="org-t", account_id=account,
    )


def _login(client, username, password=PW):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _expire(repo, account_id, clear=True):
    repo.update_account(
        account_id,
        expires_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1),
    )
    if clear:
        clear_account_expiry_cache(account_id)


def _mk_account(repo, account_id):
    repo.create_account(account_id=account_id, display_name=account_id)


def test_expired_account_full_gate(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    _mk_account(repo, "acc-a")
    _mk_user(repo, "a-admin", role="admin", account="acc-a")
    _mk_user(repo, "a-peon", role="user", account="acc-a")
    at = _login(client, "a-admin")
    ut = _login(client, "a-peon")

    # 未到期：业务面正常
    assert client.get("/api/objects", headers=_auth(at)).status_code == 200

    _expire(repo, "acc-a")
    # 登录照常（豁免路径）——到期客户要能进来看续费提示
    assert client.post("/api/auth/login", json={"username": "a-admin", "password": PW}).status_code == 200
    # me 照常 + 到期标志（整站续费页数据源）
    me = client.get("/api/auth/me", headers=_auth(at))
    assert me.status_code == 200
    body = me.json()
    assert body["account_expired"] is True and body["account_expires_at"]
    me_u = client.get("/api/auth/me", headers=_auth(ut)).json()
    assert me_u["account_expired"] is True
    # 业务面全 403 + 识别头（admin 与 user 同拦；偷来的有效 JWT 无差别）
    for token in (at, ut):
        r = client.get("/api/objects", headers=_auth(token))
        assert r.status_code == 403, r.text
        assert r.headers.get("X-Bok-Code") == "account_expired"
        assert client.get("/api/calls", headers=_auth(token)).status_code == 403
        assert client.post("/api/calls", headers=_auth(token),
                           json={"account_id": "acc-a", "mode": "simulation"}).status_code == 403
        assert client.post("/api/token", headers=_auth(token),
                           json={"call_id": "nope"}).status_code == 403
    # /health 与静态豁免照常
    assert client.get("/health").status_code == 200


def test_renew_restores_immediately(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    _mk_account(repo, "acc-b")
    _mk_user(repo, "b-admin", role="admin", account="acc-b")
    at = _login(client, "b-admin")
    _expire(repo, "acc-b")
    assert client.get("/api/objects", headers=_auth(at)).status_code == 403
    # root 登录续费（root 不受闸）
    _mk_user(repo, "rooter", role="root", account="")
    rt = _login(client, "rooter")
    r = client.post("/api/accounts/acc-b/renew", headers=_auth(rt), json={"days": 30})
    assert r.status_code == 200, r.text
    # 立刻恢复（无 60s 缓存滞后）
    assert client.get("/api/objects", headers=_auth(at)).status_code == 200
    assert client.get("/api/auth/me", headers=_auth(at)).json()["account_expired"] is False


def test_root_and_machine_and_auth_off_unaffected(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    # root 挂在一个「已到期」账号上：root 不进闸（平台方与客户租期无关）
    _mk_account(repo, "acc-c")
    _mk_user(repo, "c-root", role="root", account="acc-c")
    _expire(repo, "acc-c")
    rt = _login(client, "c-root")
    assert client.get("/api/objects", headers=_auth(rt)).status_code == 200
    assert client.get("/api/auth/me", headers=_auth(rt)).json()["account_expired"] is False

    # 机器通道：agent worker 不进闸（同账号到期也不拦内部服务）
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    assert client.get("/api/objects", headers={"Authorization": "Bearer machine-token"}).status_code == 200

    # auth-off（开发形态）：闸随 identity_gate 短路，零行为变化。先清 CP_TOKEN
    # ——否则进入 CP-token-only 模式（BOK_CP_TOKEN 设而 AUTH 未设），匿名 401
    # 是该模式的正确行为，与到期闸无关。
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    assert client.get("/api/objects").status_code == 200


def test_permanent_and_missing_account_no_gate(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    # 无 Account 行（存量/开发）= 零执法
    _mk_user(repo, "legacy", role="admin", account="acc-legacy")
    lt = _login(client, "legacy")
    assert client.get("/api/objects", headers=_auth(lt)).status_code == 200
    # expires_at=None（永久）= 零执法
    _mk_account(repo, "acc-perm")
    _mk_user(repo, "perm-admin", role="admin", account="acc-perm")
    pt = _login(client, "perm-admin")
    assert client.get("/api/objects", headers=_auth(pt)).status_code == 200
    assert client.get("/api/auth/me", headers=_auth(pt)).json()["account_expires_at"] == ""
