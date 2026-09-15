"""B3 话务员级资源：owner_user_id 可见性 / 编辑权 / 盖章 / 运行时 owner 维度。

语义：owner_user_id ''=账号共享 / user_id=话务员个人——user 见自己的+共享、只改自己的
（共享改动=admin/root，别人的 404 不泄露存在性）；admin 本账号全部；root 全部；
无身份（auth-off/机器通道）原样不过滤——agent 机器通道显式传 owner_scope=建单人，
''=仅共享（战役等无主通话）。call_sessions.created_by 建单盖章供运行时 QA 收窄。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-owner")

from bok_voice_business_db.repository import InMemoryBusinessRepository
from control_plane.auth import hash_password

PW = "Passw0rd!x"


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    # 进 startup：knowledge/node_store 等 app.state 就位（repo 仍被 monkeypatch 覆盖）。
    client = TestClient(app).__enter__()
    return client, repo


def _setup(monkeypatch):
    """acc-001 双话务员+admin+root；共享/op1/op2 三档话术与 QA。"""
    client, repo = _client_and_repo(monkeypatch)
    root_row = repo.create_user(username="rooty", password_hash=hash_password(PW), role="root", account_id="")
    repo.create_user(username="boss1", password_hash=hash_password(PW), role="admin", account_id="acc-001")
    op1_row = repo.create_user(username="op1", password_hash=hash_password(PW), role="user", account_id="acc-001")
    op2_row = repo.create_user(username="op2", password_hash=hash_password(PW), role="user", account_id="acc-001")

    def tok(username):
        r = client.post("/api/auth/login", json={"username": username, "password": PW})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    root, admin, op1, op2 = tok("rooty"), tok("boss1"), tok("op1"), tok("op2")
    t_shared = client.post("/api/templates", headers=admin,
                           json={"account_id": "acc-001", "name": "共享", "language": "zh"}).json()
    t_op1 = client.post("/api/templates", headers=op1,
                        json={"account_id": "acc-001", "name": "OP1私", "language": "zh"}).json()
    t_op2 = client.post("/api/templates", headers=op2,
                        json={"account_id": "acc-001", "name": "OP2私", "language": "zh"}).json()
    qa_shared = client.post("/api/qa-entries", headers=admin,
                            json={"question_text": "qs", "answer_text": "as", "account_id": "acc-001"}).json()
    qa_op1 = client.post("/api/qa-entries", headers=op1,
                         json={"question_text": "q1", "answer_text": "a1", "account_id": "acc-001"}).json()
    qa_op2 = client.post("/api/qa-entries", headers=op2,
                         json={"question_text": "q2", "answer_text": "a2", "account_id": "acc-001"}).json()
    ids = {
        "root_id": root_row["id"], "op1_id": op1_row["id"], "op2_id": op2_row["id"],
        "root": root, "admin": admin, "op1": op1, "op2": op2,
        "t_shared": t_shared["id"], "t_op1": t_op1["id"], "t_op2": t_op2["id"],
        "qa_shared": qa_shared["id"], "qa_op1": qa_op1["id"], "qa_op2": qa_op2["id"],
    }
    return client, repo, ids


def test_user_sees_own_plus_shared(monkeypatch):
    client, _repo, ids = _setup(monkeypatch)
    op1, op2, admin, root = ids["op1"], ids["op2"], ids["admin"], ids["root"]

    # user 列表=自己的+共享；显式传别人的 owner_scope 被压回本人
    assert {t["id"] for t in client.get("/api/templates", headers=op1).json()} == {ids["t_shared"], ids["t_op1"]}
    assert {t["id"] for t in client.get(f"/api/templates?owner_scope={ids['op2_id']}", headers=op1).json()} == {ids["t_shared"], ids["t_op1"]}
    assert {t["id"] for t in client.get("/api/templates", headers=op2).json()} == {ids["t_shared"], ids["t_op2"]}
    assert {q["id"] for q in client.get("/api/qa-entries", headers=op1).json()} == {ids["qa_shared"], ids["qa_op1"]}
    # admin 本账号全部；root 指定账号全部
    assert {t["id"] for t in client.get("/api/templates", headers=admin).json()} == {ids["t_shared"], ids["t_op1"], ids["t_op2"]}
    assert {q["id"] for q in client.get("/api/qa-entries", headers=admin).json()} == {ids["qa_shared"], ids["qa_op1"], ids["qa_op2"]}
    assert {t["id"] for t in client.get("/api/templates?account_id=acc-001", headers=root).json()} == {ids["t_shared"], ids["t_op1"], ids["t_op2"]}


def test_by_id_owner_guards(monkeypatch):
    client, _repo, ids = _setup(monkeypatch)
    op1, admin = ids["op1"], ids["admin"]

    # 别人的条目 → 404（读/改/删/版本，不泄露存在性）
    assert client.get(f"/api/templates/{ids['t_op2']}", headers=op1).status_code == 404
    assert client.get(f"/api/templates/{ids['t_op2']}/revisions", headers=op1).status_code == 404
    assert client.put(f"/api/templates/{ids['t_op2']}", headers=op1, json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/templates/{ids['t_op2']}", headers=op1).status_code == 404
    assert client.patch(f"/api/qa-entries/{ids['qa_op2']}", headers=op1, json={"answer_text": "x"}).status_code == 404
    assert client.delete(f"/api/qa-entries/{ids['qa_op2']}", headers=op1).status_code == 404

    # 共享条目可读可引用，改动 403——共享基线只归 admin/root
    assert client.get(f"/api/templates/{ids['t_shared']}", headers=op1).status_code == 200
    assert client.put(f"/api/templates/{ids['t_shared']}", headers=op1, json={"name": "x"}).status_code == 403
    assert client.patch(f"/api/qa-entries/{ids['qa_shared']}", headers=op1, json={"answer_text": "x"}).status_code == 403
    assert client.delete(f"/api/qa-entries/{ids['qa_shared']}", headers=op1).status_code == 403

    # 自己的可改
    assert client.put(f"/api/templates/{ids['t_op1']}", headers=op1, json={"name": "改名"}).json()["name"] == "改名"
    assert client.patch(f"/api/qa-entries/{ids['qa_op1']}", headers=op1, json={"answer_text": "新答"}).json()["answer_text"] == "新答"

    # admin 全可改 + 所有权转移（把 op1 的个人条目转共享）
    assert client.put(f"/api/templates/{ids['t_shared']}", headers=admin, json={"name": "基线"}).status_code == 200
    row = client.patch(f"/api/qa-entries/{ids['qa_op1']}", headers=admin, json={"owner_user_id": ""}).json()
    assert row["owner_user_id"] == ""


def test_create_stamps_and_freeze(monkeypatch):
    client, _repo, ids = _setup(monkeypatch)
    op1, admin = ids["op1"], ids["admin"]

    # user 建=自动归自己（body 指定别人无效）
    t = client.post("/api/templates", headers=op1,
                    json={"account_id": "acc-001", "name": "N", "owner_user_id": ids["op2_id"]}).json()
    assert t["owner_user_id"] == ids["op1_id"]
    qa = client.post("/api/qa-entries", headers=op1,
                     json={"question_text": "q", "answer_text": "a", "owner_user_id": ids["op2_id"]}).json()
    assert qa["owner_user_id"] == ids["op1_id"]

    # admin 建默认共享、可显式指派（给话务员派发个人资源）
    assert client.post("/api/templates", headers=admin, json={"account_id": "acc-001", "name": "A"}).json()["owner_user_id"] == ""
    assert client.post("/api/templates", headers=admin,
                       json={"account_id": "acc-001", "name": "B", "owner_user_id": ids["op2_id"]}).json()["owner_user_id"] == ids["op2_id"]

    # 堵洞：user PUT 带 account_id/owner_user_id 均被剥（账号冻结、所有权不可转移）
    row = client.put(f"/api/templates/{t['id']}", headers=op1,
                     json={"name": "N2", "account_id": "acc-002", "owner_user_id": ""}).json()
    assert row["account_id"] == "acc-001" and row["owner_user_id"] == ids["op1_id"]
    qa_row = client.patch(f"/api/qa-entries/{qa['id']}", headers=op1, json={"owner_user_id": ""}).json()
    assert qa_row["owner_user_id"] == ids["op1_id"]


def test_repo_owner_scope_tri_state(monkeypatch):
    """owner_scope 三态：None=不滤 / ''=仅共享 / uid=共享+本人（模板与 QA 同语义）。"""
    client, repo, ids = _setup(monkeypatch)
    assert {t["id"] for t in repo.list_templates("acc-001")} == {ids["t_shared"], ids["t_op1"], ids["t_op2"]}
    assert {t["id"] for t in repo.list_templates("acc-001", owner_scope="")} == {ids["t_shared"]}
    assert {t["id"] for t in repo.list_templates("acc-001", owner_scope=ids["op1_id"])} == {ids["t_shared"], ids["t_op1"]}
    assert {q["id"] for q in repo.list_qa_entries("acc-001")} == {ids["qa_shared"], ids["qa_op1"], ids["qa_op2"]}
    assert {q["id"] for q in repo.list_qa_entries("acc-001", owner_scope="")} == {ids["qa_shared"]}
    assert {q["id"] for q in repo.list_qa_entries("acc-001", owner_scope=ids["op2_id"])} == {ids["qa_shared"], ids["qa_op2"]}
    # enabled 组合仍成立
    repo.update_qa_entry(ids["qa_shared"], {"enabled": False})
    assert {q["id"] for q in repo.list_qa_entries("acc-001", enabled=True, owner_scope="")} == set()


def test_sql_repo_owner_scope_and_created_by(tmp_path):
    """SQL 后端同语义冒烟（实 sqlite 库，防双后端漂移）+ 建单盖章回程。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from bok_voice_business_db import models as m
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository
    from bok_voice_core.policies import select_session_manifest
    from bok_voice_core.types import CallMode

    engine = create_engine(f"sqlite:///{tmp_path / 'owner.db'}")
    m.Base.metadata.create_all(engine)
    repo = SqlAlchemyBusinessRepository(sessionmaker(engine)())

    repo.create_template({"id": "ts", "account_id": "acc-001", "name": "S"})
    repo.create_template({"id": "t1", "account_id": "acc-001", "name": "1", "owner_user_id": "u1"})
    repo.create_template({"id": "t2", "account_id": "acc-001", "name": "2", "owner_user_id": "u2"})
    assert {t["id"] for t in repo.list_templates("acc-001")} == {"ts", "t1", "t2"}
    assert {t["id"] for t in repo.list_templates("acc-001", owner_scope="")} == {"ts"}
    assert {t["id"] for t in repo.list_templates("acc-001", owner_scope="u1")} == {"ts", "t1"}
    assert repo.get_template("t1")["owner_user_id"] == "u1"
    # PATCH 所有权转移落库
    assert repo.update_qa_entry(repo.create_qa_entry({"question_text": "q", "answer_text": "a"})["id"],
                                {"owner_user_id": "u9"})["owner_user_id"] == "u9"

    manifest = select_session_manifest(
        session_id="call-x", account_id="acc-001", object_id="", persona_id="",
        mode=CallMode.LIVE, created_by="u1",
    )
    call = repo.create_call(manifest)
    assert call["created_by"] == "u1"


