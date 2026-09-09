"""Dispatch 工具函数单测：防重判定（has_active_dispatch）+ 主动回收（cleanup_dispatch）。

全部走 mock 的 lkapi（AsyncMock），不连真实 LiveKit；dispatch/job 结构用真实 protobuf
消息构造（livekit.api.AgentDispatch / livekit.protocol.agent.JobState），防 mock 结构漂移。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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
