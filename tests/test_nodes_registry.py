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


def test_commands_channel_enqueue_dispatch_convergence(monkeypatch):
    """P3 commands 全链（内存 store + auth-off root 直通）：入队 → 心跳领走（不重发）
    → 心跳 version 收敛自动关单 → nodes.version 写回。"""
    from fastapi.testclient import TestClient

    from control_plane.main import app

    monkeypatch.setenv("DATABASE_URL", "")  # 内存 store/repo，确定性
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    with TestClient(app) as client:
        reg = client.post("/api/nodes/register", json={"name": "n", "platform": "cuda-win"})
        node_id, token = reg.json()["node_id"], reg.json()["node_token"]
        hb_headers = {"Authorization": f"Bearer {token}"}
        # 白名单外动作 / update 缺 version → 400
        assert client.post(f"/api/nodes/{node_id}/commands",
                           json={"action": "rm -rf /"}).status_code == 400
        assert client.post(f"/api/nodes/{node_id}/commands",
                           json={"action": "update"}).status_code == 400
        r = client.post(f"/api/nodes/{node_id}/commands",
                        json={"action": "update", "version": "v0.3.0"})
        assert r.status_code == 200 and r.json()["status"] == "pending"
        cmd_id = r.json()["id"]
        # 心跳领走；下一跳不重发（delivered）
        hb = client.post("/api/nodes/heartbeat", json={}, headers=hb_headers)
        assert [c["id"] for c in hb.json()["commands"]] == [cmd_id]
        hb2 = client.post("/api/nodes/heartbeat", json={}, headers=hb_headers)
        assert hb2.json()["commands"] == []
        # version 收敛 → done；nodes.version 写回
        client.post("/api/nodes/heartbeat", json={"version": "v0.3.0"}, headers=hb_headers)
        ledger = client.get(f"/api/nodes/{node_id}/commands").json()["commands"]
        assert ledger[0]["status"] == "done" and ledger[0]["target_version"] == "v0.3.0"
        rows = client.get("/api/nodes").json()
        assert rows[0]["version"] == "v0.3.0"


def test_command_inflight_call_guard(monkeypatch):
    """无 force 时节点有在途通话 → 409 拒发；force=True 越过（强更是显式决定）。"""
    import control_plane.main as cp_main
    from fastapi.testclient import TestClient

    from control_plane.main import app

    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    with TestClient(app) as client:
        reg = client.post("/api/nodes/register", json={"name": "n", "platform": "cuda-win"})
        node_id = reg.json()["node_id"]
        monkeypatch.setattr(cp_main, "_node_active_calls",
                            lambda nid: [{"id": "call-1", "status": "active"}])
        blocked = client.post(f"/api/nodes/{node_id}/commands",
                              json={"action": "update", "version": "v0.3.0"})
        assert blocked.status_code == 409
        forced = client.post(f"/api/nodes/{node_id}/commands",
                             json={"action": "update", "version": "v0.3.0", "force": True})
        assert forced.status_code == 200
        # shutdown 不做在途检查（熔断语义高于通话）
        shutdown = client.post(f"/api/nodes/{node_id}/commands",
                               json={"action": "shutdown"})
        assert shutdown.status_code == 200


def test_commands_channel_sql_mode(tmp_path):
    """SQL 分支：NodeCommand 落库、delivered 不重发、version 收敛关单双模同形。"""
    from bok_voice_business_db import models
    from sqlalchemy import create_engine

    from control_plane.nodes_store import NodeStore

    engine = create_engine(f"sqlite:///{tmp_path}/nodes.db", future=True)
    models.create_all(engine)
    store = NodeStore(engine)
    node_id, token = store.register(name="n", platform="cuda-win", org_id="")
    cmd = store.enqueue_command(node_id, "update", args={"version": "v9"}, created_by="root")
    assert cmd["status"] == "pending"
    try:
        store.enqueue_command(node_id, "arbitrary")
    except ValueError:
        pass
    else:
        raise AssertionError("whitelist must reject unknown actions")
    got = store.pop_commands(node_id)
    assert [c["id"] for c in got] == [cmd["id"]]
    assert store.pop_commands(node_id) == []  # delivered 不重发
    ok, _ = store.heartbeat(token, metrics={}, version="v9")
    assert ok is True
    ledger = store.list_commands(node_id)
    assert ledger[0]["status"] == "done"
    rows = store.list_nodes()
    assert rows[0]["version"] == "v9"


