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
    MANAGEMENT_GRANTABLE,
    MANAGEMENT_PERMISSIONS,
    PAGE_PERMISSIONS,
    effective_admin_permissions,
    effective_permissions,
)

PW = "Passw0rd!x"
# 契约报表面（6 端点）：全部归 reports 键。（/api/insights 深测 P3 收管理面，
# GlobalInsight 无账号维度 → require_role admin/root，不归任何页面键。）
_REPORT_URLS = (
    "/api/reports/summary",
    "/api/reports/calls",
    "/api/reports/qa-pairs",
    "/api/reports/usage",
    "/api/reports/script-insights",
    "/api/reports/distill-health",
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

    # root=全部 grantable 键；admin（下发制 2026-09-20）：''=存量全量（页面+管理 14 键），
    # 显式数组=root 裁定集（目录序）；user=''/非法 JSON → 默认集；'[]' → 全关
    assert effective_permissions("root", "[]") == sorted(GRANTABLE_PERMISSIONS)
    assert effective_permissions("admin", "") == list(PAGE_PERMISSIONS) + list(MANAGEMENT_PERMISSIONS)
    assert effective_admin_permissions('["calls","users"]') == ["calls", "users"]
    assert effective_admin_permissions("[]") == []
    assert effective_admin_permissions("not-json") == list(PAGE_PERMISSIONS) + list(MANAGEMENT_PERMISSIONS)
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
    # insights 深测 P3 收管理面：授 reports 也不该看（全平台蒸馏、无账号维度）
    assert client.get("/api/insights", headers=ids["op1"]).status_code == 403
    assert client.get("/api/templates", headers=ids["op1"]).status_code == 403
    # 全关（'[]'）→ 连默认面一起关
    assert _patch_perms(client, ids["admin"], ids["op1_id"], []).status_code == 200
    assert client.get("/api/auth/me", headers=ids["op1"]).json()["permissions"] == []
    assert client.get("/api/templates", headers=ids["op1"]).status_code == 403


def test_permission_write_validation(monkeypatch):
    """写入校验：未知键 400；建号可带权限；root 也能配；下发制下 admin 目标可被
    root 授予页面键+管理键（未知键仍 400），admin 身份建 admin 仍拒。"""
    client, _repo, ids = _setup(monkeypatch)
    # 未知键（含管理面）→ 400（user 目标）
    assert _patch_perms(client, ids["admin"], ids["op1_id"], ["calls", "nodes"]).status_code == 400
    assert _patch_perms(client, ids["admin"], ids["op1_id"], ["settings"]).status_code == 400
    # root 可正常配 user
    assert _patch_perms(client, ids["root"], ids["op1_id"], ["qa"]).status_code == 200
    assert client.get("/api/auth/me", headers=ids["op1"]).json()["permissions"] == ["qa"]
    # 建号带权限；建号未知键 → 400
    created = client.post("/api/users", headers=ids["admin"],
                          json={"username": "op2", "password": PW, "role": "user",
                                "permissions": ["templates", "qa"]})
    assert created.status_code == 200, created.text
    assert created.json()["permissions"] == ["templates", "qa"]
    assert "permissions_json" not in created.json()  # 原始串不外泄
    assert client.post("/api/users", headers=ids["admin"],
                       json={"username": "op3", "password": PW, "role": "user",
                             "permissions": ["supervisor"]}).status_code == 400
    # 下发制 2026-09-20：root 建 admin 可带页面键+管理键（200）；未知键仍 400
    r = client.post("/api/users", headers=ids["root"],
                    json={"username": "boss2", "password": PW, "role": "admin",
                          "account_id": "acc-001", "permissions": ["calls", "users"]})
    assert r.status_code == 200, r.text
    assert set(r.json()["permissions"]) == {"calls", "users"}
    assert client.post("/api/users", headers=ids["root"],
                       json={"username": "boss3", "password": PW, "role": "admin",
                             "account_id": "acc-001", "permissions": ["calls", "nodes"]}).status_code == 400
    # 缺省=None 建号 → 默认集
    created2 = client.post("/api/users", headers=ids["admin"],
                           json={"username": "op4", "password": PW, "role": "user"})
    assert created2.json()["permissions"] == DEFAULT_USER_PERMISSIONS
    # 登录响应/用户列表同样带有效集，且存量 admin 行=页面+管理全量
    login_user = client.post("/api/auth/login", json={"username": "op2", "password": PW}).json()["user"]
    assert login_user["permissions"] == ["templates", "qa"]
    rows = client.get("/api/users", headers=ids["admin"]).json()["users"]
    boss_row = next(r for r in rows if r["username"] == "boss1")
    # 下发制：存量 boss1 ''=全量（页面+管理 14 键）；op2=精确集
    assert set(boss_row["permissions"]) == GRANTABLE_PERMISSIONS | MANAGEMENT_GRANTABLE
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
    assert client.get("/api/insights", headers=u).status_code == 403

    # 全开（8 键）→ 各面恢复（话术/QA 归 user 本人，B3 共享闸不参与；
    # /api/token 契约状态码是 201，其余 200）
    assert _patch_perms(client, ids["admin"], ids["op1_id"], list(PAGE_PERMISSIONS)).status_code == 200
    for method, url, body in denied:
        r = getattr(client, method)(url, headers=u, json=body) if body is not None \
            else getattr(client, method)(url, headers=u)
        assert r.status_code == (201 if url == "/api/token" else 200), \
            (method, url, r.status_code, r.text)
    # insights 是管理面：页面键全开也不恢复（深测 P3）
    assert client.get("/api/insights", headers=u).status_code == 403


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


# ---- 下发制（2026-09-20）：root 逐键下发 admin 管理面 ----


def test_admin_delegation_default_and_legacy(monkeypatch):
    """存量 ''=全量（零变化）；root 新建 admin=默认章（页面默认集+管理键全关）。"""
    client, _repo, ids = _setup(monkeypatch)
    me = client.get("/api/auth/me", headers=ids["admin"]).json()
    assert "settings" in me["permissions"] and "users" in me["permissions"]
    assert client.get("/api/settings", headers=ids["admin"]).status_code == 200
    assert client.get("/api/audit", headers=ids["admin"]).status_code == 200
    # root 建新 admin（不带 permissions）→ 默认章
    r = client.post("/api/users", headers=ids["root"],
                    json={"username": "boss9", "password": PW, "role": "admin",
                          "account_id": "acc-001"})
    assert r.status_code == 200, r.text
    boss9 = r.json()
    assert "settings" not in boss9["permissions"] and "users" not in boss9["permissions"]
    assert "calls" in boss9["permissions"] and "reports" not in boss9["permissions"]
    login = client.post("/api/auth/login", json={"username": "boss9", "password": PW}).json()
    h9 = {"Authorization": f"Bearer {login['token']}"}
    assert client.get("/api/settings", headers=h9).status_code == 403
    assert client.get("/api/users", headers=h9).status_code == 403
    assert client.post("/api/users", headers=h9,
                       json={"username": "x9", "password": PW, "role": "user"}).status_code == 403
    assert client.get("/api/knowledge", headers=h9).status_code == 403
    assert client.get("/api/audit", headers=h9).status_code == 403
    assert client.get("/api/supervisor/active-calls", headers=h9).status_code == 403
    # 运营面按页面默认集照常
    assert client.get("/api/calls", headers=h9).status_code == 200


def test_admin_delegation_grant_and_revoke(monkeypatch):
    """root 逐键下发/收回即时生效（逐请求查库，旧 token 不必重签）。"""
    client, _repo, ids = _setup(monkeypatch)
    client.post("/api/users", headers=ids["root"],
                json={"username": "boss8", "password": PW, "role": "admin",
                      "account_id": "acc-001"})
    boss8_id = next(u["id"] for u in client.get("/api/users", headers=ids["root"]).json()["users"]
                    if u["username"] == "boss8")
    login8 = client.post("/api/auth/login", json={"username": "boss8", "password": PW}).json()
    h8 = {"Authorization": f"Bearer {login8['token']}"}
    assert client.get("/api/settings", headers=h8).status_code == 403
    assert client.patch(f"/api/users/{boss8_id}", headers=ids["root"],
                        json={"permissions": ["calls", "settings", "users"]}).status_code == 200
    assert client.get("/api/settings", headers=h8).status_code == 200
    assert client.get("/api/users", headers=h8).status_code == 200
    # 收回即时生效
    assert client.patch(f"/api/users/{boss8_id}", headers=ids["root"],
                        json={"permissions": ["calls"]}).status_code == 200
    assert client.get("/api/settings", headers=h8).status_code == 403
    assert client.get("/api/users", headers=h8).status_code == 403
    # admin 即使持 users 键也建不了 admin（角色闸）
    assert client.patch(f"/api/users/{boss8_id}", headers=ids["root"],
                        json={"permissions": ["calls", "users"]}).status_code == 200
    assert client.post("/api/users", headers=h8,
                       json={"username": "nx", "password": PW, "role": "admin"}).status_code == 403


def test_admin_users_key_gate_and_containment(monkeypatch):
    """users 键缺 → users 面全 403；授出页面键以自身下发集为上界（授不出没有的）。"""
    client, _repo, ids = _setup(monkeypatch)
    # 页面集不含 reports——制造「admin 自身无 reports 键」的受控场景
    pages = [k for k in PAGE_PERMISSIONS if k != "reports"]
    assert client.patch(f"/api/users/{ids['boss_id']}", headers=ids["root"],
                        json={"permissions": pages}).status_code == 200
    me = client.get("/api/auth/me", headers=ids["admin"]).json()
    assert "users" not in me["permissions"] and "settings" not in me["permissions"]
    assert client.get("/api/users", headers=ids["admin"]).status_code == 403
    assert client.post("/api/users", headers=ids["admin"],
                       json={"username": "op9", "password": PW, "role": "user"}).status_code == 403
    # 包含规则：boss1 无 reports 键 → 授不出 reports
    assert _patch_perms(client, ids["admin"], ids["op1_id"], ["calls", "reports"]).status_code == 400
    assert _patch_perms(client, ids["admin"], ids["op1_id"], ["calls"]).status_code == 200
    # root 补发 reports 后 admin 可授
    assert client.patch(f"/api/users/{ids['boss_id']}", headers=ids["root"],
                        json={"permissions": pages + ["reports"]}).status_code == 200
    assert _patch_perms(client, ids["admin"], ids["op1_id"], ["calls", "reports"]).status_code == 200


def test_users_visibility_triad(monkeypatch):
    """users 三缺陷收口：空账号 admin 403；admin 视角无 root 行；admin 管 admin 403。"""
    client, repo, ids = _setup(monkeypatch)
    # A. 空账号 admin fail-closed（不再当 match-all 全量泄露）
    repo.create_user(username="boss0", password_hash=hash_password(PW), role="admin", account_id="")
    login0 = client.post("/api/auth/login", json={"username": "boss0", "password": PW}).json()
    assert client.get("/api/users", headers={"Authorization": f"Bearer {login0['token']}"}).status_code == 403
    # B. admin 视角列表无 root 行（root 名录只归 root，纵深防御）
    rows = client.get("/api/users", headers=ids["admin"]).json()["users"]
    assert all(r["role"] != "root" for r in rows)
    root_rows = client.get("/api/users", headers=ids["root"]).json()["users"]
    assert any(r["username"] == "rooty" for r in root_rows)
    # C. admin 不能管同账号其他 admin（对齐「仅 root 可管理」徽标）；自身资料保留自助
    repo.create_user(username="boss2", password_hash=hash_password(PW), role="admin", account_id="acc-001")
    boss2_id = repo.get_user_by_username("boss2")["id"]
    assert client.patch(f"/api/users/{boss2_id}", headers=ids["admin"],
                        json={"status": "disabled"}).status_code == 403
    assert client.patch(f"/api/users/{boss2_id}", headers=ids["root"],
                        json={"status": "disabled"}).status_code == 200
    assert client.patch(f"/api/users/{boss2_id}", headers=ids["root"],
                        json={"status": "active"}).status_code == 200
    # 自助面：admin 改自己 display_name 照旧可用（access_gates 契约），但不能自改权限
    assert client.patch(f"/api/users/{ids['boss_id']}", headers=ids["admin"],
                        json={"display_name": "主管一号"}).status_code == 200
    assert client.patch(f"/api/users/{ids['boss_id']}", headers=ids["admin"],
                        json={"permissions": ["calls", "settings"]}).status_code == 403


def test_settings_secret_surface_root_only(monkeypatch):
    """settings?internal=1 明文回源：root/机器通道/auth-off 可读；admin 403；掩码面照常。"""
    client, _repo, ids = _setup(monkeypatch)
    assert client.get("/api/settings?internal=1", headers=ids["admin"]).status_code == 403
    assert client.get("/api/settings?internal=1", headers=ids["root"]).status_code == 200
    assert client.get("/api/settings", headers=ids["admin"]).status_code == 200
    # auth-off（无身份非加固）保持可读——单机形态零变化
    assert client.get("/api/settings?internal=1").status_code == 200
