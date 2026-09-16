"""2026-09-16 深测修复回归：节点 license 配额 TOCTOU（P1-2）、指纹强制（P1-3）、
心跳指纹协议强制 + 开放期 token 拒收 + 节点级吊销（P2-7）。"""
from __future__ import annotations

import os
import threading

os.environ.setdefault("DATABASE_URL", "")

import pytest

from control_plane.nodes_store import LicenseError, NodeStore


def _store() -> NodeStore:
    s = NodeStore(None)  # 内存双模
    return s


def test_register_licensed_basic_and_quota():
    s = _store()
    lic = s.create_license(max_nodes=1, note="t")
    row, node_id, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64, name="n1")
    assert node_id and token
    # 配额满：不同指纹 → 403
    with pytest.raises(LicenseError) as ei:
        s.register_licensed(license_key=lic["license_key"], fingerprint="a" * 64)
    assert ei.value.status_code == 403
    # 同指纹幂等复用（换 token）不吃配额
    row2, node_id2, token2 = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)
    assert node_id2 == node_id and token2 != token


def test_quota_race_closed_by_lock():
    """P1-2：max_nodes=1 并发 12 注册旧版 10 个全过闸；同锁收口后只中 1。"""
    s = _store()
    lic = s.create_license(max_nodes=1, note="race")
    ok: list[str] = []
    barrier = threading.Barrier(12)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            _, _, _ = s.register_licensed(
                license_key=lic["license_key"], fingerprint=f"{i:064x}")
            ok.append(str(i))
        except LicenseError:
            pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 1, ok
    assert s._count_nodes(lic["license_id"]) == 1


def test_heartbeat_requires_fingerprint_when_bound():
    s = _store()
    lic = s.create_license(max_nodes=2, note="fp")
    _, node_id, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)
    # 绑定了指纹的节点，心跳缺指纹 = 不合作客户端 → 按指纹不符自动吊销
    ok, reason = s.heartbeat(token, {}, fingerprint="")
    assert not ok and reason == "fingerprint_mismatch"
    rows = s.list_nodes()
    assert all(r["status"] == "revoked" for r in rows if r["node_id"] == node_id)


def test_heartbeat_rejects_open_mode_token_in_hardened_mode(monkeypatch):
    s = _store()
    _, token = s.register(name="legacy", platform="t", license_id="", fingerprint="")
    ok, reason = s.heartbeat(token, {}, require_license=True)
    assert not ok and reason == "license_required"
    # 未加固模式照常
    ok, reason = s.heartbeat(token, {})
    assert ok and reason == ""


def test_node_revoke_endpoint_semantics():
    s = _store()
    lic = s.create_license(max_nodes=1, note="rv")
    _, node_id, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)
    assert s.revoke_node(node_id) is True
    assert s.revoke_node(node_id) is False  # 已吊销
    ok, reason = s.heartbeat(token, {})
    assert not ok


# ---- 熔断真实化（site-delivery M1，2026-09-16）：sticky 吊销来源 + unrevoke ----


def test_root_revoke_is_sticky_and_tags_source():
    """root 吊销打 source='root'：同指纹重注册 401 "node revoked"（不复活、
    不换发 token、配额零消耗）；auto_clone 吊销保留原机复活路径。"""
    s = _store()
    lic = s.create_license(max_nodes=1, note="sticky")
    _, node_id, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)
    assert s.revoke_node(node_id, source="root") is True
    row = s.node_by_id(node_id)
    assert row["revoked"] is True and row["revoked_source"] == "root"
    assert row["revoked_at"]  # ISO 时刻已落
    # sticky：重注册（license 仍 active、指纹相同）→ 401 node revoked。
    with pytest.raises(LicenseError) as ei:
        s.register_licensed(license_key=lic["license_key"], fingerprint="f" * 64)
    assert ei.value.status_code == 401
    assert ei.value.reason == "node revoked"
    # 旧 token 未被换发（未 mint 新 token），心跳照拒 revoked。
    assert s.heartbeat(token, fingerprint="f" * 64) == (False, "revoked")
    # 配额零消耗：行数没涨（没有新建行绕过 sticky）。
    assert sum(1 for r in s.list_nodes()) == 1


