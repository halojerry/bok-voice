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
