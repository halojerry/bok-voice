"""M-27 派发黑洞看门狗测试（2026-09-23 生产就绪修复波#2）。

背景（task-8 §4-A / task-7 F2 实证）：worker load>0.7 自标 unavailable 的瞬时窗
（~2.5s）恰跨 token 建单瞬间 → LiveKit 建房触发的 RoomConfiguration dispatch
丢失且无重试 → 整轮死空气（2/55 轮 ≈1.8%，通话 active 悬挂需人工收）。
既有 webhook 恢复链只盖「agent 离房」（participant_left），不盖「agent 从未入房」。

修复契约：
- token 签发（A 线 dispatch 分支）后 CP 起看门狗：短退避重试窗内验证 agent 是否
  回房；房间已有真人而 agent 缺席 = 派发丢失信号 → 清扫 stale dispatch 后显式
  create_dispatch（官方 AgentDispatchService，与 webhook 恢复链同款纪律：
  终态不派 / 防重 / per-room 锁 / stale 清扫）；
- 杀开关 `BOK_DISPATCH_RETRY`（默认开；"0"=关。CP 侧键走 `_control_plane_env`
  注入面——BOK_POLISH_OFFLINE 判例，非 _FORWARD_ENV）。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-dispatch-watchdog")

from livekit.api import JS_PENDING  # noqa: E402

import control_plane.main as cp_main  # noqa: E402


# ---- fakes ----


def _participant(identity: str):
    return SimpleNamespace(identity=identity)


class _FakeRooms:
    """list_participants 假体：rooms_fn() -> dict[room, identity 列表]；缺房=不存在。"""

    def __init__(self, rooms_fn):
        self._rooms_fn = rooms_fn

    async def list_participants(self, req):
        rooms = self._rooms_fn()
        room = getattr(req, "room", "")
        if room not in rooms:
            raise RuntimeError(f"room not found: {room}")
        return SimpleNamespace(participants=[_participant(i) for i in rooms[room]])


class _FakeDispatches:
    """agent_dispatch 假体：记录 create/delete；dispatch 表可编程。"""

    def __init__(self, dispatches=None):
        self.dispatches = list(dispatches or [])
        self.created: list[str] = []
        self.deleted: list[str] = []

    async def list_dispatch(self, room_name: str):
        return list(self.dispatches)

    async def create_dispatch(self, req):
        self.created.append(getattr(req, "room", ""))

    async def delete_dispatch(self, dispatch_id: str, room_name: str):
        self.deleted.append(dispatch_id)
        self.dispatches = [d for d in self.dispatches if d.id != dispatch_id]


async def _noop_close():
    return None


def _fake_lkapi(rooms_fn, dispatches=None):
    disp = _FakeDispatches(dispatches)
    lkapi = SimpleNamespace(room=_FakeRooms(rooms_fn), agent_dispatch=disp, aclose=_noop_close)
    return lkapi, disp


def _stale_dispatch():
    """stale dispatch：带 RUNNING job（旧死局形状），但 agent 实际不在房。"""
    return SimpleNamespace(
        id="d-stale", agent_name="bok-voice",
        state=SimpleNamespace(jobs=[SimpleNamespace(state=SimpleNamespace(status=1))]),
    )


def _active_dispatch():
    """活跃 dispatch：job 处于 PENDING（in-flight，防重应让位）。"""
    return SimpleNamespace(
        id="d-active", agent_name="bok-voice",
        state=SimpleNamespace(jobs=[SimpleNamespace(state=SimpleNamespace(status=JS_PENDING))]),
    )


def _live_repo():
    return SimpleNamespace(get_call=lambda rid: {"id": rid, "status": "active"})


@pytest.fixture()
def env_guard(monkeypatch):
    """隔离进程内 in-flight 表 + 审计 + 主 loop 旗标；返回审计记录列表。

    `_main_loop=None` 强制线程兜底路径——单元级调度测试要钉的是该路径的去重/
    封顶/杀开关;主 loop 路由由专用测试钉（run_coroutine_threadsafe 归一锁域）。
    """
    audits: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: audits.append((action, kw)))
    monkeypatch.setattr(cp_main, "_dispatch_watchdog_inflight", set())
    monkeypatch.setattr(cp_main, "_main_loop", None)
    return audits


# ---- attempt 级单测 ----


def test_attempt_creates_dispatch_when_humans_present_agent_absent(env_guard, monkeypatch):
    """核心场景：房里有真人、agent 缺席 = 派发丢失 → 显式补派 + 审计。"""
    lkapi, disp = _fake_lkapi(lambda: {"call-x": ["operator-acc-001-call-x"]})
    monkeypatch.setattr(cp_main, "_lkapi_client", lambda: lkapi)
    monkeypatch.setattr(cp_main, "_repo", _live_repo)
    outcome = asyncio.run(cp_main._dispatch_watchdog_attempt(lkapi, "call-x", 0))
    assert outcome == "created"
    assert disp.created == ["call-x"]
    assert any(a[0] == "agent.watchdog_dispatch" for a in env_guard)


def test_attempt_skips_when_agent_already_in_room(env_guard, monkeypatch):
    lkapi, disp = _fake_lkapi(lambda: {"call-x": ["operator-a", "agent-AJ_123"]})
    monkeypatch.setattr(cp_main, "_repo", _live_repo)
    outcome = asyncio.run(cp_main._dispatch_watchdog_attempt(lkapi, "call-x", 0))
    assert outcome == "recovered"
    assert disp.created == []


def test_attempt_gives_up_on_terminal_call(env_guard, monkeypatch):
    """挂断抢先：终态通话不得补派（防复活门，与 webhook 重派同纪律）。"""
    lkapi, disp = _fake_lkapi(lambda: {"call-x": ["operator-a"]})
    monkeypatch.setattr(
        cp_main, "_repo",
        lambda: SimpleNamespace(get_call=lambda rid: {"id": rid, "status": "ended"}),
    )
    outcome = asyncio.run(cp_main._dispatch_watchdog_attempt(lkapi, "call-x", 0))
    assert outcome == "terminal"
    assert disp.created == []


def test_attempt_waits_when_room_not_yet_created(env_guard, monkeypatch):
    """房内零真人 = 建房触发的 token 派发根本没发生，不算丢失——等，不补派。"""
    lkapi, disp = _fake_lkapi(lambda: {"call-x": []})  # 房在、无人
    monkeypatch.setattr(cp_main, "_repo", _live_repo)
    outcome = asyncio.run(cp_main._dispatch_watchdog_attempt(lkapi, "call-x", 0))
    assert outcome == "waiting"
    assert disp.created == []


def test_attempt_holds_when_active_dispatch_in_flight(env_guard, monkeypatch):
    """已有 PENDING/RUNNING job（正常派发在途）→ 让位，不叠加第二套 agent。"""
    lkapi, disp = _fake_lkapi(lambda: {"call-x": ["operator-a"]}, dispatches=[_active_dispatch()])
    monkeypatch.setattr(cp_main, "_repo", _live_repo)
    outcome = asyncio.run(cp_main._dispatch_watchdog_attempt(lkapi, "call-x", 0))
    assert outcome == "dup"
    assert disp.created == []


def test_attempt_cleans_stale_dispatch_on_retry(env_guard, monkeypatch):
    """复查轮（attempt>0）：agent 仍缺席 → 清扫 stale dispatch（旧死局形状）再补派。

    webhook 恢复链实证的 OSS 缺口：agent 缺席时旧 dispatch 常驻「活跃」，不清扫
    会让防重门永远让位 = 双重死锁。"""
    lkapi, disp = _fake_lkapi(lambda: {"call-x": ["operator-a"]}, dispatches=[_stale_dispatch()])
    monkeypatch.setattr(cp_main, "_repo", _live_repo)
    outcome = asyncio.run(cp_main._dispatch_watchdog_attempt(lkapi, "call-x", 1))
    assert outcome == "created"
    assert disp.deleted == ["d-stale"]
    assert disp.created == ["call-x"]


def test_attempt_last_chance_create_still_fires_on_last_attempt(env_guard, monkeypatch):
    """末次尝试：真人仍缺席 → 最后一搏补派（worker 可能刚恢复）并留痕，不当失败收。"""
    lkapi, disp = _fake_lkapi(lambda: {"call-x": ["operator-a"]})
    monkeypatch.setattr(cp_main, "_repo", _live_repo)
    outcome = asyncio.run(cp_main._dispatch_watchdog_attempt(lkapi, "call-x", 2, is_last=True))
    assert outcome == "created"
    assert disp.created == ["call-x"]
    assert any(a[0] == "agent.watchdog_dispatch" for a in env_guard)


def test_attempt_api_error_on_last_stays_silent(monkeypatch):
    """末次尝试 API 异常（房未建/瞬断，状态未知）→ 不留 exhausted 审计（探针签
    token 不 join 是常态，审计只记「真人在场仍未恢复」的真丢失）。"""

    class _BoomRooms:
        async def list_participants(self, req):
            raise RuntimeError("livekit api down")

    audits: list[tuple] = []
    monkeypatch.setattr(cp_main, "_audit", lambda action, **kw: audits.append((action, kw)))
    lkapi = SimpleNamespace(room=_BoomRooms(), agent_dispatch=_FakeDispatches(), aclose=_noop_close)
    monkeypatch.setattr(cp_main, "_repo", _live_repo)
    outcome = asyncio.run(cp_main._dispatch_watchdog_attempt(lkapi, "call-x", 2, is_last=True))
    assert outcome == "error"  # 状态未知=可重试档；末次后 loop 自然收口
    assert audits == []


# ---- loop 级单测 ----


def test_loop_recovers_across_attempts(env_guard, monkeypatch):
    """首轮房未建（waiting）→ 次轮真人入场 agent 缺席（补派）→ 末轮 agent 回房收口。"""
    step = {"n": 0}

    def _rooms():
        if step["n"] == 0:
            return {}
        if step["n"] == 1:
            return {"call-x": ["operator-a"]}
        return {"call-x": ["operator-a", "agent-AJ_9"]}

    class _StepRooms(_FakeRooms):
        async def list_participants(self, req):
            step["n"] += 1
            return await super().list_participants(req)

    disp = _FakeDispatches()
    lkapi = SimpleNamespace(room=_StepRooms(_rooms), agent_dispatch=disp, aclose=_noop_close)
    monkeypatch.setattr(cp_main, "_lkapi_client", lambda: lkapi)
    monkeypatch.setattr(cp_main, "_repo", _live_repo)
    outcome = asyncio.run(cp_main._dispatch_watchdog_loop("call-x", schedule=(0, 0, 0)))
    assert outcome == "recovered"
    assert disp.created == ["call-x"]


# ---- 调度入口 ----


def test_schedule_spawns_daemon_watchdog_dedupes_inflight(env_guard, monkeypatch):
    """token 签发后的同步入口：起守护线程跑 loop；in-flight 去重；收尾清表。"""
    ran: list[str] = []

    async def _fake_loop(room, schedule=None):
        ran.append(room)
        await asyncio.sleep(0.3)  # 撑住 in-flight 窗口供第二次调用撞去重
        return "recovered"

    monkeypatch.setattr(cp_main, "_dispatch_watchdog_loop", _fake_loop)
    monkeypatch.delenv("BOK_DISPATCH_RETRY", raising=False)
    cp_main._schedule_dispatch_watchdog("call-a")
    cp_main._schedule_dispatch_watchdog("call-a")  # in-flight 去重
    deadline = time.monotonic() + 3
    while len(ran) < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ran == ["call-a"]
    deadline = time.monotonic() + 3
    while cp_main._dispatch_watchdog_inflight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "call-a" not in cp_main._dispatch_watchdog_inflight  # 收尾清表


def test_schedule_kill_switch(env_guard, monkeypatch):
    """BOK_DISPATCH_RETRY=0 → 整块 no-op（零线程零副作用）。"""
    ran: list[str] = []

    async def _fake_loop(room, schedule=None):
        ran.append(room)
        return "recovered"

    monkeypatch.setattr(cp_main, "_dispatch_watchdog_loop", _fake_loop)
    monkeypatch.setenv("BOK_DISPATCH_RETRY", "0")
    cp_main._schedule_dispatch_watchdog("call-k")
    time.sleep(0.05)
    assert ran == []


def test_schedule_inflight_cap(env_guard, monkeypatch):
    """in-flight 封顶：超限时新房间不再起线程（防 token 风暴线程膨胀）。"""
    ran: list[str] = []

    async def _fake_loop(room, schedule=None):
        ran.append(room)
        return "recovered"

    monkeypatch.setattr(cp_main, "_dispatch_watchdog_loop", _fake_loop)
    monkeypatch.delenv("BOK_DISPATCH_RETRY", raising=False)
    monkeypatch.setattr(
        cp_main, "_dispatch_watchdog_inflight",
        {f"call-{i}" for i in range(cp_main._DISPATCH_WATCHDOG_CAP)},
    )
    cp_main._schedule_dispatch_watchdog("call-new")
    time.sleep(0.05)
    assert ran == []


# ---- I-1（fix round 1）：主 loop 路由 + 跨上下文锁争用 ----


def _background_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True, name="fake-cp-main-loop")
    t.start()
    return loop, t


def test_schedule_routes_to_main_loop_when_captured(monkeypatch):
    """I-1 方案①：startup 捕获的主 loop 在场 → 看门狗作业经
    run_coroutine_threadsafe 挂【主 loop】跑（_redispatch_locks 锁域与 webhook
    恢复链归一），不再起守护线程自持 loop（跨 loop RuntimeError 根除）。"""
    ran: list[asyncio.AbstractEventLoop] = []

    async def _fake_loop(room, schedule=None):
        ran.append(asyncio.get_running_loop())
        return "recovered"

    monkeypatch.setattr(cp_main, "_dispatch_watchdog_loop", _fake_loop)
    monkeypatch.delenv("BOK_DISPATCH_RETRY", raising=False)
    monkeypatch.setattr(cp_main, "_dispatch_watchdog_inflight", set())
    loop, thread = _background_loop()
    try:
        monkeypatch.setattr(cp_main, "_main_loop", loop)
        cp_main._schedule_dispatch_watchdog("call-loop")
        deadline = time.monotonic() + 3
        while not ran and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ran and ran[0] is loop  # 作业跑在捕获的主 loop 上
        assert not any(
            t.name.startswith("dispatch-watchdog-") for t in threading.enumerate()
        )  # 不再起自持 loop 线程
        assert "call-loop" not in cp_main._dispatch_watchdog_inflight
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_cross_context_lock_contention_serialized(monkeypatch):
    """I-1 跨上下文争用：线程侧排程（看门狗形状）与主 loop 侧协程（webhook 恢复
    链形状）并发抢同房 `_redispatch_locks`——方案①下双方同 loop，互斥成立、
    临界段零重叠、无跨 loop RuntimeError。"""
    monkeypatch.delenv("BOK_DISPATCH_RETRY", raising=False)
    monkeypatch.setattr(cp_main, "_dispatch_watchdog_inflight", set())
    loop, thread = _background_loop()
    occupancy = {"n": 0, "max": 0}

    async def _critical_section():
        async with cp_main._redispatch_locks["call-x"]:
            occupancy["n"] += 1
            occupancy["max"] = max(occupancy["max"], occupancy["n"])
            await asyncio.sleep(0.005)  # 让出执行权：真并发窗
            occupancy["n"] -= 1

    async def _fake_attempt(lkapi, room, attempt, *, is_last=False):
        await _critical_section()
        return "created"

    async def _webhook_shape():
        for _ in range(6):
            await _critical_section()

    monkeypatch.setattr(cp_main, "_dispatch_watchdog_attempt", _fake_attempt)
    monkeypatch.setattr(cp_main, "_repo", _live_repo)
    monkeypatch.setattr(cp_main, "_DISPATCH_WATCHDOG_SCHEDULE", (0.0,))  # 单次尝试：作业秒收口
    lkapi, _disp = _fake_lkapi(lambda: {"call-x": ["operator-a"]})
    monkeypatch.setattr(cp_main, "_lkapi_client", lambda: lkapi)
    try:
        monkeypatch.setattr(cp_main, "_main_loop", loop)
        asyncio.run_coroutine_threadsafe(_webhook_shape(), loop)
        for _ in range(6):
            cp_main._schedule_dispatch_watchdog("call-x")
            deadline = time.monotonic() + 3
            while "call-x" in cp_main._dispatch_watchdog_inflight and time.monotonic() < deadline:
                time.sleep(0.01)  # 等本窗收口再排下一窗（模拟不同 token 签发时刻）
        # 跨线程提交的临界段照常执行（无 RuntimeError 吞没），互斥成立
        fut = asyncio.run_coroutine_threadsafe(
            cp_main._dispatch_watchdog_attempt(lkapi, "call-x", 0), loop
        )
        assert fut.result(timeout=5) == "created"
        assert occupancy["max"] == 1  # 临界段零重叠
    finally:
        loop.call_soon_threadsafe(loop.stop)


# ---- token 端点接线 ----


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from bok_voice_business_db.repository import InMemoryBusinessRepository
    from control_plane.main import app
    from control_plane.nodes_store import NodeStore

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    monkeypatch.setattr(app.state, "node_store", NodeStore(None), raising=False)
    monkeypatch.setattr(cp_main, "_token_issue_times", {})
    return TestClient(app), repo


def test_token_endpoint_schedules_watchdog_for_a_line_dispatch(monkeypatch):
    """A 线有记录房间 token → 看门狗排程；recordless / interpret / listen 不排程。"""
    client, _repo = _client(monkeypatch)
    scheduled: list[str] = []
    monkeypatch.setattr(cp_main, "_schedule_dispatch_watchdog", lambda room: scheduled.append(room))

    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    r = client.post("/api/token", json={"account_id": "acc-001", "call_id": call["id"]})
    assert r.status_code == 201
    assert scheduled == [call["id"]]

    # recordless：不排程（无 dispatch 可守）
    scheduled.clear()
    client.post("/api/token", json={"account_id": "acc-001", "room_name": "ghost-room"})
    assert scheduled == []

    # interpret：不排程（B 线短命房、崩溃不自动补位是既有契约）
    icall = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "language": "zh", "target_lang": "en"},
    ).json()
    client.post("/api/token", json={"account_id": "acc-001", "call_id": icall["id"], "role": "me"})
    assert scheduled == []

    # listen：不排程（旁听零副作用）
    scheduled.clear()
    client.post(
        "/api/token",
        json={"account_id": "acc-001", "call_id": call["id"], "purpose": "listen"},
    )
    assert scheduled == []


def test_token_endpoint_watchdog_never_breaks_token_issue(monkeypatch):
    """看门狗 loop 崩溃（守护线程内）不得打断 token 签发主链路，in-flight 表照常清。"""
    client, _repo = _client(monkeypatch)

    async def _explode(room, schedule=None):
        raise RuntimeError("watchdog loop exploded")

    monkeypatch.setattr(cp_main, "_dispatch_watchdog_loop", _explode)
    monkeypatch.delenv("BOK_DISPATCH_RETRY", raising=False)
    monkeypatch.setattr(cp_main, "_dispatch_watchdog_inflight", set())
    call = client.post("/api/calls", json={"account_id": "acc-001"}).json()
    r = client.post("/api/token", json={"account_id": "acc-001", "call_id": call["id"]})
    assert r.status_code == 201
    deadline = __import__("time").monotonic() + 3
    while cp_main._dispatch_watchdog_inflight and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.02)
    assert call["id"] not in cp_main._dispatch_watchdog_inflight
