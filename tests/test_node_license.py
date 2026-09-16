"""P1 节点鉴权（license key + 机器指纹 + 吊销）：加固模式注册闸、配额、克隆检出、
静态站 GET 豁免。store 层双模（内存/SQL）+ CP 端点（TestClient）+ 本地模式零变化。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.main import app
from control_plane.nodes_store import LicenseError, NodeStore


@pytest.fixture()
def fresh_store(monkeypatch):
    """每测独立内存 NodeStore + 本地形态（双关全空=开放注册）。"""
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    monkeypatch.delenv("BOK_AUTH_REQUIRED", raising=False)
    with TestClient(app) as client:
        app.state.node_store = NodeStore(None)
        yield client


@pytest.fixture()
def hardened_store(monkeypatch):
    """加固模式（BOK_CP_TOKEN 档）：register 先过机器 token 门、再过 license 闸。"""
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("BOK_CP_TOKEN", "cp-secret-9")
    with TestClient(app) as client:
        app.state.node_store = NodeStore(None)
        yield client


@pytest.fixture()
def auth_on_store(monkeypatch):
    """加固模式（BOK_AUTH_REQUIRED=1 档）：root 种子+JWT——非 root 角色闸测试用。"""
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_JWT_SECRET", "k" * 40)
    monkeypatch.setenv("BOK_ROOT_USERNAME", "root")
    monkeypatch.setenv("BOK_ROOT_PASSWORD", "Root-Bok-Test-1")
    with TestClient(app) as client:
        app.state.node_store = NodeStore(None)
        yield client


# ---- store 层（内存 + SQL 双模）----


def test_license_lifecycle_memory():
    store = NodeStore(None)
    lic = store.create_license(org_id="org-1", max_nodes=1, note="edge")
    assert lic["license_key"].startswith("bokn_")
    assert "key_hash" not in lic  # 永不出 hash

    ok = store.validate_license_for_register(lic["license_key"], "fp-a")
    assert ok["license_id"] == lic["license_id"]

    with pytest.raises(LicenseError) as e:
        store.validate_license_for_register("bokn_wrong", "fp-a")
    assert e.value.status_code == 401

    # 配额：max_nodes=1，不同指纹第二台 → 403；同指纹重注册不吃配额。
    store.register(name="n1", platform="mac-mlx", license_id=lic["license_id"],
                   fingerprint="fp-a")
    store.register(name="n1b", platform="mac-mlx", license_id=lic["license_id"],
                   fingerprint="fp-a")  # 幂等复用
    with pytest.raises(LicenseError) as e:
        store.validate_license_for_register(lic["license_key"], "fp-b")
    assert e.value.status_code == 403

    # 吊销：license 死 → 名下节点心跳 401（license_revoked）。
    _, token = store.register(name="n1", platform="mac-mlx",
                              license_id=lic["license_id"], fingerprint="fp-a")
    out = store.revoke_license(lic["license_id"])
    assert out["nodes_revoked"] >= 1
    ok, reason = store.heartbeat(token, metrics={})
    assert (ok, reason) == (False, "license_revoked")


def test_fingerprint_mismatch_revokes_and_original_can_reregister():
    """克隆检出：token 被别的机器（指纹不符）使用 → 自动吊销；原机指纹重注册
    幂等复用 node_id 换新 token——恢复路径存在（这是「机器码鉴权」的闭环）。"""
    store = NodeStore(None)
    lic = store.create_license(max_nodes=1)
    node_id, token = store.register(name="a", platform="mac-mlx",
                                    license_id=lic["license_id"], fingerprint="fp-real")

    ok, reason = store.heartbeat(token, fingerprint="fp-clone")
    assert (ok, reason) == (False, "fingerprint_mismatch")
    # 节点已吊销：克隆者再用心跳也不通，原 token 同死。
    assert store.heartbeat(token, fingerprint="fp-real")[0] is False

    node_id2, token2 = store.register(name="a", platform="mac-mlx",
                                      license_id=lic["license_id"], fingerprint="fp-real")
    assert node_id2 == node_id  # 幂等复用
    assert store.heartbeat(token2, fingerprint="fp-real") == (True, "")


def test_license_sql_mode_roundtrip(tmp_path):
    from bok_voice_business_db import models
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path}/lic.db", future=True)
    models.create_all(engine)
    store = NodeStore(engine)
    lic = store.create_license(org_id="org-2", max_nodes=2)
    assert store.validate_license_for_register(lic["license_key"], "fp-x")["status"] == "active"
    _, token = store.register(name="sql-node", platform="cuda-win",
                              license_id=lic["license_id"], fingerprint="fp-x")
    assert store.heartbeat(token, fingerprint="fp-x") == (True, "")
    # 重启（新 store 同 engine）后 license/节点/指纹仍在。
    store2 = NodeStore(engine)
    assert store2.heartbeat(token, fingerprint="fp-x") == (True, "")
    assert store2.heartbeat(token, fingerprint="fp-other") == (False, "fingerprint_mismatch")
    assert [r["license_id"] for r in store2.list_licenses()] == [lic["license_id"]]


# ---- CP 端点 ----


def test_local_mode_register_stays_open(fresh_store):
    """本地形态（双关全空）零变化：无 license 也能注册（向后兼容）。"""
    r = fresh_store.post("/api/nodes/register", json={"name": "n", "platform": "mac-mlx"})
    assert r.status_code == 200
    assert r.json()["node_token"]


def test_hardened_register_requires_license(hardened_store):
    cp = {"Authorization": "Bearer cp-secret-9"}
    # 过了机器 token 门但没 license → 401（license 闸）。
    r = hardened_store.post("/api/nodes/register", json={"name": "n"},
                            headers=cp)
    assert r.status_code == 401
    # root 签发 license（机器通道可代 root 面做引导）。
    lic = hardened_store.post("/api/nodes/licenses", json={"max_nodes": 1}, headers=cp)
    assert lic.status_code == 200
    key = lic.json()["license_key"]
    r = hardened_store.post("/api/nodes/register", json={
        "name": "n", "license_key": key, "fingerprint": "fp-a"}, headers=cp)
    assert r.status_code == 200
    token = r.json()["node_token"]
    # 心跳带指纹（豁免 CP token 门）。
    hb = hardened_store.post("/api/nodes/heartbeat", json={"fingerprint": "fp-a"},
                             headers={"Authorization": f"Bearer {token}"})
    assert hb.status_code == 200
    # 配额满 + 异指纹 → 403。
    r2 = hardened_store.post("/api/nodes/register", json={
        "name": "clone", "license_key": key, "fingerprint": "fp-b"}, headers=cp)
    assert r2.status_code == 403
    # 吊销 → 心跳死。
    rev = hardened_store.post(
        f"/api/nodes/licenses/{lic.json()['license_id']}/revoke", json={}, headers=cp)
    assert rev.status_code == 200
    hb2 = hardened_store.post("/api/nodes/heartbeat", json={"fingerprint": "fp-a"},
                              headers={"Authorization": f"Bearer {token}"})
    assert hb2.status_code == 401


def test_license_endpoints_machine_gated(hardened_store):
    """license 管理面=root 专属：加固模式下匿名不可碰、机器通道可代做引导。"""
    cp = {"Authorization": "Bearer cp-secret-9"}
    r = hardened_store.post("/api/nodes/licenses", json={"max_nodes": 1})
    assert r.status_code == 401  # 无凭据 → 中间件 401
    assert hardened_store.get("/api/nodes/licenses").status_code == 401
    ok = hardened_store.post("/api/nodes/licenses", json={"max_nodes": 1}, headers=cp)
    assert ok.status_code == 200
    assert "license_key" in ok.json()


def test_static_get_exempt_under_auth_on(monkeypatch):
    """静态站 GET 豁免：auth-on 下裸 GET / 不得被 401（登录页必须可达）。"""
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("BOK_AUTH_REQUIRED", "1")
    monkeypatch.setenv("BOK_JWT_SECRET", "x" * 40)
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code != 401  # 404（无静态产物）或 200，都不许是 401


# ---- 熔断真实化（site-delivery M1，2026-09-16）：窒息点 / sticky / unrevoke / 401 体 ----


def _register_node(client, *, cp="", license_key="", fingerprint=""):
    body = {"name": "n", "platform": "mac-mlx"}
    if license_key:
        body.update({"license_key": license_key, "fingerprint": fingerprint})
    r = client.post("/api/nodes/register", json=body, headers=cp)
    assert r.status_code == 200, r.text
    return r.json()["node_id"], r.json()["node_token"]


def test_chokepoint_revoked_node_token_403_auth_off(fresh_store):
    """窒息点（auth-off 开放流）：revoked node_token 打 POST /api/calls 与
    /api/token → 403 "node revoked"；在册 node_token / 非节点 Bearer 行为零变化。"""
    node_id, node_token = _register_node(fresh_store)
    # 在册节点建单照常（backward-compat 基线：窒息点只打 revoked）。
    ok_call = fresh_store.post("/api/calls", json={"account_id": "acc-001"},
                               headers={"Authorization": f"Bearer {node_token}"})
    assert ok_call.status_code == 200
    # 非节点 Bearer（随机串）→ 解不出节点 → 直通建单（不得误杀）。
    ok_other = fresh_store.post("/api/calls", json={"account_id": "acc-001"},
                                headers={"Authorization": "Bearer random-user-cred"})
    assert ok_other.status_code == 200
    # root 吊销（auth-off require_role 直通）。
    rev = fresh_store.post(f"/api/nodes/{node_id}/revoke", json={})
    assert rev.status_code == 200 and rev.json()["revoked"] is True
    # 窒息点：两个通话面端点都 403，detail 逐字 "node revoked"。
    c = fresh_store.post("/api/calls", json={"account_id": "acc-001"},
                         headers={"Authorization": f"Bearer {node_token}"})
    assert c.status_code == 403 and c.json() == {"detail": "node revoked"}
    t = fresh_store.post("/api/token", json={"room_name": ok_call.json()["id"]},
                         headers={"Authorization": f"Bearer {node_token}"})
    assert t.status_code == 403 and t.json() == {"detail": "node revoked"}
    # GET 面不在窒息点路径（今日行为：/api/calls GET 不拦 node_token——语义口径
    # 钉死在 POST 通话面）。
    g = fresh_store.get("/api/calls", headers={"Authorization": f"Bearer {node_token}"})
    assert g.status_code == 200


def test_chokepoint_revoked_node_token_403_hardened(hardened_store):
    """窒息点（BOK_CP_TOKEN 加固档）：revoked node_token → 403（不是 401）——
    窒息点中间件必须坐在 CP token/身份门禁**之外**，否则 node_token 会被内层
    门禁先 401、403 语义表达不出来。"""
    cp = {"Authorization": "Bearer cp-secret-9"}
    lic = hardened_store.post("/api/nodes/licenses", json={"max_nodes": 1}, headers=cp)
    node_id, node_token = _register_node(
        hardened_store, cp=cp, license_key=lic.json()["license_key"], fingerprint="fp-a")
    rev = hardened_store.post(f"/api/nodes/{node_id}/revoke", json={}, headers=cp)
    assert rev.status_code == 200
    c = hardened_store.post("/api/calls", json={"account_id": "acc-001"},
                            headers={"Authorization": f"Bearer {node_token}"})
    assert c.status_code == 403 and c.json() == {"detail": "node revoked"}
    t = hardened_store.post("/api/token", json={"room_name": "room-x"},
                            headers={"Authorization": f"Bearer {node_token}"})
    assert t.status_code == 403 and t.json() == {"detail": "node revoked"}


def test_heartbeat_401_detail_shapes_root_vs_auto_clone(hardened_store):
    """⑦ 停栈指令契约：root 吊销心跳 401 detail=机器可执行 dict（action:"shutdown"）；
    auto_clone 吊销保持纯文本（原机重注册复活路径保留，不得逼停整台机器）。"""
    cp = {"Authorization": "Bearer cp-secret-9"}
    lic = hardened_store.post("/api/nodes/licenses", json={"max_nodes": 2}, headers=cp)
    key = lic.json()["license_key"]
    node_a, tok_root = _register_node(hardened_store, cp=cp, license_key=key, fingerprint="fp-ra")
    node_b, tok_b = _register_node(hardened_store, cp=cp, license_key=key, fingerprint="fp-rb")
    # root 吊销 A → 心跳 401 detail 是 dict 且 action=="shutdown"。
    hardened_store.post(f"/api/nodes/{node_a}/revoke", json={}, headers=cp)
    r = hardened_store.post("/api/nodes/heartbeat", json={"fingerprint": "fp-ra"},
                            headers={"Authorization": f"Bearer {tok_root}"})
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["action"] == "shutdown" and detail["reason"] == "node revoked"
    # auto_clone（克隆指纹心跳触发自动吊销 B）→ 随后心跳 401 detail 仍是纯文本。
    r2 = hardened_store.post("/api/nodes/heartbeat", json={"fingerprint": "fp-clone"},
                             headers={"Authorization": f"Bearer {tok_b}"})
    assert r2.status_code == 401 and r2.json()["detail"] == "fingerprint mismatch (clone/relocated?)"
    r3 = hardened_store.post("/api/nodes/heartbeat", json={"fingerprint": "fp-rb"},
                             headers={"Authorization": f"Bearer {tok_b}"})
    assert r3.status_code == 401
    assert r3.json()["detail"] == "node revoked"  # 纯文本，无 action 指令


def test_root_revoke_sticky_reregister_401_and_unrevoke_revives(hardened_store):
    """⑤+⑧ 端点契约：root 吊销后同 license 同指纹重注册 401 "node revoked"
    （加固档 sticky）；root /unrevoke 解除后重注册复活同一 node_id。"""
    cp = {"Authorization": "Bearer cp-secret-9"}
    lic = hardened_store.post("/api/nodes/licenses", json={"max_nodes": 1}, headers=cp)
    key = lic.json()["license_key"]
    node_id, token = _register_node(hardened_store, cp=cp, license_key=key, fingerprint="fp-s")
    assert hardened_store.post(f"/api/nodes/{node_id}/revoke", json={}, headers=cp).status_code == 200
    # sticky：重注册 401，错误体区分 "node revoked"（非 unknown license）。
    rr = hardened_store.post("/api/nodes/register", json={
        "name": "n", "platform": "mac-mlx", "license_key": key,
        "fingerprint": "fp-s"}, headers=cp)
    assert rr.status_code == 401 and rr.json()["detail"] == "node revoked"
    # 解除前旧 token 心跳仍拒。
    hb = hardened_store.post("/api/nodes/heartbeat", json={"fingerprint": "fp-s"},
                             headers={"Authorization": f"Bearer {token}"})
    assert hb.status_code == 401
    # unrevoke → 重注册复活同一 node_id，新 token 心跳 200。
    un = hardened_store.post(f"/api/nodes/{node_id}/unrevoke", json={}, headers=cp)
    assert un.status_code == 200 and un.json() == {"node_id": node_id, "revoked": False}
    rr2 = hardened_store.post("/api/nodes/register", json={
        "name": "n", "platform": "mac-mlx", "license_key": key,
        "fingerprint": "fp-s"}, headers=cp)
    assert rr2.status_code == 200 and rr2.json()["node_id"] == node_id
    hb2 = hardened_store.post("/api/nodes/heartbeat", json={"fingerprint": "fp-s"},
                              headers={"Authorization": f"Bearer {rr2.json()['node_token']}"})
    assert hb2.status_code == 200


def test_unrevoke_404_and_409_live(hardened_store):
    """unrevoke 边界：未知节点 404；在册（未吊销）节点 409 "node not revoked"。"""
    cp = {"Authorization": "Bearer cp-secret-9"}
    assert hardened_store.post("/api/nodes/node-missing/unrevoke", json={}, headers=cp).status_code == 404
    lic = hardened_store.post("/api/nodes/licenses", json={"max_nodes": 1}, headers=cp)
    node_id, _ = _register_node(hardened_store, cp=cp,
                                license_key=lic.json()["license_key"], fingerprint="fp-live")
    live = hardened_store.post(f"/api/nodes/{node_id}/unrevoke", json={}, headers=cp)
    assert live.status_code == 409 and live.json()["detail"] == "node not revoked"


def test_unrevoke_requires_root(auth_on_store):
    """unrevoke 是 root 面：匿名 401（auth-on 门禁）、普通 user 403（角色闸）。"""
    login = auth_on_store.post("/api/auth/login", json={
        "username": "root", "password": "Root-Bok-Test-1"})
    assert login.status_code == 200
    root_jwt = {"Authorization": f"Bearer {login.json()['token']}"}
    mk_user = auth_on_store.post("/api/users", headers=root_jwt, json={
        "username": "op1", "password": "Operator-Pass-1", "role": "user"})
    assert mk_user.status_code in (200, 201), mk_user.text
    user_login = auth_on_store.post("/api/auth/login", json={
        "username": "op1", "password": "Operator-Pass-1"})
    user_jwt = {"Authorization": f"Bearer {user_login.json()['token']}"}
    anon = auth_on_store.post("/api/nodes/node-x/unrevoke", json={})
    assert anon.status_code == 401
    denied = auth_on_store.post("/api/nodes/node-x/unrevoke", json={}, headers=user_jwt)
    assert denied.status_code == 403
    # root 对未知节点 unrevoke → 404（过了角色闸才谈存在性）。
    assert auth_on_store.post("/api/nodes/node-x/unrevoke", json={}, headers=root_jwt).status_code == 404


def test_call_node_id_validation_and_token_bound_refusal(fresh_store, monkeypatch):
    """产品路径：建单显式 node_id（未知 404 / revoked 403 / 在册落库）；
    绑定节点被 root 吊销后，其通话的房 token 签发 403——坐席 JWT 通道（非
    node_token Bearer）同样被熔断；未绑定通话不受影响。"""
    # 注入 LiveKit 凭据（/api/token 的 503 缺凭据闸之后才有绑定节点校验）；
    # monkeypatch 保证测试后还原，不污染同进程其他用例。
    monkeypatch.setattr(app.state, "lk_key", "devkey", raising=False)
    monkeypatch.setattr(app.state, "lk_secret", "devsecret-devsecret-devsecret", raising=False)
    node_id, node_token = _register_node(fresh_store)
    # 未绑定（缺省 ""）行为零变化。
    plain = fresh_store.post("/api/calls", json={"account_id": "acc-001"})
    assert plain.status_code == 200 and plain.json()["node_id"] == ""
    # 显式绑定：在册节点落库；未知节点 404。
    bound = fresh_store.post("/api/calls", json={"account_id": "acc-001", "node_id": node_id})
    assert bound.status_code == 200 and bound.json()["node_id"] == node_id
    missing = fresh_store.post("/api/calls", json={"account_id": "acc-001", "node_id": "node-nope"})
    assert missing.status_code == 404
    # 绑定通话在节点在册时可取房 token。
    tok_ok = fresh_store.post("/api/token", json={"room_name": bound.json()["id"]})
    assert tok_ok.status_code == 201
    # root 吊销 → 绑定节点建单 403、绑定通话取 token 403（无 node_token 参与，
    # 走的是端点内通话绑定校验，不是窒息点中间件）。
    fresh_store.post(f"/api/nodes/{node_id}/revoke", json={})
    refused = fresh_store.post("/api/calls", json={"account_id": "acc-001", "node_id": node_id})
    assert refused.status_code == 403 and refused.json()["detail"] == "node revoked"
    tok_refused = fresh_store.post("/api/token", json={"room_name": bound.json()["id"]})
    assert tok_refused.status_code == 403 and tok_refused.json()["detail"] == "node revoked"
    # 未绑定通话的 token 照常签发（熔断不误伤无节点拓扑）。
    tok_plain = fresh_store.post("/api/token", json={"room_name": plain.json()["id"]})
    assert tok_plain.status_code == 201
