"""Dispatch 工具函数单测：防重判定（has_active_dispatch）+ 主动回收（cleanup_dispatch）。

全部走 mock 的 lkapi（AsyncMock），不连真实 LiveKit；dispatch/job 结构用真实 protobuf
消息构造（livekit.api.AgentDispatch / livekit.protocol.agent.JobState），防 mock 结构漂移。

第二部分是调用点级测试：monkeypatch control_plane.main 里的工具函数与
`_lkapi_client`，钉死三处接线的行为——
- webhook 崩溃重派：已有活跃 dispatch → 跳过 create_dispatch（REDISPATCH_SKIP）；
- hangup 端点：ENDED+断房后回收该房间 dispatch；
- reaper ENDED 分支：空房置 ENDED+settle 后回收 dispatch。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from livekit.api import AgentDispatch
from livekit.protocol.agent import JS_FAILED, JS_PENDING, JS_RUNNING, JS_SUCCESS, JobState

from control_plane.dispatch_utils import cleanup_dispatch, has_active_dispatch


def _dispatch(agent_name: str, job_statuses: tuple[int, ...] = (), dispatch_id: str = "") -> AgentDispatch:
    d = AgentDispatch(id=dispatch_id, agent_name=agent_name)
    for status in job_statuses:
        d.state.jobs.add(state=JobState(status=status))
    return d


def _lkapi(
    dispatches: list[AgentDispatch] | Exception,
    delete_results: list[AgentDispatch | Exception] | None = None,
) -> MagicMock:
    api = MagicMock()
    if isinstance(dispatches, Exception):
        api.agent_dispatch.list_dispatch = AsyncMock(side_effect=dispatches)
    else:
        api.agent_dispatch.list_dispatch = AsyncMock(return_value=dispatches)
    if delete_results is not None:
        api.agent_dispatch.delete_dispatch = AsyncMock(side_effect=delete_results)
    else:
        api.agent_dispatch.delete_dispatch = AsyncMock(return_value=AgentDispatch())
    return api


def test_has_active_dispatch_true_when_same_agent_has_pending_or_running_job():
    """同 agent_name 且有 PENDING/RUNNING job → 已有活跃 dispatch，防重派。"""
    lkapi = _lkapi(
        [
            _dispatch("other-agent", (JS_RUNNING,)),  # 别的 agent，不算
            _dispatch("bok-voice", (JS_SUCCESS,)),  # 已结束，不算
            _dispatch("bok-voice", (JS_PENDING,)),  # 同 agent PENDING → True
            _dispatch("bok-voice", (JS_RUNNING,)),  # 同 agent RUNNING → True
        ]
    )
    assert asyncio.run(has_active_dispatch(lkapi, "room-1")) is True


def test_has_active_dispatch_false_when_no_live_job_for_same_agent():
    """只有其他 agent 的 dispatch、或同 agent jobs 全空/全结束 → False。"""
    lkapi = _lkapi(
        [
            _dispatch("other-agent", (JS_RUNNING, JS_PENDING)),
            _dispatch("bok-voice"),
            _dispatch("bok-voice", (JS_SUCCESS, JS_FAILED)),
        ]
    )
    assert asyncio.run(has_active_dispatch(lkapi, "room-1")) is False


def test_has_active_dispatch_api_error_is_best_effort_false():
    """list_dispatch 抛异常 → 防重判定 best-effort 返回 False（不阻断通话创建主链路）。"""
    lkapi = _lkapi(RuntimeError("list boom"))
    assert asyncio.run(has_active_dispatch(lkapi, "room-1")) is False


def test_cleanup_dispatch_deletes_every_dispatch_and_returns_count():
    """两个 dispatch → delete_dispatch 调两次、参数序正确（dispatch_id, room_name）、返回 2。"""
    lkapi = _lkapi(
        [
            _dispatch("bok-voice", dispatch_id="dp-1"),
            _dispatch("other-agent", dispatch_id="dp-2"),
        ]
    )
    assert asyncio.run(cleanup_dispatch(lkapi, "room-1")) == 2
    assert lkapi.agent_dispatch.delete_dispatch.await_count == 2
    first = lkapi.agent_dispatch.delete_dispatch.await_args_list[0].kwargs
    assert first["dispatch_id"] == "dp-1"
    assert first["room_name"] == "room-1"


def test_cleanup_dispatch_empty_list_returns_zero():
    """无 dispatch（空列表）→ 返回 0 且不调 delete。"""
    lkapi = _lkapi([])
    assert asyncio.run(cleanup_dispatch(lkapi, "room-1")) == 0
    lkapi.agent_dispatch.delete_dispatch.assert_not_awaited()


def test_cleanup_dispatch_swallows_list_error_and_returns_zero():
    """list_dispatch 抛异常 → 不 raise、返回 0。"""
    lkapi = _lkapi(RuntimeError("list boom"))
    assert asyncio.run(cleanup_dispatch(lkapi, "room-1")) == 0


def test_cleanup_dispatch_partial_delete_failure_keeps_deleted_count():
    """单个 delete 失败不吞掉已删数：第一个成功第二个炸 → 返回 1。"""
    lkapi = _lkapi(
        [
            _dispatch("bok-voice", dispatch_id="dp-1"),
            _dispatch("bok-voice", dispatch_id="dp-2"),
        ],
        delete_results=[AgentDispatch(), RuntimeError("delete boom")],
    )
    assert asyncio.run(cleanup_dispatch(lkapi, "room-1")) == 1
    assert lkapi.agent_dispatch.delete_dispatch.await_count == 2


# ---- 调用点级测试（control_plane.main 三处接线）----


def _lkapi_dummy() -> MagicMock:
    """patch `_lkapi_client` 用的一次性假客户端：aclose 必须 awaitable。"""
    return MagicMock(aclose=AsyncMock())


def _wait_awaited(mock: AsyncMock, timeout: float = 5.0) -> None:
    """webhook 用 create_task fire-and-forget，轮询等待后台任务跑到 mock。"""
    deadline = time.monotonic() + timeout
    while mock.await_count == 0:
        if time.monotonic() > deadline:
            raise AssertionError("background redispatch task did not reach mock in time")
        time.sleep(0.02)


def _post_webhook(client: TestClient, room: str) -> dict:
    return client.post(
        "/api/webhook/livekit",
        json={"event": "participant_left", "room": {"name": room}, "participant": {"identity": "bok-voice"}},
    ).json()


def test_webhook_redispatch_skipped_when_active_dispatch_exists(monkeypatch):
    """崩溃重派防重：同 agent 已有活跃 dispatch → 不调 create_dispatch + REDISPATCH_SKIP 打点。"""
    from control_plane import main as m

    lkapi = _lkapi_dummy()
    lkapi.agent_dispatch.create_dispatch = AsyncMock()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    has_active = AsyncMock(return_value=True)
    monkeypatch.setattr(m, "has_active_dispatch", has_active)
    skip_log = MagicMock()
    monkeypatch.setattr(m, "control_log", skip_log)

    with TestClient(m.app) as client:
        assert _post_webhook(client, "room-skip") == {"handled": True, "redispatch": "bok-voice"}
        _wait_awaited(has_active)

    has_active.assert_awaited_once_with(lkapi, "room-skip")
    lkapi.agent_dispatch.create_dispatch.assert_not_awaited()
    lkapi.aclose.assert_awaited_once()
    # REDISPATCH_SKIP 打点：structured event 与房间名可追溯。
    assert skip_log.info.call_count == 1
    assert "redispatch_skip" in str(skip_log.info.call_args.args[0])
    assert skip_log.info.call_args.kwargs["extra"]["event"] == "dispatch.redispatch.skip"
    assert skip_log.info.call_args.kwargs["extra"]["data"]["room"] == "room-skip"


def test_webhook_redispatch_creates_when_no_active_dispatch(monkeypatch):
    """无活跃 dispatch → 照常 create_dispatch（防重不误伤正常重派）。"""
    from control_plane import main as m

    lkapi = _lkapi_dummy()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    monkeypatch.setattr(m, "has_active_dispatch", AsyncMock(return_value=False))
    create = AsyncMock(return_value=AgentDispatch())
    lkapi.agent_dispatch.create_dispatch = create

    with TestClient(m.app) as client:
        _post_webhook(client, "room-create")
        _wait_awaited(create)

    create.assert_awaited_once_with(room="room-create", agent_name="bok-voice")
    lkapi.aclose.assert_awaited_once()


def test_hangup_cleans_up_dispatch_after_ended(monkeypatch):
    """挂断：置 ENDED + 断房后必须 best-effort 回收该房间 dispatch，失败不外溢。"""
    from control_plane import main as m

    lkapi = _lkapi_dummy()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    cleanup = AsyncMock(return_value=2)
    monkeypatch.setattr(m, "cleanup_dispatch", cleanup)

    with TestClient(m.app) as client:
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
        ).json()
        resp = client.post(f"/api/calls/{created['id']}/hangup")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ended"
    cleanup.assert_awaited_once_with(lkapi, created["id"])
    lkapi.aclose.assert_awaited_once()


def test_reaper_ended_branch_cleans_up_dispatch(monkeypatch):
    """reaper：空房通话置 ENDED+settle 后回收 dispatch（房间名=call_id）。"""
    from bok_voice_core.types import CallStatus

    from control_plane import main as m

    class FakeRepo:
        def list_calls(self, account_id: str, status: str = ""):
            if status == CallStatus.ACTIVE.value:
                return [{"id": "call-reap-me", "created_at": "2026-09-09T00:00:00+00:00"}]
            return []

        def update_call(self, call_id: str, **fields):
            return {"id": call_id, **fields}

    monkeypatch.setattr(m, "_repo", lambda: FakeRepo())
    monkeypatch.setattr(m, "_room_has_participants", AsyncMock(return_value=False))
    monkeypatch.setattr(m, "settle", AsyncMock(return_value={}))
    lkapi = _lkapi_dummy()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    cleanup = AsyncMock(return_value=1)
    monkeypatch.setattr(m, "cleanup_dispatch", cleanup)

    out = asyncio.run(m._reap_stale_calls_once())

    assert out["ended"] == 1
    assert out["settled"] == 1
    cleanup.assert_awaited_once_with(lkapi, "call-reap-me")
    lkapi.aclose.assert_awaited_once()


def test_reaper_skips_dispatch_cleanup_without_credentials(monkeypatch):
    """无 LiveKit 凭据（_lkapi_client=None）→ 跳过回收不炸（best-effort）。"""
    from bok_voice_core.types import CallStatus

    from control_plane import main as m

    class FakeRepo:
        def list_calls(self, account_id: str, status: str = ""):
            if status == CallStatus.ACTIVE.value:
                return [{"id": "call-no-creds", "created_at": "2026-09-09T00:00:00+00:00"}]
            return []

        def update_call(self, call_id: str, **fields):
            return {"id": call_id, **fields}

    monkeypatch.setattr(m, "_repo", lambda: FakeRepo())
    monkeypatch.setattr(m, "_room_has_participants", AsyncMock(return_value=False))
    monkeypatch.setattr(m, "settle", AsyncMock(return_value={}))
    monkeypatch.setattr(m, "_lkapi_client", lambda: None)
    cleanup = AsyncMock(return_value=0)
    monkeypatch.setattr(m, "cleanup_dispatch", cleanup)

    out = asyncio.run(m._reap_stale_calls_once())

    assert out["ended"] == 1
    cleanup.assert_not_awaited()
