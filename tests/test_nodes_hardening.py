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