def test_node_artifact_download_auth_and_traversal(monkeypatch, tmp_path):
    """工件下载：node_token/license 自证、路径段白名单（穿越/编码绕行 404）、
    auth-on 加固态同样可达（端点内自证 + 中间件前缀豁免）。"""
    import hashlib

    from fastapi.testclient import TestClient

    import control_plane.main as cp_main
    from control_plane.main import app

    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("BOK_CP_TOKEN", "cp-secret-1")  # 加固态：验证前缀豁免链路
    art = tmp_path / "downloads"
    pkg_dir = art / "pkg" / "v0.3.0"
    pkg_dir.mkdir(parents=True)
    body = b"bok-node-bytes"
    (pkg_dir / "bok-node-v0.3.0.tar.gz").write_bytes(body)
    (pkg_dir / "bok-node-v0.3.0.tar.gz.sha256").write_text(
        hashlib.sha256(body).hexdigest() + "  bok-node-v0.3.0.tar.gz\n")
    monkeypatch.setattr(cp_main, "BOK_NODE_ARTIFACTS_DIR_OVERRIDE", str(art), raising=False)
    # 端点读 env——monkeypatch 环境变量（app 已实例化但端点每次现读）。
    monkeypatch.setenv("BOK_NODE_ARTIFACTS_DIR", str(art))
    with TestClient(app) as client:
        cp = {"Authorization": "Bearer cp-secret-1"}
        lic = client.post("/api/nodes/licenses", json={"max_nodes": 1}, headers=cp).json()
        reg = client.post("/api/nodes/register", json={
            "name": "n", "platform": "cuda-win",
            "license_key": lic["license_key"], "fingerprint": "fp-a"}, headers=cp)
        node_token = reg.json()["node_token"]
        url = "/api/nodes/downloads/pkg/v0.3.0/bok-node-v0.3.0.tar.gz"
        # node_token 自证可下
        r = client.get(url, headers={"Authorization": f"Bearer {node_token}"})
        assert r.status_code == 200 and r.content == body
        # license key 自证可下
        r = client.get(url, headers={"Authorization": f"Bearer {lic['license_key']}"})
        assert r.status_code == 200
        # 无凭据 401；错凭据 401
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer bogus"}).status_code == 401
        # 路径段穿越/非常规字符 → 404（白名单拒绝，不触达文件系统）
        for bad in (
            "/api/nodes/downloads/pkg/..%2F..%2Fetc/bok-node-v0.3.0.tar.gz",
            "/api/nodes/downloads/pkg/v0.3.0/..%2Fsecret.txt",
            "/api/nodes/downloads/other/v0.3.0/bok-node-v0.3.0.tar.gz",
        ):
            got = client.get(bad, headers={"Authorization": f"Bearer {node_token}"})
            assert got.status_code in (403, 404), (bad, got.status_code)


def test_node_logs_upload_list_download(monkeypatch, tmp_path):
    """W2 远程日志通道：node_token 上传 gzip 束（自证+魔数+限额）→ root 清单/
    下载回读；无凭据 401、非 gzip 415、超限 413、坏文件名 404。"""
    import gzip as _gzip

    from fastapi.testclient import TestClient

    from control_plane.main import app

    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("BOK_CP_TOKEN", raising=False)
    monkeypatch.setenv("BOK_NODE_ARTIFACTS_DIR", str(tmp_path / "dl"))
    with TestClient(app) as client:
        reg = client.post("/api/nodes/register", json={"name": "n", "platform": "cuda-linux"})
        node_id, token = reg.json()["node_id"], reg.json()["node_token"]
        headers = {"Authorization": f"Bearer {token}"}
        payload = _gzip.compress(b"fake-log-bundle")
        # 无凭据 401（豁免只是免中间件，端点内 node_token 闸真实在岗）
        assert client.post("/api/nodes/logs", content=payload).status_code == 401
        assert client.post("/api/nodes/logs", content=payload,
                           headers={"Authorization": "Bearer bogus"}).status_code == 401
        # 非 gzip 415
        assert client.post("/api/nodes/logs", content=b"not-gzip",
                           headers=headers).status_code == 415
        # 超限 413（体首两字节是 gzip 魔数，验证限额先于魔数挡下）
        big = b"\x1f\x8b" + b"0" * (9 * 1024 * 1024)
        assert client.post("/api/nodes/logs", content=big,
                           headers=headers).status_code == 413
        # 正常上传（同秒两发不互覆——文件名带体长+短随机）
        r1 = client.post("/api/nodes/logs", content=payload,
                         headers={**headers, "Content-Type": "application/gzip"})
        r2 = client.post("/api/nodes/logs", content=payload,
                         headers={**headers, "Content-Type": "application/gzip"})
        assert r1.status_code == 200 and r1.json()["ok"] is True
        assert r2.json()["file"] != r1.json()["file"]
        # 清单新→旧、体长如实；下载回读逐字节一致
        listing = client.get(f"/api/nodes/{node_id}/logs").json()
        assert len(listing) == 2 and all(x["bytes"] == len(payload) for x in listing)
        got = client.get(f"/api/nodes/{node_id}/logs/{listing[0]['file']}")
        assert got.status_code == 200 and got.content == payload
        # 坏文件名（白名单外/不存在）→ 404
        assert client.get(f"/api/nodes/{node_id}/logs/nope.tar.gz").status_code == 404
        assert client.get(
            f"/api/nodes/{node_id}/logs/..%2F..%2Fsecret.tar.gz").status_code == 404
