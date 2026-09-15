"""2026-09-16 深测修复回归：节点 license 配额 TOCTOU（P1-2）与指纹强制（P1-3）。"""
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
