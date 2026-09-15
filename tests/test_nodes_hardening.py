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
