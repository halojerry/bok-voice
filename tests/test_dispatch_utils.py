"""Dispatch 工具函数单测：防重判定（has_active_dispatch）+ 主动回收（cleanup_dispatch）。

全部走 mock 的 lkapi（AsyncMock），不连真实 LiveKit；dispatch/job 结构用真实 protobuf
消息构造（livekit.api.AgentDispatch / livekit.protocol.agent.JobState），防 mock 结构漂移。

第二部分是调用点级测试：monkeypatch control_plane.main 里的工具函数与
`_lkapi_client`，钉死 webhook 重派链的行为——
- F1 终态门：ENDED/FAILED/记录已删的死通话不重派（防 cleanup 赛跑输掉后死通话复活）；
- 防重：已有活跃 dispatch → 跳过 create_dispatch（REDISPATCH_SKIP）；无 → 照常创建；
- F2 per-room 锁：并发双 webhook 串行过临界区，并发双 create 坍缩为一次；
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


def _wait_until(pred, what: str, timeout: float = 5.0) -> None:
    """webhook 重派走 create_task fire-and-forget，断言前轮询等后台任务到位。"""
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def _create_call(client: TestClient) -> dict:
    """建一通 RINGING 通话：F1 终态门要求 webhook 房间对应真实非终态通话记录。"""
    return client.post(
        "/api/calls",
        json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
    ).json()


def _post_webhook(client: TestClient, room: str, identity: str = "bok-voice") -> dict:
    return client.post(
        "/api/webhook/livekit",
        json={"event": "participant_left", "room": {"name": room}, "participant": {"identity": identity}},
    ).json()


def test_webhook_redispatch_skipped_when_active_dispatch_exists(monkeypatch):
    """崩溃重派防重：同 agent 已有活跃 dispatch → 不调 create_dispatch + REDISPATCH_SKIP 打点；
    skip 后安排 20s 复查(测试注入 0.2s),复查时 dispatch 仍活跃 → 依旧不补派。"""
    from control_plane import main as m

    monkeypatch.setattr(m, "_REDISPATCH_RECHECK_DELAY", 0.2)
    lkapi = _lkapi_dummy()
    lkapi.agent_dispatch.create_dispatch = AsyncMock()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    has_active = AsyncMock(return_value=True)
    monkeypatch.setattr(m, "has_active_dispatch", has_active)
    skip_log = MagicMock()
    monkeypatch.setattr(m, "control_log", skip_log)

    with TestClient(m.app) as client:
        room = _create_call(client)["id"]
        assert _post_webhook(client, room) == {"handled": True, "redispatch": "bok-voice"}
        _wait_until(lambda: skip_log.info.call_count == 1, "redispatch skip log")
        # F5: aclose 纳入同一轮询等待,不依赖「后台任务已跑完」的调度假设。
        _wait_until(lambda: lkapi.aclose.await_count == 1, "client aclose")
        # 复查任务(0.2s 后)跑完:dispatch 仍活跃 → 依旧 skip,不补派。
        _wait_until(lambda: has_active.await_count >= 2, "recheck has_active")

    assert all(c.args == (lkapi, room) for c in has_active.await_args_list)
    lkapi.agent_dispatch.create_dispatch.assert_not_awaited()
    assert skip_log.info.call_count == 1
    assert "redispatch_skip" in str(skip_log.info.call_args.args[0])
    assert skip_log.info.call_args.kwargs["extra"]["event"] == "dispatch.redispatch.skip"
    assert skip_log.info.call_args.kwargs["extra"]["data"]["room"] == room


def test_webhook_redispatch_recheck_creates_when_dispatch_gone(monkeypatch):
    """真崩溃场景(实机实证 2026-09-10):强杀瞬间 job 仍 RUNNING → 首查 skip;
    20s 复查(测试注入 0.2s)时 dispatch 已被 livekit 判死消失 → 补派。"""
    from control_plane import main as m

    monkeypatch.setattr(m, "_REDISPATCH_RECHECK_DELAY", 0.2)
    lkapi = _lkapi_dummy()
    create = AsyncMock(return_value=AgentDispatch())
    lkapi.agent_dispatch.create_dispatch = create
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    has_active = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(m, "has_active_dispatch", has_active)
    skip_log = MagicMock()
    monkeypatch.setattr(m, "control_log", skip_log)

    with TestClient(m.app) as client:
        room = _create_call(client)["id"]
        assert _post_webhook(client, room) == {"handled": True, "redispatch": "bok-voice"}
        _wait_until(lambda: skip_log.info.call_count == 1, "redispatch skip log")
        # 复查(0.2s)必须在 TestClient 上下文内等完——F5 同款教训:上下文退出
        # 会取消 app 后台任务。
        _wait_until(lambda: create.await_count == 1, "recheck create_dispatch")

    create.assert_awaited_once_with(room=room, agent_name="bok-voice")
    assert has_active.await_count == 2


def test_webhook_redispatch_matches_sdk_agent_identity(monkeypatch):
    """实机实证(2026-09-10)：agents SDK 真实 job 入房 identity 是 agent-<jobid>
    （livekit/agents job.py:1018 服务端 job token 签发，非 agent_name bok-voice）——
    webhook 门必须认 agent- 前缀，否则崩溃补位永不触发。"""
    from control_plane import main as m

    monkeypatch.setattr(m, "_REDISPATCH_RECHECK_DELAY", 0.2)
    lkapi = _lkapi_dummy()
    lkapi.agent_dispatch.create_dispatch = AsyncMock()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    has_active = AsyncMock(return_value=True)
    monkeypatch.setattr(m, "has_active_dispatch", has_active)
    skip_log = MagicMock()
    monkeypatch.setattr(m, "control_log", skip_log)

    with TestClient(m.app) as client:
        room = _create_call(client)["id"]
        # 真实形态：identity = "agent-" + job id（如 agent-AJ_aZTC3G9UvEuy）
        # 端点响应回显 identity（create_dispatch 仍按 agent_name="bok-voice" 重派）
        assert _post_webhook(client, room, identity="agent-AJ_aZTC3G9UvEuy") == {
            "handled": True,
            "redispatch": "agent-AJ_aZTC3G9UvEuy",
        }
        _wait_until(lambda: skip_log.info.call_count == 1, "redispatch skip log")

    lkapi.agent_dispatch.create_dispatch.assert_not_awaited()
    assert has_active.await_count >= 1


def test_webhook_redispatch_creates_when_no_active_dispatch(monkeypatch):
    """无活跃 dispatch → 照常 create_dispatch（防重不误伤正常重派）。"""
    from control_plane import main as m

    lkapi = _lkapi_dummy()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    monkeypatch.setattr(m, "has_active_dispatch", AsyncMock(return_value=False))
    create = AsyncMock(return_value=AgentDispatch())
    lkapi.agent_dispatch.create_dispatch = create

    with TestClient(m.app) as client:
        room = _create_call(client)["id"]
        _post_webhook(client, room)
        _wait_until(lambda: create.await_count == 1, "create_dispatch")
        _wait_until(lambda: lkapi.aclose.await_count == 1, "client aclose")

    create.assert_awaited_once_with(room=room, agent_name="bok-voice")
    lkapi.aclose.assert_awaited_once()


def test_webhook_redispatch_skipped_when_call_ended(monkeypatch):
    """F1 防复活：ENDED 死通话不重派——cleanup 赢了赛跑也不许给死通话新建 dispatch。"""
    from bok_voice_core.types import CallStatus

    from control_plane import main as m

    lkapi = _lkapi_dummy()
    lkapi.agent_dispatch.create_dispatch = AsyncMock()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    has_active = AsyncMock(return_value=False)
    monkeypatch.setattr(m, "has_active_dispatch", has_active)
    skip_log = MagicMock()
    monkeypatch.setattr(m, "control_log", skip_log)

    with TestClient(m.app) as client:
        room = _create_call(client)["id"]
        m._repo().update_call(room, status=CallStatus.ENDED.value)
        _post_webhook(client, room)
        _wait_until(lambda: skip_log.info.call_count == 1, "redispatch skip log")

    has_active.assert_not_awaited()  # 终态门在防重检查之前，死通话不值得发 LiveKit 请求
    lkapi.agent_dispatch.create_dispatch.assert_not_awaited()
    assert skip_log.info.call_args.kwargs["extra"]["data"]["reason"] == "call_ended"


def test_webhook_redispatch_skipped_when_call_record_missing(monkeypatch):
    """F1 同罪：通话记录不存在（已删/幻影房间）同样不重派，reason=call_not_found。"""
    from control_plane import main as m

    lkapi = _lkapi_dummy()
    lkapi.agent_dispatch.create_dispatch = AsyncMock()
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    monkeypatch.setattr(m, "has_active_dispatch", AsyncMock(return_value=False))
    skip_log = MagicMock()
    monkeypatch.setattr(m, "control_log", skip_log)

    with TestClient(m.app) as client:
        assert _post_webhook(client, "room-phantom") == {"handled": True, "redispatch": "bok-voice"}
        _wait_until(lambda: skip_log.info.call_count == 1, "redispatch skip log")

    lkapi.agent_dispatch.create_dispatch.assert_not_awaited()
    assert skip_log.info.call_args.kwargs["extra"]["data"]["reason"] == "call_not_found"


def test_webhook_concurrent_redispatch_serialized_by_per_room_lock(monkeypatch):
    """F2 TOCTOU：per-room 锁串行化「检查+创建」——并发双 webhook 只 create 一次。

    判别器是检查的「进入时刻」快照 observed：无锁时 T2 必然在 T1 的 0.2s 检查
    窗口内就进入（observed=[0,0]，create 未发生，本测试失败）；有锁时 T2 被锁
    挡到 T1 create 之后才进入（observed=[0,1]）→ 复查见活跃 → 只 create 一次。
    """
    from control_plane import main as m

    lkapi = _lkapi_dummy()
    create = AsyncMock(return_value=AgentDispatch())
    lkapi.agent_dispatch.create_dispatch = create
    monkeypatch.setattr(m, "_lkapi_client", lambda: lkapi)
    monkeypatch.setattr(m, "_REDISPATCH_RECHECK_DELAY", 0.2)

    observed: list[int] = []

    async def fake_has_active(_lkapi, room, agent_name="bok-voice"):
        observed.append(create.await_count)  # 进入检查那一刻 create 已完成数
        await asyncio.sleep(0.2)  # 拉长检查窗口制造无锁重叠机会
        return create.await_count > 0  # 已有任何 create 完成 → 视为同 agent 活跃 dispatch

    monkeypatch.setattr(m, "has_active_dispatch", fake_has_active)

    with TestClient(m.app) as client:
        room = _create_call(client)["id"]
        _post_webhook(client, room)
        _post_webhook(client, room)
        # 完成信号：两任务各自 aclose 一次（第二个任务只 skip 不 create）。
        _wait_until(lambda: lkapi.aclose.await_count == 2, "both redispatch tasks finished")

    assert observed[:2] == [0, 1]  # 第二次检查被锁串行到第一次 create 之后(recheck 尾查可能追加)
    assert create.await_count == 1


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
