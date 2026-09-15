"""B4 页面权限：主管按人配置话务员可见面（目录 / 存储 / 执掌 / 端点矩阵）。

语义：`users.permissions_json` ''=默认集（7 键，报表默认关）；JSON 数组=精确集合
（'[]'=全关）。执掌：root/admin/机器通道（BOK_CP_TOKEN）/无身份（auth-off）恒直通；
user 缺键 403。权限逐请求查库（JWT 只装身份），主管改权限对已签发 token 即时生效。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-permissions")

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password
from control_plane.permissions import (
    DEFAULT_USER_PERMISSIONS,
    GRANTABLE_PERMISSIONS,
    PAGE_PERMISSIONS,
    effective_permissions,
)

PW = "Passw0rd!x"
# 契约报表面（6 端点）+ 洞察：全部归 reports 键。
_REPORT_URLS = (
    "/api/reports/summary",
    "/api/reports/calls",
    "/api/reports/qa-pairs",
    "/api/reports/usage",
    "/api/reports/script-insights",
    "/api/reports/distill-health",
    "/api/insights",
)


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    # 进 startup：knowledge/node_store 等 app.state 就位（repo 仍被 monkeypatch 覆盖）。
    client = TestClient(app).__enter__()
    return client, repo


def _setup(monkeypatch):
    """root + admin(acc-001) + op1(acc-001, 带 display_name) 三身份。"""
    client, repo = _client_and_repo(monkeypatch)
    repo.create_user(username="rooty", password_hash=hash_password(PW), role="root", account_id="")
    boss = repo.create_user(username="boss1", password_hash=hash_password(PW), role="admin", account_id="acc-001")
    op1 = repo.create_user(username="op1", password_hash=hash_password(PW), role="user",
                           account_id="acc-001", display_name="小陈")

    def tok(username):
        r = client.post("/api/auth/login", json={"username": username, "password": PW})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    ids = {"root": tok("rooty"), "admin": tok("boss1"), "op1": tok("op1"),
           "boss_id": boss["id"], "op1_id": op1["id"]}
    return client, repo, ids


def _patch_perms(client, headers, user_id, permissions):
    return client.patch(f"/api/users/{user_id}", headers=headers, json={"permissions": permissions})


# ---- 目录与有效集（纯函数） ----


def test_catalog_and_effective_permissions():
    """目录顺序=契约表；默认集=default=True 的 7 键；无效/缺失输入回默认集。"""
    assert list(PAGE_PERMISSIONS) == [
        "calls", "roster", "campaigns", "objects", "interpret", "templates", "qa", "reports",
    ]
    assert all(meta["grantable"] is True and meta["label"] for meta in PAGE_PERMISSIONS.values())
    assert PAGE_PERMISSIONS["reports"]["default"] is False
    assert DEFAULT_USER_PERMISSIONS == [
        k for k in PAGE_PERMISSIONS if k != "reports"
    ]
    assert GRANTABLE_PERMISSIONS == set(PAGE_PERMISSIONS)

    # admin/root=全部 grantable 键；user=''/非法 JSON → 默认集；'[]' → 全关
    assert effective_permissions("admin", "") == sorted(GRANTABLE_PERMISSIONS)
    assert effective_permissions("root", "[]") == sorted(GRANTABLE_PERMISSIONS)
    assert effective_permissions("user", "") == DEFAULT_USER_PERMISSIONS
    assert effective_permissions("user", "  ") == DEFAULT_USER_PERMISSIONS
    assert effective_permissions("user", "not-json") == DEFAULT_USER_PERMISSIONS
    assert effective_permissions("user", '{"calls": 1}') == DEFAULT_USER_PERMISSIONS
    assert effective_permissions("user", "[]") == []
    # 精确集合按目录序输出；非 grantable/未知键静默丢弃（写入路径已 400）
    assert effective_permissions("user", '["reports","calls"]') == ["calls", "reports"]
    assert effective_permissions("user", '["calls","nodes","qa","calls"]') == ["calls", "qa"]


# ---- 默认集 / 授予 / 校验 ----


def test_default_set_me_and_page_gate(monkeypatch):
    """未配权限的新 user = 默认集：me() 出 7 键（无 reports），templates 200 / reports 403。"""
    client, _repo, ids = _setup(monkeypatch)
    me = client.get("/api/auth/me", headers=ids["op1"]).json()
    assert me["display_name"] == "小陈"
    assert me["permissions"] == DEFAULT_USER_PERMISSIONS
    assert len(me["permissions"]) == 7 and "reports" not in me["permissions"]
    assert client.get("/api/templates", headers=ids["op1"]).status_code == 200
    assert client.get("/api/reports/summary", headers=ids["op1"]).status_code == 403


def test_patch_permissions_exact_set_and_immediate(monkeypatch):
    """PATCH 精确集合：授予 reports、同时收回其余面；旧 token 无需重签即时生效。"""
    client, _repo, ids = _setup(monkeypatch)
    r = _patch_perms(client, ids["admin"], ids["op1_id"], ["calls", "reports"])
    assert r.status_code == 200, r.text
    assert r.json()["permissions"] == ["calls", "reports"]
    # 同一枚（改权限前签发的）token
    me = client.get("/api/auth/me", headers=ids["op1"]).json()
    assert me["permissions"] == ["calls", "reports"]
    assert client.get("/api/reports/summary", headers=ids["op1"]).status_code == 200
    assert client.get("/api/insights", headers=ids["op1"]).status_code == 200
    assert client.get("/api/templates", headers=ids["op1"]).status_code == 403
    # 全关（'[]'）→ 连默认面一起关
    assert _patch_perms(client, ids["admin"], ids["op1_id"], []).status_code == 200
    assert client.get("/api/auth/me", headers=ids["op1"]).json()["permissions"] == []
    assert client.get("/api/templates", headers=ids["op1"]).status_code == 403


def test_permission_write_validation(monkeypatch):
    """写入校验：未知键 400；admin/root 目标 400；建号可带权限；root 也能配。"""
    client, _repo, ids = _setup(monkeypatch)
    # 未知键（含主管专属面）→ 400
    assert _patch_perms(client, ids["admin"], ids["op1_id"], ["calls", "nodes"]).status_code == 400
    assert _patch_perms(client, ids["admin"], ids["op1_id"], ["settings"]).status_code == 400
    # admin 目标带 permissions → 400（root 操作 admin 目标同 400）
    assert _patch_perms(client, ids["root"], ids["boss_id"], ["calls"]).status_code == 400
    # root 可正常配 user
    assert _patch_perms(client, ids["root"], ids["op1_id"], ["qa"]).status_code == 200
    assert client.get("/api/auth/me", headers=ids["op1"]).json()["permissions"] == ["qa"]
    # 建号带权限；建 admin 带权限 → 400；建号未知键 → 400
    created = client.post("/api/users", headers=ids["admin"],
                          json={"username": "op2", "password": PW, "role": "user",
                                "permissions": ["templates", "qa"]})
    assert created.status_code == 200, created.text
    assert created.json()["permissions"] == ["templates", "qa"]
    assert "permissions_json" not in created.json()  # 原始串不外泄
    assert client.post("/api/users", headers=ids["root"],
                       json={"username": "boss2", "password": PW, "role": "admin",
                             "permissions": ["calls"]}).status_code == 400
    assert client.post("/api/users", headers=ids["admin"],
                       json={"username": "op3", "password": PW, "role": "user",
                             "permissions": ["supervisor"]}).status_code == 400
    # 缺省=None 建号 → 默认集
    created2 = client.post("/api/users", headers=ids["admin"],
                           json={"username": "op4", "password": PW, "role": "user"})
    assert created2.json()["permissions"] == DEFAULT_USER_PERMISSIONS
    # 登录响应/用户列表同样带有效集，且 admin 行=全部 grantable
    login_user = client.post("/api/auth/login", json={"username": "op2", "password": PW}).json()["user"]
    assert login_user["permissions"] == ["templates", "qa"]
    rows = client.get("/api/users", headers=ids["admin"]).json()["users"]
    assert set(next(r for r in rows if r["username"] == "boss1")["permissions"]) == GRANTABLE_PERMISSIONS
    assert next(r for r in rows if r["username"] == "op2")["permissions"] == ["templates", "qa"]


# ---- 端点矩阵 ----


def _matrix_fixtures(client, repo, admin_headers, user_headers):
    """建齐各面资源（对象/话术/QA/通话+名册/战役），返回 id 表。

    话术/QA 用 user 身份建（B3 盖章本人）——否则共享条目在「全开」回程仍会因
    B3 共享改动闸 403，掩盖页面闸的观测。
    """
    obj = client.post("/api/objects?account_id=acc-001", headers=admin_headers,
                      json={"display_name": "A1", "phone": "+85211111111"}).json()
    tpl = client.post("/api/templates", headers=user_headers,
                      json={"account_id": "acc-001", "name": "T1", "language": "zh"}).json()
    qa = client.post("/api/qa-entries", headers=user_headers,
                     json={"question_text": "q", "answer_text": "a", "account_id": "acc-001"}).json()
    call = client.post("/api/calls", headers=admin_headers,
                       json={"account_id": "acc-001", "object_id": obj["id"]}).json()
    client.post(f"/api/calls/{call['id']}/whatsapp", headers=admin_headers,
                json={"number": "+85233333333"})
    roster = repo.list_roster("acc-001")
    camp = client.post("/api/campaigns", headers=admin_headers,
                       json={"name": "波一", "object_ids": [obj["id"]], "account_id": "acc-001"}).json()
    return {"obj": obj["id"], "tpl": tpl["id"], "qa": qa["id"], "call": call["id"],
            "roster": roster[0]["id"], "camp": camp["id"]}


def test_endpoint_matrix_revoked_vs_granted(monkeypatch):
    """全关 → 各面 403（hit 不入闸）；全开 → 各面 200（含同传/建单键映射）。"""
    client, repo, ids = _setup(monkeypatch)
    fx = _matrix_fixtures(client, repo, ids["admin"], ids["op1"])
    u = ids["op1"]

    assert _patch_perms(client, ids["admin"], ids["op1_id"], []).status_code == 200

    denied = [
        ("get", "/api/calls", None), ("post", "/api/calls", {"account_id": "acc-001"}),
        ("post", "/api/token", {"account_id": "acc-001", "call_id": fx["call"]}),
        ("get", "/api/roster", None),
        ("post", f"/api/roster/{fx['roster']}/claim", {"claimed_by": "x"}),
        ("post", f"/api/roster/{fx['roster']}/unclaim", None),
        ("post", f"/api/roster/{fx['roster']}/handled", {"handled": True}),
        ("get", "/api/campaigns", None),
        ("post", "/api/campaigns", {"name": "x", "object_ids": [fx["obj"]], "account_id": "acc-001"}),
        ("get", f"/api/campaigns/{fx['camp']}", None),
        ("post", f"/api/campaigns/{fx['camp']}/start", None),
        ("post", f"/api/campaigns/{fx['camp']}/pause", None),
        ("post", f"/api/campaigns/{fx['camp']}/stop", None),
        ("delete", f"/api/campaigns/{fx['camp']}", None),
        ("get", "/api/objects", None), ("get", f"/api/objects/{fx['obj']}", None),
        ("get", f"/api/objects/{fx['obj']}/topics", None),
        ("get", f"/api/objects/{fx['obj']}/digest", None),
        ("get", "/api/templates", None), ("get", f"/api/templates/{fx['tpl']}", None),
        ("post", "/api/templates", {"account_id": "acc-001", "name": "N"}),
        ("put", f"/api/templates/{fx['tpl']}", {"name": "N2"}),
        ("get", f"/api/templates/{fx['tpl']}/revisions", None),
        ("delete", f"/api/templates/{fx['tpl']}", None),
        ("get", "/api/qa-entries", None),
        ("post", "/api/qa-entries", {"question_text": "q", "answer_text": "a"}),
        ("patch", f"/api/qa-entries/{fx['qa']}", {"answer_text": "x"}),
        ("delete", f"/api/qa-entries/{fx['qa']}", None),
    ] + [("get", url, None) for url in _REPORT_URLS]
    for method, url, body in denied:
        r = getattr(client, method)(url, headers=u, json=body) if body is not None \
            else getattr(client, method)(url, headers=u)
        assert r.status_code == 403, (method, url, r.status_code, r.text)

    # hit 端点不入闸（agent 机器通道）——全关的 user 照旧 200
    assert client.post(f"/api/qa-entries/{fx['qa']}/hit", headers=u).status_code == 200
    # 非页面面（管理面）仍按角色 403 不变
    assert client.get("/api/settings", headers=u).status_code == 403
    assert client.get("/api/users", headers=u).status_code == 403

    # 全开（8 键）→ 各面恢复（话术/QA 归 user 本人，B3 共享闸不参与；
    # /api/token 契约状态码是 201，其余 200）
    assert _patch_perms(client, ids["admin"], ids["op1_id"], list(PAGE_PERMISSIONS)).status_code == 200
    for method, url, body in denied:
        r = getattr(client, method)(url, headers=u, json=body) if body is not None \
            else getattr(client, method)(url, headers=u)
        assert r.status_code == (201 if url == "/api/token" else 200), \
            (method, url, r.status_code, r.text)


def test_interpret_key_mapping(monkeypatch):
    """同传建单/token 归 interpret 键：只有 interpret 时 A 线面 403、同传面通。"""
    client, _repo, ids = _setup(monkeypatch)
    u = ids["op1"]
    assert _patch_perms(client, ids["admin"], ids["op1_id"], ["interpret"]).status_code == 200
    # A 线建单/列表被 calls 键挡住
    assert client.post("/api/calls", headers=u, json={"account_id": "acc-001"}).status_code == 403
    assert client.get("/api/calls", headers=u).status_code == 403
    # kind=interpret 建单放行
    call = client.post("/api/calls", headers=u,
                       json={"account_id": "acc-001", "kind": "interpret", "language": "zh",
                             "target_lang": "en"})
    assert call.status_code == 200, call.text
    assert call.json()["kind"] == "interpret"
    # token 按通话 kind 选键：同传 201 / A 线 403
    r = client.post("/api/token", headers=u,
                    json={"account_id": "acc-001", "call_id": call.json()["id"]})
    assert r.status_code == 201, r.text
    a_line = client.post("/api/calls", headers=ids["admin"], json={"account_id": "acc-001"}).json()
    assert client.post("/api/token", headers=u,
                       json={"account_id": "acc-001", "call_id": a_line["id"]}).status_code == 403


def test_revoke_campaigns_blocks_create(monkeypatch):
    """收回 campaigns → 建波 403；admin（管理面）不受影响 200。"""
    client, repo, ids = _setup(monkeypatch)
    obj = client.post("/api/objects?account_id=acc-001", headers=ids["root"],
                      json={"display_name": "A1", "phone": "+85211111111"}).json()
    body = {"name": "x", "object_ids": [obj["id"]], "account_id": "acc-001"}
    assert _patch_perms(client, ids["admin"], ids["op1_id"], ["calls"]).status_code == 200
    assert client.post("/api/campaigns", headers=ids["op1"], json=body).status_code == 403
    assert client.post("/api/campaigns", headers=ids["admin"], json=body).status_code == 200


def test_machine_channel_unaffected(monkeypatch):
    """机器通道（BOK_CP_TOKEN，agent 装配线）恒直通：user 被收回的面照旧 200。"""
    client, _repo, ids = _setup(monkeypatch)
    assert _patch_perms(client, ids["admin"], ids["op1_id"], []).status_code == 200
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    m = {"Authorization": "Bearer machine-token"}
    assert client.get("/api/templates", headers=m, params={"account_id": "acc-001"}).status_code == 200
    assert client.get("/api/qa-entries", headers=m, params={"account_id": "acc-001"}).status_code == 200
    assert client.get("/api/campaigns", headers=m).status_code == 200
    assert client.get("/api/reports/summary", headers=m).status_code == 200


def test_auth_off_regression(monkeypatch):
    """auth-off（无头）零变化：gated 面全开（含原管理面行为不变）。"""
    client, _repo, _ids = _setup(monkeypatch)
    for url in ("/api/calls", "/api/roster", "/api/campaigns", "/api/objects",
                "/api/templates", "/api/qa-entries") + _REPORT_URLS:
        assert client.get(url).status_code == 200, url
    assert client.post("/api/calls", json={"account_id": "acc-001"}).status_code == 200
    assert client.get("/api/settings").status_code == 200


# ---- 存储（双后端 + 迁移） ----


def test_repo_permissions_roundtrip_inmemory(monkeypatch):
    client, repo, ids = _setup(monkeypatch)
    assert repo.get_user(ids["op1_id"])["permissions_json"] == ""
    # 建号即带权限（PATCH 走的是同一存储键）
    r = _patch_perms(client, ids["admin"], ids["op1_id"], ["calls"])
    assert r.status_code == 200
    assert repo.get_user(ids["op1_id"])["permissions_json"] == '["calls"]'


def test_repo_permissions_roundtrip_sql(tmp_path):
    """SQL 后端同语义冒烟（实 sqlite 库，防双后端漂移）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from bok_voice_business_db import models as m
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

    engine = create_engine(f"sqlite:///{tmp_path / 'perm.db'}")
    m.Base.metadata.create_all(engine)
    repo = SqlAlchemyBusinessRepository(sessionmaker(engine)())

    row = repo.create_user(username="u1", password_hash="x", role="user",
                           account_id="acc-001", permissions_json='["qa"]')
    assert row["permissions_json"] == '["qa"]'
    assert repo.get_user(row["id"])["permissions_json"] == '["qa"]'
    assert repo.get_user_by_username("u1")["permissions_json"] == '["qa"]'
    assert repo.list_users("acc-001")[0]["permissions_json"] == '["qa"]'
    # 缺省建号=''（默认集）；update_user 白名单放行 permissions_json
    plain = repo.create_user(username="u2", password_hash="x", role="user", account_id="acc-001")
    assert plain["permissions_json"] == ""
    assert repo.update_user(row["id"], permissions_json="[]")["permissions_json"] == "[]"
    # 未知键仍被白名单忽略（两后端同款）
    assert repo.update_user(row["id"], username="hack")["username"] == "u1"


