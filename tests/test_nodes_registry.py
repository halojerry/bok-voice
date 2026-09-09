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
    assert store.heartbeat(token, metrics={"gpu": 0.4}) is True
    assert store.heartbeat("bad-token", metrics={}) is False


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