def test_machine_channel_and_call_stamping(monkeypatch):
    client, _repo, ids = _setup(monkeypatch)
    # 建单盖章：op1 建通话 → created_by=user_id；匿名建（campaign 同路径）→ ''
    c1 = client.post("/api/calls", headers=ids["op1"], json={"account_id": "acc-001"}).json()
    assert c1["created_by"] == ids["op1_id"]
    c2 = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    assert c2["created_by"] == ""

    # 机器通道（agent 装配线语义）：显式 owner_scope 三态全放行
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    h = {"Authorization": "Bearer machine-token"}
    qa = client.get("/api/qa-entries", headers=h,
                    params={"account_id": "acc-001", "owner_scope": ids["op1_id"]}).json()
    assert {r["id"] for r in qa} == {ids["qa_shared"], ids["qa_op1"]}
    qa = client.get("/api/qa-entries", headers=h, params={"account_id": "acc-001", "owner_scope": ""}).json()
    assert {r["id"] for r in qa} == {ids["qa_shared"]}
    qa = client.get("/api/qa-entries", headers=h, params={"account_id": "acc-001"}).json()
    assert {r["id"] for r in qa} == {ids["qa_shared"], ids["qa_op1"], ids["qa_op2"]}


def test_auth_off_owner_regression(monkeypatch):
    """auth-off（无头）owner 维度零变化：列表不过滤、body owner 原样收（机器信任级）。"""
    client, _repo, ids = _setup(monkeypatch)
    assert {t["id"] for t in client.get("/api/templates").json()} == {ids["t_shared"], ids["t_op1"], ids["t_op2"]}
    assert len(client.get("/api/qa-entries").json()) == 3
    t = client.post("/api/templates", json={"account_id": "acc-001", "name": "M", "owner_user_id": "op9"}).json()
    assert t["owner_user_id"] == "op9"


def test_migration_adds_owner_columns(tmp_path, monkeypatch):
    """存量库三表无 owner_user_id/created_by 列 → 启动补列（幂等，B3）。"""
    import sqlite3

    db = tmp_path / "owner.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE conversation_templates (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) DEFAULT '', hotwords TEXT DEFAULT '');"
        "CREATE TABLE qa_entries (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) DEFAULT '', question_text TEXT);"
        "CREATE TABLE call_sessions (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) DEFAULT '', template_id VARCHAR(64) DEFAULT '');"
    )
    conn.close()

    from control_plane.deps import build_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    build_engine()
    build_engine()  # 幂等：二启不报错

    c = sqlite3.connect(db)
    cols = {t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
            for t in ("conversation_templates", "qa_entries", "call_sessions")}
    c.close()
    assert "owner_user_id" in cols["conversation_templates"]
    assert "owner_user_id" in cols["qa_entries"]
    assert "created_by" in cols["call_sessions"]