def test_auto_clone_revoke_still_revives_via_reregister():
    """auto_clone 吊销（克隆/挪机检出）非 sticky：原机指纹重注册复活同一 node_id
    换新 token；复活后 revoked_source 清空、心跳正常。"""
    s = _store()
    lic = s.create_license(max_nodes=1, note="clone")
    node_id, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)[1:]
    # 克隆心跳 → 自动吊销，来源 auto_clone。
    assert s.heartbeat(token, fingerprint="fp-clone") == (False, "fingerprint_mismatch")
    row = s.node_by_id(node_id)
    assert row["revoked"] and row["revoked_source"] == "auto_clone"
    # 原机重注册 → 复活（非 sticky）。
    node_id2, token2 = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)[1:]
    assert node_id2 == node_id
    row2 = s.node_by_id(node_id)
    assert row2["revoked"] is False and row2["revoked_source"] == ""
    assert s.heartbeat(token2, fingerprint="f" * 64) == (True, "")


def test_unrevoke_clears_sticky_and_reenables_revive():
    """unrevoke：root 吊销解除后（source→''、revoked_at 留作历史）重注册复活；
    未吊销节点 unrevoke → "live"（端点 409 语义）；未知 → None。"""
    s = _store()
    lic = s.create_license(max_nodes=1, note="unrv")
    node_id, _ = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)[1:]
    s.revoke_node(node_id, source="root")
    revoked_at = s.node_by_id(node_id)["revoked_at"]
    assert s.unrevoke_node(node_id) == "unrevoked"
    row = s.node_by_id(node_id)
    assert row["revoked"] is False and row["revoked_source"] == ""
    assert row["revoked_at"] == revoked_at  # 历史保留
    # 解除后同指纹重注册复活。
    node_id2, token2 = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)[1:]
    assert node_id2 == node_id and token2
    assert s.heartbeat(token2, fingerprint="f" * 64) == (True, "")
    # live 节点再 unrevoke → "live"；未知节点 → None。
    assert s.unrevoke_node(node_id) == "live"
    assert s.unrevoke_node("node-does-not-exist") is None


def test_unrevoke_sql_mode_sets_offline_status(tmp_path):
    """SQL 模式 unrevoke 语义：status→offline（须重注册/心跳才回 online）、
    revoked_source→''、revoked_at 保留——与内存双模出参同契约。"""
    from bok_voice_business_db import models
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path}/unrv.db", future=True)
    models.create_all(engine)
    s = NodeStore(engine)
    lic = s.create_license(max_nodes=1)
    node_id, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="fp-sql")[1:]
    s.revoke_node(node_id, source="root")
    assert s.heartbeat(token, fingerprint="fp-sql") == (False, "revoked")
    assert s.unrevoke_node(node_id) == "unrevoked"
    row = s.node_by_id(node_id)
    assert row["status"] == "offline" and row["revoked_source"] == ""
    assert row["revoked_at"]  # 历史保留
    assert s.heartbeat(token, fingerprint="fp-sql") == (True, "")  # 心跳复活回在线


def test_resolve_node_token_tolerates_absent_and_foreign_credentials():
    """resolve_node_token：node_token → 节点行；空串/用户凭据类随机串 → None
    （窒息点中间件对非节点 Bearer 必须零命中直通）。"""
    s = _store()
    lic = s.create_license(max_nodes=1)
    _, _, token = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)
    node = s.resolve_node_token(token)
    assert node is not None and node["revoked"] is False
    assert s.resolve_node_token("") is None
    assert s.resolve_node_token("not-a-node-token") is None


def test_license_revoke_tags_nodes_root():
    """license 吊销连带节点也是 root 面动作 → source='root'（sticky 口径一致）。"""
    s = _store()
    lic = s.create_license(max_nodes=2, note="licrev")
    node_id, _ = s.register_licensed(
        license_key=lic["license_key"], fingerprint="f" * 64)[1:]
    out = s.revoke_license(lic["license_id"])
    assert out["nodes_revoked"] == 1
    assert s.node_by_id(node_id)["revoked_source"] == "root"