def test_repo_login_password_hash_roundtrip_both_backends(tmp_path):
    """登录凭据回程双后端一致性（2026-09-15 实机冒烟实证的 B1 漂移防复发）。

    SQL 后端 _user_to_dict 曾漏带 password_hash → SQL 库登录恒 401（auth_login 验
    空串）；B1-B4 全部鉴权测试走 InMemory 替身从未踩到。钉死：两后端
    get_user_by_username 回程都必须带可验的 password_hash——出仓剥凭据在 CP
    侧 _user_public 单点做，repo 层恒为完整内部行。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from bok_voice_business_db import models as m
    from bok_voice_business_db.repository import InMemoryBusinessRepository, SqlAlchemyBusinessRepository
    from control_plane.auth import hash_password, verify_password

    pw_hash = hash_password("Passw0rd!x")

    sql_engine = create_engine(f"sqlite:///{tmp_path / 'login.db'}")
    m.Base.metadata.create_all(sql_engine)
    repos = [
        SqlAlchemyBusinessRepository(sessionmaker(sql_engine)()),
        InMemoryBusinessRepository(),
    ]
    for repo in repos:
        name = type(repo).__name__
        repo.create_user(username="op1", password_hash=pw_hash, role="user", account_id="acc-001")
        row = repo.get_user_by_username("op1")
        assert row, name
        got = str(row.get("password_hash") or "")
        assert got and verify_password("Passw0rd!x", got), f"{name}: 登录凭据回程可验"


def test_migration_adds_users_permissions_column(tmp_path, monkeypatch):
    """存量 users 表无 permissions_json → 启动补列（幂等，B4）。"""
    import sqlite3

    db = tmp_path / "users.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE users (id VARCHAR(64) PRIMARY KEY, org_id VARCHAR(64) DEFAULT '',"
        " account_id VARCHAR(64) DEFAULT '', username VARCHAR(64),"
        " password_hash VARCHAR(255) DEFAULT '', display_name VARCHAR(255) DEFAULT '',"
        " role VARCHAR(16) DEFAULT 'user', status VARCHAR(16) DEFAULT 'active',"
        " created_at DATETIME);"
    )
    conn.close()

    from control_plane.deps import build_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    build_engine()
    build_engine()  # 幂等：二启不报错

    c = sqlite3.connect(db)
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    c.close()
    assert "permissions_json" in cols, cols
