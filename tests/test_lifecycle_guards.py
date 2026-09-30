"""生命周期守卫单测（2026-09-27）：reaper tri-state 跳过 + 建单并发/防重闸。

覆盖两处独立缺陷的收口契约：
- TASK 2：`_room_has_participants` 改 tri-state（True/False/None），reaper 对
  状态未知（None）**跳过**回收并审计 `reaper.skip`；确认空房才置 ENDED 且用
  `disposition='reaped'`（与客户真挂断区分）。旧版任何异常=可回收
  （dispatch_list_failed 628× / ≥52 通 active 被误杀，与真挂断不可分）。
- TASK 3：`_create_call_in` 建单前闸——活通话数达 BOK_MAX_ACTIVE_CALLS（默认 2，
  2026-10-01 实测诚实上限收紧；3 通从未验证全质量）
  → 409 `call.reject_concurrency`；同 object_id 已有活通话 → 409 `call.reject_duplicate`。
  （单机单并发 LLM：2 通降级可服务、6 通 Metal OOM，reports/mac-concurrency-2026-09-24。）

测试全部走内存仓 + TestClient / AsyncMock，不连真 LiveKit。
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-lifecycle-guards")

from bok_voice_business_db.repository import InMemoryBusinessRepository  # noqa: E402
from bok_voice_core.types import CallStatus  # noqa: E402

import control_plane.main as cp_main  # noqa: E402


# ---------------------------------------------------------------------------
# TASK 2：reaper tri-state
# ---------------------------------------------------------------------------


class _ReaperRepo:
    """只有一个 active 通话的假仓；记录 update_call 落点。"""

    def __init__(self, *, call_id: str = "call-reap"):
        self.call_id = call_id
        self.updates: list[tuple[str, dict]] = []

    def list_calls(self, account_id: str, status: str = ""):
        if status == CallStatus.ACTIVE.value:
            return [{"id": self.call_id, "object_id": "o-1",
                     "created_at": "2020-01-01T00:00:00+00:00"}]
        return []

    def update_call(self, call_id: str, **fields):
        self.updates.append((call_id, fields))
        return {"id": call_id, **fields}

    def get_settlement(self, call_id: str):
        return None


def _reaper_harness(monkeypatch, room_state):
    """装 reaper 依赖；room_state = _room_has_participants 的返回值。"""
    audits: list[tuple] = []
    repo = _ReaperRepo()
    monkeypatch.setattr(cp_main, "_repo", lambda: repo)
    monkeypatch.setattr(cp_main, "_room_has_participants", AsyncMock(return_value=room_state))
    monkeypatch.setattr(cp_main, "_settle_core", AsyncMock(return_value={}))
    monkeypatch.setattr(cp_main, "_lkapi_client", lambda: None)
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: audits.append((action, kw)))
    return repo, audits


def test_reaper_skips_when_room_state_unknown(monkeypatch):
    """房间状态未知（LiveKit 瞬断/鉴权/网络）→ 跳过回收 + 审计 reaper.skip。"""
    repo, audits = _reaper_harness(monkeypatch, None)
    out = asyncio.run(cp_main._reap_stale_calls_once())

    assert out["skipped"] == 1
    assert out["ended"] == 0
    assert out["settled"] == 0
    assert repo.updates == []  # 绝不置终态
    assert any(a[0] == "reaper.skip" for a in audits)
    assert cp_main._settle_core.await_count == 0


def test_reaper_ends_with_reaped_disposition_when_room_empty(monkeypatch):
    """确认空房 → ENDED 且 disposition='reaped'（与客户真挂断区分）。"""
    repo, _audits = _reaper_harness(monkeypatch, False)
    out = asyncio.run(cp_main._reap_stale_calls_once())

    assert out["ended"] == 1
    assert out["skipped"] == 0
    assert out["settled"] == 1
    ended = [f for _cid, f in repo.updates if f.get("status") == CallStatus.ENDED.value]
    assert ended and ended[0]["disposition"] == "reaped"


def test_reaper_leaves_live_call_untouched_when_participants_present(monkeypatch):
    """房间仍有真人 → 不动（既不置终态也不审计 skip）。"""
    repo, audits = _reaper_harness(monkeypatch, True)
    out = asyncio.run(cp_main._reap_stale_calls_once())

    assert out["ended"] == 0
    assert out["skipped"] == 0
    assert repo.updates == []
    assert audits == []
    assert cp_main._settle_core.await_count == 0


# ---------------------------------------------------------------------------
# TASK 3：建单并发准入 + 重复防重
# ---------------------------------------------------------------------------


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    audits: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: audits.append((action, kw)))
    client = TestClient(app).__enter__()
    return client, repo, audits


def test_create_call_rejected_at_concurrency_limit(monkeypatch):
    """达上限拒建：活通话数 >= max → 409 call.reject_concurrency，不落库。"""
    client, repo, audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_MAX_ACTIVE_CALLS", "2")
    # 本组钉并发/防重闸；实时通话强绑模板闸隔离关掉（契约见 test_template_gate.py）。
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")
    try:
        for _ in range(2):
            r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
            assert r.status_code == 200, r.text
        r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
        assert r.status_code == 409, r.text
        assert any(a[0] == "call.reject_concurrency" for a in audits)
        assert len(repo.list_calls("")) == 2  # 被拒的通话未落库
    finally:
        client.__exit__(None, None, None)


def test_create_call_passes_below_concurrency_limit(monkeypatch):
    """未超上限照建（默认 2——2026-10-01 实测诚实上限,建 2 通全过）。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.delenv("BOK_MAX_ACTIVE_CALLS", raising=False)
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")
    try:
        assert cp_main._max_active_calls_env() == 2  # 缺省收紧 3→2 的源级钉
        for _ in range(2):
            r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
            assert r.status_code == 200, r.text
        assert len(repo.list_calls("")) == 2
    finally:
        client.__exit__(None, None, None)


def test_create_call_zero_limit_is_unlimited(monkeypatch):
    """BOK_MAX_ACTIVE_CALLS=0 → 不限（建 5 通全过）。"""
    client, repo, _audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_MAX_ACTIVE_CALLS", "0")
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")
    try:
        for _ in range(5):
            r = client.post("/api/calls", json={"account_id": "acc-001", "mode": "live"})
            assert r.status_code == 200, r.text
        assert len(repo.list_calls("")) == 5
    finally:
        client.__exit__(None, None, None)


def test_create_call_rejects_duplicate_object(monkeypatch):
    """同 object_id 已有活通话 → 409 call.reject_duplicate（并发闸放宽避干扰）。"""
    client, repo, audits = _client_and_repo(monkeypatch)
    monkeypatch.setenv("BOK_MAX_ACTIVE_CALLS", "10")
    monkeypatch.setenv("BOK_REQUIRE_TEMPLATE", "0")
    try:
        r1 = client.post("/api/calls", json={"account_id": "acc-001", "object_id": "obj-dup", "mode": "live"})
        assert r1.status_code == 200, r1.text
        r2 = client.post("/api/calls", json={"account_id": "acc-001", "object_id": "obj-dup", "mode": "live"})
        assert r2.status_code == 409, r2.text
        assert any(a[0] == "call.reject_duplicate" for a in audits)
        assert len(repo.list_calls("")) == 1
        # 不同对象照建（防重只钉同对象）。
        r3 = client.post("/api/calls", json={"account_id": "acc-001", "object_id": "obj-other", "mode": "live"})
        assert r3.status_code == 200, r3.text
    finally:
        client.__exit__(None, None, None)