def test_deps_build_engine_adds_killswitch_columns_fresh_and_migrated(tmp_path, monkeypatch):
    """build_engine 幂等补列回归：fresh 库与「缺三列的存量库」二跑后
    nodes.revoked_source/revoked_at 与 call_sessions.node_id 都在。"""
    from sqlalchemy import create_engine, inspect as sa_inspect, text

    from control_plane.deps import build_engine

    db = tmp_path / "ks.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    # fresh 库：建表即带列。
    engine = build_engine()
    assert engine is not None
    insp = sa_inspect(engine)
    node_cols = {c["name"] for c in insp.get_columns("nodes")}
    call_cols = {c["name"] for c in insp.get_columns("call_sessions")}
    assert {"revoked_source", "revoked_at"} <= node_cols
    assert "node_id" in call_cols
    # 迁移库：模拟 M1 前存量（剥掉三列）→ 再跑 build_engine 幂等补回。
    engine.dispose()
    raw = create_engine(f"sqlite:///{db}", future=True)
    with raw.begin() as conn:
        conn.execute(text("ALTER TABLE nodes DROP COLUMN revoked_source"))
        conn.execute(text("ALTER TABLE nodes DROP COLUMN revoked_at"))
        conn.execute(text("ALTER TABLE call_sessions DROP COLUMN node_id"))
    raw.dispose()
    engine2 = build_engine()
    assert engine2 is not None
    insp2 = sa_inspect(engine2)
    assert {"revoked_source", "revoked_at"} <= {
        c["name"] for c in insp2.get_columns("nodes")}
    assert "node_id" in {c["name"] for c in insp2.get_columns("call_sessions")}


def test_deps_dedupes_duplicate_fingerprint_rows(tmp_path):
    """终审修复(2)：唯一索引建前的 (license_id, fingerprint) 去重语句回归。

    历史配额竞态可能在 nodes 表留下同 (license_id, fingerprint) 重复行，令
    CREATE UNIQUE INDEX 直接失败。用裸 SQLAlchemy engine 执行 deps.py 的
    NODES_FP_DEDUPE_SQL（与启动迁移同一语句，防漂移）：行数收敛、每组保留
    MAX(id) 行、随后唯一索引可建成；open-mode（license_id=''）行不受影响。
    """
    from sqlalchemy import create_engine, text

    from control_plane.deps import NODES_FP_DEDUPE_SQL

    engine = create_engine(f"sqlite:///{tmp_path}/nodes_dedupe.db", future=True)
    from bok_voice_business_db import models

    models.create_all(engine)
    with engine.begin() as conn:
        # 同 (lic-1, fp-A) 三行：应只留 MAX(id)='node-n3'；另置一组 (lic-2, fp-B)
        # 重复两行 + 一行 open-mode（license_id=''）重复两行（必须原样保留）。
        conn.execute(text(
            "INSERT INTO nodes (id, org_id, name, token_hash, platform, version,"
            " status, metrics_json, license_id, fingerprint, created_at) VALUES"
            "('node-n1','o','a','','t','v','offline','{}','lic-1','fp-A','2026-09-16 00:00:01'),"
            "('node-n3','o','c','','t','v','offline','{}','lic-1','fp-A','2026-09-16 00:00:01'),"
            "('node-n2','o','b','','t','v','offline','{}','lic-1','fp-A','2026-09-16 00:00:01'),"
            "('node-m1','o','m1','','t','v','offline','{}','lic-2','fp-B','2026-09-16 00:00:01'),"
            "('node-m2','o','m2','','t','v','offline','{}','lic-2','fp-B','2026-09-16 00:00:01'),"
            "('node-o1','o','o1','','t','v','offline','{}','','fp-X','2026-09-16 00:00:01'),"
            "('node-o2','o','o2','','t','v','offline','{}','','fp-X','2026-09-16 00:00:01')"
        ))
        result = conn.execute(text(NODES_FP_DEDUPE_SQL))
        # lic-1 组删 2（留 n3）、lic-2 组删 1（留 m2）；open-mode 组零删除。
        assert result.rowcount == 3, result.rowcount
        remaining = dict(conn.execute(
            text("SELECT id, license_id FROM nodes")
        ).fetchall())
    assert set(remaining) == {"node-n3", "node-m2", "node-o1", "node-o2"}
    assert remaining["node-n3"] == "lic-1" and remaining["node-m2"] == "lic-2"
    # 去重后部分唯一索引必须可建成（重复行在则 IntegrityError）。
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_license_fingerprint "
            "ON nodes (license_id, fingerprint) WHERE license_id <> ''"
        ))
    # 幂等：再跑一遍零删除，索引仍在。
    with engine.begin() as conn:
        assert conn.execute(text(NODES_FP_DEDUPE_SQL)).rowcount == 0
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_license_fingerprint "
            "ON nodes (license_id, fingerprint) WHERE license_id <> ''"
        ))
