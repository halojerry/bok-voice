"""Agent dispatch 防重判定与主动回收（官方 livekit.api AgentDispatchService）。

P1 僵尸通话缺陷的可单测内核：
- :func:`has_active_dispatch` — 重派前防重：同 agent_name 的 dispatch 里存在
  PENDING/RUNNING job 即视为活跃（崩溃重派前先查，防止叠加两套 agent）。
- :func:`cleanup_dispatch` — 通话结束后回收：删掉房间内全部 explicit dispatch，
  返回删除数。
- :func:`list_dispatches_safe` — `list_dispatch` 的 tri-state 包装（成功=列表 /
  异常=None），供需要区分「无在派」与「状态未知」的调用点（看门狗）使用。

has_active_dispatch / cleanup_dispatch / has_dispatch_record 全程 best-effort：
LiveKit API 异常只记 warning 不外抛，防重/回收的失败不应阻断通话创建与收尾主链路。
`list_dispatches_safe` 是例外——它把「状态未知」显式交还给调用方决策（看门狗据此
跳过本轮补派，而不是静默按「无在派」重建）。
"""

from __future__ import annotations

import time

from livekit.api import JS_PENDING, JS_RUNNING, LiveKitAPI
from bok_voice_obs.logging import get_logger

log = get_logger("control-plane.dispatch_utils", component="control-plane", service="control-plane")

# 僵尸判龄下限（秒）：agent 冷启动实测 ~3s（LOAD 压测 ≤8s），带 created_at 的
# dispatch 超过此龄仍无 agent 入房 = job 僵死，可安全清扫重建。
DISPATCH_ZOMBIE_MIN_AGE_S = 30.0

__all__ = [
    "cleanup_dispatch",
    "dispatch_is_zombie",
    "has_active_dispatch",
    "has_dispatch_record",
    "has_dispatched_before",
    "list_dispatches_safe",
    "mark_dispatch_alive",
]


# ever_dispatched 账本（2026-09-29 生命周期治本 P1.b，spec §4）：room → agent
# 曾真实入房。看门狗只救「从未入房」的冷启动窗；job 入房过再退出（正常结束/
# 崩溃）= 本通 agent 生命周期已尽，永不复活——复活=接手客户已放弃的空房
# 空转到回收器（2026-09-29 三例 job1 退→3s 补 job2 实证：803e44ad/8c25aa8a/
# 9f2ff7a3，机制=job 正常结束→dispatch 变全终态空壳→被 dispatch_is_zombie
# 清扫删除→存在性判据失效→看门狗判「丢派发」补位）。进程内存态：CP 重启清零
# 保守（重启后对在途通话失去记忆，最坏回到旧补派行为，不劣化）。
_EVER_DISPATCHED: set[str] = set()


def mark_dispatch_alive(room: str) -> None:
    """agent 真实入房时烧标记（看门狗 recovered 判定处调用）。"""
    if room:
        _EVER_DISPATCHED.add(room)


def has_dispatched_before(room: str) -> bool:
    """本房是否有过真实入房的 agent（True=生命周期已尽，永不补派）。"""
    return room in _EVER_DISPATCHED


async def list_dispatches_safe(lkapi: LiveKitAPI, room: str) -> list | None:
    """`list_dispatch` 的 tri-state 包装：成功返回 dispatch 列表（可空），异常返回 None。

    与三处 best-effort 调用点的关键区别：None 明确表达「状态未知」——调用方据此可
    选择保守动作（看门狗跳过本轮补派并下轮重试），而不是把异常静默压成「无 dispatch」。
    2026-09 实证（dispatch_list_failed 628× / ≥52 通 active 被误回收）：LiveKit 瞬断
    下 best-effort False 会静默卸掉防重闸，看门狗照建第二个 job。
    """
    try:
        return await lkapi.agent_dispatch.list_dispatch(room_name=room)
    except Exception as exc:
        log.warning(
            "dispatch_list_failed",
            extra={"event": "dispatch.list.error", "data": {"room": room, "error": str(exc)}},
        )
        return None


def _live_job(d) -> bool:
    jobs = getattr(getattr(d, "state", None), "jobs", None) or []
    for job in jobs:
        if getattr(getattr(job, "state", None), "status", None) in (JS_PENDING, JS_RUNNING):
            return True
    return False


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
        if _live_job(dispatch):
            return True
    return False


async def has_dispatch_record(lkapi: LiveKitAPI, room: str, agent_name: str = "bok-voice") -> bool:
    """房间内是否存在指定 agent 的 **任意** dispatch 记录（不看 job 状态）。

    2026-09-28 双派发实证（call-0105a539）：room-config dispatch 的 job 在
    「已派发、agent 冷启动未入房」窗口不反映 PENDING/RUNNING——job-state 判据
    必误判「无在派」，看门狗照建第二个 job。存在性才是「已在派」的可靠信号：
    一条 dispatch 记录从创建到显式删除全程在场。best-effort 同 has_active_dispatch。
    """
    try:
        dispatches = await lkapi.agent_dispatch.list_dispatch(room_name=room)
    except Exception as exc:
        log.warning(
            "dispatch_list_failed",
            extra={"event": "dispatch.list.error", "data": {"room": room, "error": str(exc)}},
        )
        return False
    return any(d.agent_name == agent_name for d in dispatches)


def dispatch_is_zombie(d, *, attempt: int, now_s: float | None = None) -> bool:
    """单个 dispatch 是否可安全清扫（agent 缺席由调用点前置守卫保证）。

    - job 全终态/为空 → 空壳，可清。
    - 有活跃 job 且带 created_at：agent 冷启动 ~3s、LOAD ≤8s，超过
      `DISPATCH_ZOMBIE_MIN_AGE_S`(30s) 仍缺席 = 僵死，可清；龄内绝不动
      （2026-09-28 实证：2s 复查轮删掉的正是冷启动中的活派发）。
    - 有活跃 job 但无 created_at（旧服务端/假体，无从判龄）：复查轮
      （attempt>0，即 agent 已缺席 ≥首段 grace）按旧 M-27 僵尸形状清扫——
      首_attempt 永不清，防重优先。
    """
    if not _live_job(d):
        return True
    created = float(getattr(d, "created_at", 0) or 0)
    if created > 0:
        return (now_s if now_s is not None else time.time()) - created >= DISPATCH_ZOMBIE_MIN_AGE_S
    return attempt > 0


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
