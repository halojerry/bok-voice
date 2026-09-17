"""fire-and-forget 任务强引用池(task_pool)与调用点收编单测。

debug 收编 A-F4 缓项(2026-09-18):裸 `create_task` 的任务只被事件循环弱引用,
GC/teardown 可在中途掐掉("Task was destroyed")=静默丢轮。结算域此前已用
`_SETTLE_TASKS`/`_spawn_report` 的「模块级 set + 完成自清」姿势修过同族问题
(AGENTS.md debug 收编 ⑦);本档钉死剩余调用点的收编:

- spawn 的任务在完成前一直被模块级 `_BACKGROUND_TASKS` 持有,完成后自清;
- 异常任务打点 BACKGROUND_TASK_ERR 不外抛、照样自清;
- 无事件循环(同步测试上下文)spawn 返回 None 且协程被关闭(不炸不警告);
- 取消路径:CancelledError 是 BaseException,`except Exception` 接不住——
  WA 上报的取消路径必须回滚 `_wa_reported` 键(否则该号永远不再补报);
- 源码扫描:apps/agent 内不得再有「结果不持有」的裸 create_task
  (test_cantonese_terminology 同款全仓扫描口径)。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import pytest  # noqa: E402

from agent_runtime import task_pool  # noqa: E402


# ---------------------------------------------------------------------------
# task_pool.spawn 基础语义
# ---------------------------------------------------------------------------


def test_spawn_holds_task_until_done_then_discards():
    """完成前一直被模块级强引用池持有(裸 create_task 的根因=只有弱引用)。"""

    async def main():
        started = asyncio.Event()
        release = asyncio.Event()

        async def work():
            started.set()
            await release.wait()

        task = task_pool.spawn(work(), label="held")
        assert task is not None
        await started.wait()
        assert task in task_pool._BACKGROUND_TASKS
        release.set()
        await task
        for _ in range(3):  # 让 done 回调(自清)在循环里跑完
            await asyncio.sleep(0)
        assert task not in task_pool._BACKGROUND_TASKS

    asyncio.run(main())


def test_spawn_discards_after_exception_and_logs(capsys):
    """异常任务打点不外抛(fire-and-forget 失败不阻主流程)且照样自清。"""

    async def main():
        async def boom():
            raise RuntimeError("boom-x")

        task = task_pool.spawn(boom(), label="boom-label")
        assert task is not None
        with pytest.raises(RuntimeError):
            await task
        for _ in range(3):
            await asyncio.sleep(0)
        assert task not in task_pool._BACKGROUND_TASKS
        assert "BACKGROUND_TASK_ERR boom-label" in capsys.readouterr().out

    asyncio.run(main())


def test_spawn_discards_after_cancel():
    """取消路径同样自清——池不积账(取消不是池要保的东西,只是别中途 GC)。"""

    async def main():
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.sleep(60)

        task = task_pool.spawn(hang(), label="hang")
        assert task is not None
        await started.wait()
        assert task in task_pool._BACKGROUND_TASKS
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(3):
            await asyncio.sleep(0)
        assert task not in task_pool._BACKGROUND_TASKS

    asyncio.run(main())


def test_spawn_without_loop_returns_none_and_closes_coro():
    """无事件循环(测试/同步上下文)安全:返回 None 且协程被关闭,不留
    never-awaited 警告——旧调用点的 try/except 兜底由此内聚进 spawn。"""

    async def work():
        pytest.fail("coro must not run without a loop")

    coro = work()
    assert inspect.getcoroutinestate(coro) == "CORO_CREATED"
    assert task_pool.spawn(coro, label="noloop") is None
    assert inspect.getcoroutinestate(coro) == "CORO_CLOSED"


# ---------------------------------------------------------------------------
# WA 上报取消/失败回滚(_wa_reported 键)
# ---------------------------------------------------------------------------


def test_wa_report_cancelled_rolls_back_reported_key():
    """teardown 掐杀走 CancelledError(BaseException,except Exception 接不住):
    若无独立捕获回滚,键被永久占用=这个号永远不再补报(AGENTS.md ⑦)。"""

    from agent_runtime.agent import _report_whatsapp_once

    reported = {"852-64325432"}

    class _SlowCP:
        async def report_whatsapp(self, call_id, num, channel=""):
            await asyncio.sleep(60)  # 模拟请求在途时被 teardown 掐杀

    async def main():
        task = asyncio.create_task(
            _report_whatsapp_once(
                _SlowCP(), "call-x", "852-64325432", "whatsapp", reported, "852-64325432"
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "852-64325432" not in reported  # 键回滚:后续轮再侦测到可补报

    asyncio.run(main())


def test_wa_report_failure_rolls_back_and_logs(capsys):
    """失败路径维持旧行为:回滚键 + 打点(server 幂等,后续轮补报)。"""

    from agent_runtime.agent import _report_whatsapp_once

    reported = {"k1"}

    class _FailingCP:
        async def report_whatsapp(self, call_id, num, channel=""):
            raise RuntimeError("cp down")

    async def main():
        await _report_whatsapp_once(_FailingCP(), "c1", "1234", "whatsapp", reported, "k1")

    asyncio.run(main())
    assert "k1" not in reported
    assert "[whatsapp] report failed" in capsys.readouterr().out


def test_wa_report_success_keeps_key(capsys):
    """成功路径:键留在 reported(去重),无失败打点。"""

    from agent_runtime.agent import _report_whatsapp_once

    reported = {"k2"}
    seen: list[tuple[str, str, str]] = []

    class _OkCP:
        async def report_whatsapp(self, call_id, num, channel=""):
            seen.append((call_id, num, channel))

    async def main():
        await _report_whatsapp_once(_OkCP(), "c1", "6432", "whatsapp", reported, "k2")

    asyncio.run(main())
    assert seen == [("c1", "6432", "whatsapp")]
    assert reported == {"k2"}
    assert "[whatsapp] report failed" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 源码扫描:apps/agent 不得再有裸 fire-and-forget create_task
# ---------------------------------------------------------------------------

AGENT_ROOT = Path(__file__).resolve().parents[1] / "apps" / "agent"
_ASSIGN_RE = re.compile(r"(?:^|[^=!<>+\-*/%])=(?!=)")
_EXEMPT_MARKER = "FIRE_FORGET_EXEMPT:"


def _bare_create_task_offenders() -> list[str]:
    """扫 apps/agent 全部源码:每个 create_task 调用点必须「结果被持有」
    (赋值/入池/列表)或带 FIRE_FORGET_EXEMPT: 标记;task_pool.py 是唯一包装点。
    """
    offenders: list[str] = []
    for path in sorted(AGENT_ROOT.rglob("*.py")):
        rel = path.relative_to(AGENT_ROOT.parent).as_posix()
        if rel.endswith("task_pool.py"):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or "create_task(" not in line:
                continue
            if _EXEMPT_MARKER in line:
                continue
            before = line.split("create_task(")[0]
            if _ASSIGN_RE.search(before):
                continue
            offenders.append(f"{rel}:{lineno}: {stripped}")
    return offenders


def test_no_bare_fire_and_forget_in_agent_runtime():
    offenders = _bare_create_task_offenders()
    assert not offenders, (
        "裸 create_task(结果不持有)只被事件循环弱引用,GC/teardown 中途可掐掉=静默丢轮。"
        "改用 agent_runtime.task_pool.spawn() 入模块级强引用池;确属有意 detach 的"
        f"加 FIRE_FORGET_EXEMPT: 标记说明理由:\n" + "\n".join(offenders)
    )


def test_scanner_catches_bare_call(tmp_path):
    """扫描器本身要能抓裸调用(防扫描器退化成恒绿)。"""

    bare = tmp_path / "x.py"
    bare.write_text("async def f():\n    asyncio.create_task(g())\n", encoding="utf-8")
    held = tmp_path / "y.py"
    held.write_text("async def f():\n    t = asyncio.create_task(g())\n", encoding="utf-8")

    def scan(root: Path) -> list[str]:
        out = []
        for path in sorted(root.rglob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#") or "create_task(" not in line:
                    continue
                before = line.split("create_task(")[0]
                if _ASSIGN_RE.search(before):
                    continue
                out.append(f"{path.name}:{lineno}")
        return out

    assert scan(tmp_path) == ["x.py:2"]
