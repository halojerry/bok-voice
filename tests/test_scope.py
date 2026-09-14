"""B2 范围强制：账号收窄 / 越权 404 / 管理面角色闸 / 机器通道直通。

语义：**有身份就按身份收紧**——user=本账号+本人、admin=本账号+管理面（整 org 待
org→account 映射落地）、root=全部；无身份（auth-off 开发形态或 BOK_CP_TOKEN 机器
通道）放行——agent 等内部服务不受角色闸误杀。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-scope")

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
    """root/admin(acc-001)/user(acc-001) 三身份 + 两账号数据，返回 (client, ids, tok)。"""
    client, repo = _client_and_repo(monkeypatch)
    repo.create_user(username="rooty", password_hash=hash_password(PW), role="root", account_id="")
    repo.create_user(username="boss1", password_hash=hash_password(PW), role="admin", account_id="acc-001")
    repo.create_user(username="op1", password_hash=hash_password(PW), role="user", account_id="acc-001")

    def tok(username):
        r = client.post("/api/auth/login", json={"username": username, "password": PW})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    root, admin, user = tok("rooty"), tok("boss1"), tok("op1")
    o1 = client.post("/api/objects?account_id=acc-001", headers=root,
                     json={"display_name": "A1", "phone": "+85211111111"}).json()
    o2 = client.post("/api/objects?account_id=acc-002", headers=root,
                     json={"display_name": "A2", "phone": "+85222222222"}).json()
    c1 = client.post("/api/calls", headers=user,
                     json={"account_id": "acc-001", "object_id": o1["id"]}).json()
    c2 = client.post("/api/calls", headers=root,
                     json={"account_id": "acc-002", "object_id": o2["id"]}).json()
    t1 = client.post("/api/templates", headers=root,
                     json={"account_id": "acc-001", "name": "T1", "language": "zh"}).json()
    t2 = client.post("/api/templates", headers=root,
                     json={"account_id": "acc-002", "name": "T2", "language": "zh"}).json()
    camp1 = client.post("/api/campaigns", headers=root,
                        json={"name": "波一", "object_ids": [o1["id"]], "account_id": "acc-001"}).json()
    qa1 = client.post("/api/qa-entries", headers=root,
                      json={"question_text": "q", "answer_text": "a", "account_id": "acc-001"}).json()
    qa2 = client.post("/api/qa-entries", headers=root,
                      json={"question_text": "q2", "answer_text": "a2", "account_id": "acc-002"}).json()
    ids = {"o1": o1["id"], "o2": o2["id"], "c1": c1["id"], "c2": c2["id"],
           "t1": t1["id"], "t2": t2["id"], "camp1": camp1["id"],
           "qa1": qa1["id"], "qa2": qa2["id"], "root": root, "admin": admin, "user": user}
    return client, repo, ids


def test_user_lists_scoped_and_cross_account_404(monkeypatch):
    client, _repo, ids = _setup(monkeypatch)
    u = ids["user"]

    # 列表只见本账号；显式传别账号也被压回
    assert {o["id"] for o in client.get("/api/objects", headers=u).json()} == {ids["o1"]}
    calls = client.get("/api/calls?account_id=acc-002", headers=u).json()
    assert {c["id"] for c in calls} == {ids["c1"]}
    assert {c["id"] for c in client.get("/api/campaigns", headers=u).json()} == {ids["camp1"]}
    assert {t["id"] for t in client.get("/api/templates", headers=u).json()} == {ids["t1"]}

    # 跨账号 by-ID 一律 404（不泄露存在性）
    for url in (
        f"/api/calls/{ids['c2']}",
        f"/api/calls/{ids['c2']}/turns",
        f"/api/calls/{ids['c2']}/settlement",
        f"/api/calls/{ids['c2']}/metrics",
        f"/api/objects/{ids['o2']}",
        f"/api/templates/{ids['t2']}",
        f"/api/campaigns/{ids['camp1']}" if False else f"/api/calls/{ids['c2']}/hangup",
    ):
        r = client.get(url, headers=u) if not url.endswith("/hangup") else client.post(url, headers=u)
        assert r.status_code == 404, url

    # 跨账号 QA/话术/战役操作同样 404
    assert client.patch(f"/api/qa-entries/{ids['qa2']}", headers=u, json={"answer_text": "x"}).status_code == 404
    assert client.delete(f"/api/qa-entries/{ids['qa2']}", headers=u).status_code == 404
    assert client.delete(f"/api/templates/{ids['t2']}", headers=u).status_code == 404
    assert client.get(f"/api/campaigns/{ids['camp1']}", headers=u).status_code == 200  # 本账号 ✓


def test_user_admin_surfaces_403(monkeypatch):
    client, _repo, ids = _setup(monkeypatch)
    u = ids["user"]
    # 管理面（页面矩阵：话务员不可见）
    for url in ("/api/settings", "/api/knowledge", "/api/personas", "/api/fillers",
                "/api/reports/summary", "/api/reports/calls", "/api/audit",
                "/api/insights", "/api/supervisor/active-calls"):
        assert client.get(url, headers=u).status_code == 403, url
    # 主管操作与旁听
    assert client.post(f"/api/supervisor/{ids['c1']}/pause-agent", headers=u).status_code == 403
    assert client.post(f"/api/supervisor/{ids['c1']}/listen", headers=u).status_code == 403
    # 话务员不得自签 supervisor 房间 token
    r = client.post("/api/token", headers=u,
                    json={"account_id": "acc-001", "call_id": ids["c1"], "role": "supervisor"})
    assert r.status_code == 403
    # 对象只读：写 403
    assert client.post("/api/objects?account_id=acc-001", headers=u,
                       json={"display_name": "X"}).status_code == 403
    assert client.patch(f"/api/objects/{ids['o1']}", headers=u,
                        json={"display_name": "X"}).status_code == 403
    assert client.delete(f"/api/objects/{ids['o1']}", headers=u).status_code == 403
    # 跨账号通话 token 404（本账号 200 由 E2E/工作台常态覆盖）
    assert client.post("/api/token", headers=u,
                       json={"account_id": "acc-002", "call_id": ids["c2"]}).status_code == 404


def test_admin_surface_ok_but_scoped(monkeypatch):
    client, _repo, ids = _setup(monkeypatch)
    a = ids["admin"]
    # 管理面 200
    assert client.get("/api/settings", headers=a).status_code == 200
    assert client.get("/api/knowledge", headers=a).status_code == 200
    assert client.get("/api/personas", headers=a).status_code == 200
    assert client.get("/api/reports/summary", headers=a).status_code == 200
    assert client.get("/api/audit", headers=a).status_code == 200
    assert client.get("/api/supervisor/active-calls", headers=a).status_code == 200
    # 列表仍只见本账号；跨账号资源 404
    assert {o["id"] for o in client.get("/api/objects", headers=a).json()} == {ids["o1"]}
    assert client.get(f"/api/objects/{ids['o2']}", headers=a).status_code == 404
    assert client.post(f"/api/supervisor/{ids['c2']}/pause-agent", headers=a).status_code == 404
    # 节点注册表=root 专属
    assert client.get("/api/nodes", headers=a).status_code == 403


def test_root_cross_account_and_machine_channel(monkeypatch):
    client, _repo, ids = _setup(monkeypatch)
    r = ids["root"]
    # root 跨账号可见可操作
    assert client.get(f"/api/objects/{ids['o2']}", headers=r).status_code == 200
    assert {c["id"] for c in client.get("/api/calls?account_id=acc-002", headers=r).json()} == {ids["c2"]}
    assert client.get("/api/nodes", headers=r).status_code == 200

    # 机器通道（agent worker 语义）：BOK_CP_TOKEN 直通 agent 路径
    monkeypatch.setenv("BOK_CP_TOKEN", "machine-token")
    h = {"Authorization": "Bearer machine-token"}
    turn = client.post(f"/api/calls/{ids['c2']}/turns", headers=h,
                       params={"role": "user", "transcript": "喂？", "speaker": "customer"})
    assert turn.status_code == 200, turn.text
    assert client.get("/api/fillers", headers=h).status_code == 200
    assert client.get("/api/settings?internal=1", headers=h).status_code == 200
    assert client.get("/api/campaigns", headers=h).status_code == 200


def test_create_forces_identity_account(monkeypatch):
    client, repo, ids = _setup(monkeypatch)
    u = ids["user"]
    # 话务员建通话/战役显式传别账号 → 强制落本账号
    c = client.post("/api/calls", headers=u,
                    json={"account_id": "acc-002", "object_id": ids["o1"]}).json()
    assert c["account_id"] == "acc-001"
    camp = client.post("/api/campaigns", headers=u,
                       json={"name": "x", "object_ids": [ids["o1"]], "account_id": "acc-002"}).json()
    assert camp["account_id"] == "acc-001"
    qa = client.post("/api/qa-entries", headers=u,
                     json={"question_text": "q", "answer_text": "a", "account_id": "acc-002"}).json()
    assert qa["account_id"] == "acc-001"
    tpl = client.post("/api/templates", headers=u,
                      json={"account_id": "acc-002", "name": "TT", "language": "zh"}).json()
    assert tpl["account_id"] == "acc-001"
    # 认领：claimed_by 取用户名（不再误落 account 列）
    client.post(f"/api/calls/{ids['c1']}/whatsapp", headers=u, json={"number": "+85233333333"})
    roster = client.get("/api/roster", headers=u).json()
    assert roster and roster[0]["account_id"] == "acc-001"
    claimed = client.post(f"/api/roster/{roster[0]['id']}/claim", headers=u,
                          json={"claimed_by": "acc-001"}).json()
    assert claimed["claimed_by"] == "op1"


def test_auth_off_regression_open(monkeypatch):
    """auth-off（无头）全站原行为——基线零破。"""
    client, _repo, ids = _setup(monkeypatch)
    assert client.get("/api/objects").status_code == 200
    assert client.get("/api/calls").status_code == 200
    # 匿名旧行为：默认 account=acc-001（硬默认，非全量）。
    assert {c["id"] for c in client.get("/api/calls").json()} == {ids["c1"]}
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/supervisor/active-calls").status_code == 200
