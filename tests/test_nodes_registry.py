"""节点注册/心跳 P0 最小版（spec §4.2 L1 前置）：token 只存哈希、心跳刷新
last_seen、离线判定=3× 心跳窗口、engine=None 时退化为内存存储（测试/dev 栈）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from control_plane.nodes_store import NodeStore, effective_status


def test_register_returns_plaintext_token_once_and_stores_hash():
    store = NodeStore(None)
    node_id, token = store.register(name="n1", platform="cuda-win", org_id="org-1")
    assert node_id and token
    assert store._rows[node_id]["token_hash"] != token  # 落库的是哈希
    assert len(store._rows[node_id]["token_hash"]) == 64  # sha256 hex


def test_heartbeat_authenticates_by_token():
    store = NodeStore(None)
    node_id, token = store.register(name="n1", platform="cuda-win", org_id="")
    assert store.heartbeat(token, metrics={"gpu": 0.4}) == (True, "")
    assert store.heartbeat("bad-token", metrics={})[0] is False


def test_effective_status_offline_after_window():
    now = datetime.now(timezone.utc)
    assert effective_status(None, now) == "offline"
    assert effective_status(now - timedelta(seconds=120), now) == "online"
    assert effective_status(now - timedelta(seconds=181), now) == "offline"


def test_list_nodes_shape():
    store = NodeStore(None)
    node_id, token = store.register(name="edge-1", platform="mac-mlx", org_id="org-1")
    store.heartbeat(token, metrics={})
    rows = store.list_nodes()
    assert rows[0]["node_id"] == node_id
    assert rows[0]["status"] == "online"
    assert "token_hash" not in rows[0]  # 永不出参


def test_sql_mode_list_nodes_no_typeerror_and_iso_shape(tmp_path):
    """SQL 分支回归：nodes.last_seen_at 是 naive DateTime 列（SQLite/Postgres 静默丢
    tz），归一 UTC 后 list_nodes 不得 TypeError，且 isoformat 与内存模式同形（带
    +00:00 偏移）。"""
    from bok_voice_business_db import models
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path}/nodes.db", future=True)
    models.create_all(engine)
    store = NodeStore(engine)
    node_id, token = store.register(name="edge-sql", platform="cuda-win", org_id="org-1")
    assert store.heartbeat(token, metrics={"gpu": 0.5}) == (True, "")
    rows = store.list_nodes()
    assert rows[0]["node_id"] == node_id
    assert rows[0]["status"] == "online"
    assert isinstance(rows[0]["last_seen_at"], str)
    assert rows[0]["last_seen_at"].endswith("+00:00")  # 双模 isoformat 同形
    assert "token_hash" not in rows[0]


def test_node_heartbeat_bypasses_cp_token_gate_register_does_not(monkeypatch):
    """BOK_CP_TOKEN 门禁豁免：heartbeat 用 node_token 自鉴权、register 用 license
    key 自证（P1 起两者都在端点内自鉴权，中间件不预拦——无凭据请求仍 401：
    register 裸注册被 license 闸拒）；list 属管理操作仍受门禁。"""
    from fastapi.testclient import TestClient

    from control_plane.main import app

    monkeypatch.setenv("DATABASE_URL", "")  # 内存 NodeStore，确定性
    monkeypatch.setenv("BOK_CP_TOKEN", "cp-secret-1")
    with TestClient(app) as client:
        reg = client.post("/api/nodes/register", json={"name": "n", "platform": "cuda-win"})
        assert reg.status_code == 401  # 无 CP token → 管理操作被门禁拒
        # P1 起加固模式 register 还要 license（第二因子）：先经机器通道签发。
        cp = {"Authorization": "Bearer cp-secret-1"}
        lic = client.post("/api/nodes/licenses", json={"max_nodes": 1}, headers=cp).json()
        reg_ok = client.post(
            "/api/nodes/register",
            json={"name": "n", "platform": "cuda-win",
                  "license_key": lic["license_key"], "fingerprint": "fp-z"},
            headers=cp,
        )
        assert reg_ok.status_code == 200
        hb = client.post(
            "/api/nodes/heartbeat",
            # P2-7 起心跳指纹协议强制：注册绑定了指纹的节点心跳必须带同指纹
            #（缺=按 fingerprint_mismatch 自动吊销）。
            json={"fingerprint": "fp-z"},
            headers={"Authorization": f"Bearer {reg_ok.json()['node_token']}"},
        )
        assert hb.status_code == 200  # node_token 直达心跳，不被 CP 门禁拦
        assert hb.json() == {"ok": True, "commands": []}
