"""fire-and-forget 任务强引用池(debug 收编 A-F4,2026-09-18)。

事件循环对 asyncio.Task 只持弱引用——裸 `create_task` 的任务若无人持有,
GC/teardown 可在中途回收("Task was destroyed")=静默丢轮。结算域此前已用
`_SETTLE_TASKS`/`_spawn_report` 的「模块级 set + 完成自清」姿势修过同族问题
(AGENTS.md debug 收编 ⑦);本模块把同款姿势收成单一实现,供剩余调用点收编:

    task = spawn(some_coro(), label="wa-report")

- 入池:`spawn` 创建任务即加入模块级 `_BACKGROUND_TASKS`(强引用,防 GC);
- 自清:done 回调 discard(完成/异常/取消都清,池不积账);
- 打点:异常打印 `BACKGROUND_TASK_ERR` 不外抛——后台任务失败不阻主流程,
  但 never-retrieved 静默会掩盖缺口(REPORT_TASK_ERR 同款口径);
- 取消回滚:有状态要回滚的调用点(如 WA 上报的 `_wa_reported` 键)在协程内
  `except asyncio.CancelledError: 回滚; raise`——CancelledError 是
  BaseException,`except Exception` 接不住,teardown 掐杀时若无独立捕获会令
  状态标记永久占用(补报永不发生)。

测试钉住:tests/test_fire_forget_pool.py(含 apps/agent 裸 create_task 源码
扫描门禁——新调用点必须走 spawn / 持有结果 / 显式豁免标记)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

# 强引用池(模块级):任务完成前一直在池内,GC 无法中途回收;done 回调自清。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def pending_background_tasks() -> int:
    """池内未完成任务数(诊断/测试用)。"""
    return len(_BACKGROUND_TASKS)


def spawn(coro: Coroutine[Any, Any, Any], *, label: str = "") -> asyncio.Task | None:
    """创建 fire-and-forget 任务并入模块级强引用池;无事件循环返回 None。

    对比裸 `asyncio.create_task(coro)`:fire-and-forget 语义不变(不等待、
    不进会话队列),差别只有「入池强引用 + done 自清 + 异常打点」。

    无事件循环(同步测试上下文/loop 已关)时显式关闭协程并返回 None——旧调用
    点各自包 try/except 的兜底由此内聚,调用方不再需要。
    """
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return None
    _BACKGROUND_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            # 打点不外抛:后台任务失败不阻通话,但必须可见——静默 never-retrieved
            # 会掩盖「计数没报/落库没写」这类缺口(REPORT_TASK_ERR 同款)。
            print(f"BACKGROUND_TASK_ERR {label or 'unnamed'} {t.exception()!r}", flush=True)

    task.add_done_callback(_done)
    return task
