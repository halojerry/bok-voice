"""Agent dispatch 防重判定与主动回收（官方 livekit.api AgentDispatchService）。

P1 僵尸通话缺陷的可单测内核：
- :func:`has_active_dispatch` — 重派前防重：同 agent_name 的 dispatch 里存在
  PENDING/RUNNING job 即视为活跃（崩溃重派前先查，防止叠加两套 agent）。
- :func:`cleanup_dispatch` — 通话结束后回收：删掉房间内全部 explicit dispatch，
  返回删除数。

两个函数全程 best-effort：LiveKit API 异常只记 warning 不外抛，
防重/回收的失败不应阻断通话创建与收尾主链路。
"""

from __future__ import annotations

from livekit.api import JS_PENDING, JS_RUNNING, LiveKitAPI
from bok_voice_obs.logging import get_logger

log = get_logger("control-plane.dispatch_utils", component="control-plane", service="control-plane")

__all__ = ["cleanup_dispatch", "has_active_dispatch"]


async def has_active_dispatch(lkapi: LiveKitAPI, room: str, agent_name: str = "bok-voice") -> bool:
    """房间内是否已有指定 agent 的活跃 dispatch（job 处于 PENDING/RUNNING）。

    best-effort：list_dispatch 异常记 warning 并返回 False（宁可重派不可不派）。
    """
    try:
        dispatches = await lkapi.agent_dispatch.list_dispatch(room_name=room)
    except Exception as exc:
        log.warning(
            "dispatch_list_failed",
            extra={"event": "dispatch.list.error", "data": {"room": room, "error": str(exc)}},
        )
        return False
    for dispatch in dispatches:
        if dispatch.agent_name != agent_name:
            continue
        for job in dispatch.state.jobs:
            # job.state 是 JobState 消息，活跃性看其 status 枚举（JS_PENDING=0/JS_RUNNING=1）。
            if job.state.status in (JS_PENDING, JS_RUNNING):
                return True
    return False


async def cleanup_dispatch(lkapi: LiveKitAPI, room: str) -> int:
    """删除房间内全部 explicit dispatch，返回实际删除数（含其他 agent 的残留）。

    best-effort：list/delete 任一异常记 warning 继续处理下一个，返回已删数。
    """
    try:
        dispatches = await lkapi.agent_dispatch.list_dispatch(room_name=room)
    except Exception as exc:
        log.warning(
            "dispatch_list_failed",
            extra={"event": "dispatch.list.error", "data": {"room": room, "error": str(exc)}},
        )
        return 0
    deleted = 0
    for dispatch in dispatches:
        try:
            # 注意参数序：delete_dispatch(dispatch_id, room_name)。
            await lkapi.agent_dispatch.delete_dispatch(dispatch_id=dispatch.id, room_name=room)
            deleted += 1
        except Exception as exc:
            log.warning(
                "dispatch_delete_failed",
                extra={
                    "event": "dispatch.delete.error",
                    "data": {"room": room, "dispatch_id": dispatch.id, "error": str(exc)},
                },
            )
    if deleted:
        log.info(
            "dispatch_cleaned",
            extra={"event": "dispatch.cleanup.done", "data": {"room": room, "deleted": deleted}},
        )
    return deleted
