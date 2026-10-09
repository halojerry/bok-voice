"""W⑥-2/W⑥-3/W⑥-4/W⑥-5（2026-10-09）重放越权矩阵 + 启动闸真值表 + 费用限速 + 密钥卫生。

重放威胁模型：登录后的客户（拿到我们全部请求形状）用自己的 JWT 重放特权/
跨租户请求——每层闸服务端逐请求重估，此处把矩阵写成回归资产（新增端点漏闸
时矩阵不必全绿即红）。厂商字面量响应断言在 W③ 落地后由
test_model_opaque.py 承接（本文件只钉访问矩阵）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "x" * 40)

import pytest
from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import clear_account_expiry_cache, hash_password

PW = "Passw0rd" + "!x"


def _client_and_repo(monkeypatch, auth_on=True):
    from control_plane.main import app
    from control_plane.nodes_store import NodeStore
    from fastapi.testclient import TestClient

    if auth_on:
        monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    else:
        monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    monkeypatch.setattr(
        app.state, "account_lookup",
        lambda account_id: repo.get_account(account_id), raising=False,
    )
    monkeypatch.setattr(app.state, "node_store", NodeStore(None), raising=False)
    # 测试隔离：auth._ACCOUNT_EXPIRY_CACHE 模块级 TTL 缓存跨测试存活——
    # 换仓即清（前一测试把同名账号缓存成 expired 会污染本文件全部 403）。
    clear_account_expiry_cache()
    return TestClient(app), repo


def _mk(repo, username, role, account):
    repo.create_user(
        username=username, password_hash=hash_password(PW),
        role=role, org_id="", account_id=account,
    )


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def three_identities(monkeypatch):
    """user@A / admin@A / admin@B + 各自账号的一件资源。"""
    client, repo = _client_and_repo(monkeypatch)
    _mk(repo, "a-admin", "admin", "acc-a")
    _mk(repo, "a-peon", "user", "acc-a")
    _mk(repo, "b-admin", "admin", "acc-b")
    obj_a = repo.create_object("acc-a", {"display_name": "A 的对象", "phone": "10000000001"})
    obj_b = repo.create_object("acc-b", {"display_name": "B 的对象", "phone": "10000000002"})
    tokens = {
        "user_a": _login(client, "a-peon"),
        "admin_a": _login(client, "a-admin"),
        "admin_b": _login(client, "b-admin"),
    }
    return client, repo, tokens, obj_a, obj_b


def test_cross_tenant_idor_matrix(three_identities):
    client, repo, tk, obj_a, obj_b = three_identities
    # A 侧身份读 B 的对象 → 404（不泄露存在性）；读自己的 → 200
    for key in ("user_a", "admin_a"):
        assert client.get(f"/api/objects/{obj_b['id']}", headers=_auth(tk[key])).status_code == 404
        assert client.get(f"/api/objects/{obj_a['id']}", headers=_auth(tk[key])).status_code == 200
    # B 读 A → 404
    assert client.get(f"/api/objects/{obj_a['id']}", headers=_auth(tk["admin_b"])).status_code == 404
    # 列表强压回本账号：A 的列表永不含 B 的资源
    for key in ("user_a", "admin_a"):
        ids = {o["id"] for o in client.get("/api/objects", headers=_auth(tk[key])).json()}
        assert obj_a["id"] in ids and obj_b["id"] not in ids
    # 建单盖账号章：A 用户以 B 的 account_id 建单 → 落回 acc-a（不落 B）
    r = client.post("/api/calls", headers=_auth(tk["user_a"]), json={
        "account_id": "acc-b", "object_id": "", "mode": "simulation",
    })
    assert r.status_code in (200, 201), r.text
    assert r.json()["account_id"] == "acc-a"


def test_vertical_privilege_matrix(three_identities):
    client, repo, tk, obj_a, _ = three_identities
    # 模型路由族（root+机器专属）对 admin/user 恒 403
    for key in ("user_a", "admin_a", "admin_b"):
        h = _auth(tk[key])
        assert client.get("/api/model-routing", headers=h).status_code == 403
        assert client.put("/api/model-routing", headers=h, json={"lanes": {}}).status_code == 403
        assert client.get("/api/model-routing/detect", headers=h).status_code == 403
        assert client.get("/api/accounts", headers=h).status_code == 403
        assert client.post("/api/accounts", headers=h,
                           json={"admin_username": "x", "admin_password": "x" * 12}).status_code == 403
        assert client.get("/api/ops/disaster-status", headers=h).status_code == 403
    # mass-assignment：admin 建号 role=admin → 403（只能建 user）
    assert client.post("/api/users", headers=_auth(tk["admin_a"]), json={
        "username": "esc", "password": PW, "role": "admin",
    }).status_code == 403
    # PATCH 提角色 → 403
    peon = repo.get_user_by_username("a-peon")
    assert client.patch(f"/api/users/{peon['id']}", headers=_auth(tk["admin_a"]),
                        json={"role": "admin"}).status_code == 403
    # JSON 重复键污染（last-wins）：role 双写解析成 admin → 直接 403 拒绝
    #（若解析器取首值则落 user 也安全；两态都过，别的状态码即事故）。
    r = client.post(
        "/api/users",
        headers={**_auth(tk["admin_a"]), "Content-Type": "application/json"},
        content='{"username":"dup","password":"' + PW + '","role":"user","role":"admin"}',
    )
    assert r.status_code in (200, 201, 400, 403, 409)
    if r.status_code in (200, 201):
        assert r.json()["role"] == "user"


def test_forged_and_garbage_tokens(three_identities):
    client, _, tk, _, _ = three_identities
    for bad in ("garbage.token.here", "e30.e30.e30", "Bearer "):
        assert client.get("/api/objects", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    # 篡改真 token 的任意一段 → 401
    good = tk["admin_a"]
    head, body, sig = good.split(".")
    assert client.get("/api/objects", headers={
        "Authorization": f"Bearer {head}.{body[:-2]}aa.{sig}"}).status_code == 401


def test_machine_channel_not_reachable_from_user_tokens(three_identities):
    client, _, tk, _, _ = three_identities
    monkeypatch_env = os.environ
    # 用户 token 冒充机器通道：CP_TOKEN 未设时它只是普通 JWT；设了也须精确同值。
    os.environ["BOK_CP_TOKEN"] = "cp-secret-token-value"
    try:
        # 用户 JWT ≠ CP token → 走用户闸（200=本账号可见，机器专属面仍 403）
        assert client.get("/api/objects", headers=_auth(tk["admin_a"])).status_code == 200
        assert client.get("/api/model-routing", headers=_auth(tk["admin_a"])).status_code == 403
    finally:
        os.environ.pop("BOK_CP_TOKEN", None)
        monkeypatch_env.pop("BOK_CP_TOKEN", None)


def test_secrets_never_in_responses(three_identities, monkeypatch):
    client, _, tk, _, _ = three_identities
    monkeypatch.setenv("BOK_CP_TOKEN", "cp-canary-secret-do-not-leak")
    monkeypatch.setenv("BOK_JWT_SECRET", "jwt-canary-secret-do-not-leak-" + "0" * 16)
    for path in ("/api/settings", "/api/objects", "/api/calls", "/api/auth/me", "/health"):
        r = client.get(path, headers=_auth(tk["admin_a"]))
        assert "cp-canary-secret-do-not-leak" not in r.text
        assert "jwt-canary-secret-do-not-leak" not in r.text
        assert "password_hash" not in r.text


def test_public_bind_guard_truth_table():
    from control_plane.main import _public_bind_user_auth_missing as f

    # 公网 bind + auth-off（无论 CP token）→ 拒；显式认账 → 放
    assert f("0.0.0.0", auth_on=False) is True
    assert f("0.0.0.0", auth_on=False, insecure_ack="1") is False
    assert f("example.com", auth_on=False, insecure_ack="") is True
    # auth-on → 放（identity_gate 全量把关）
    assert f("0.0.0.0", auth_on=True) is False
    # 回环 bind → 放（本机开发形态）
    assert f("127.0.0.1", auth_on=False) is False
    assert f("::1", auth_on=False) is False
    assert f("localhost", auth_on=False) is False


def test_cost_rate_limit_on_preview(monkeypatch):
    client, _ = _client_and_repo(monkeypatch, auth_on=False)
    codes = []
    for _ in range(32):
        r = client.post("/api/tts/preview", json={"provider": "qwen3_tts", "text": "x", "voice": "v"})
        codes.append(r.status_code)
    assert 429 in codes and codes[-1] == 429
    assert codes[0] != 429  # 首发不限（内部错误码可能是 5xx，但不是 429）
    # kill-switch 关闭后不再限
    monkeypatch.setenv("BOK_COST_RATE_LIMIT", "0")
    r = client.post("/api/tts/preview", json={"provider": "qwen3_tts", "text": "x", "voice": "v"})
    assert r.status_code != 429
