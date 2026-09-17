"""fire-and-forget task 池化回归(2026-09-17 全量 debug P2-A + P1-A agent 侧)。

背景:事件循环对 task 只持弱引用,GC 可中途回收仍在跑的裸 create_task 任务——
本仓三次实证同 bug 类(_duration_fuse 注释、MiniMax 孤儿 invalidate、F5 结算
丢失)。本文件钉死三件事:

1. 池机制语义:强引用入池→完成自清→失败打点(LEDGER_TASK_ERR);
2. 位点接线:入口点/模块内不再有裸 create_task 目标位点(source 断言,
   镜像 F5 手法——嵌套闭包函数不可直接引用,源码断言是既有折衷);
3. P1-A:B 线 session-report payload 带 worker 来源标识(纯函数直喂)。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re

import pytest

from agent_runtime.interpret import (
    _session_report_payload,
    _spawn_pooled_task,
)


# ---- 1. 池机制语义(_spawn_pooled_task,interpret.py 模块级) ----


def test_spawn_pooled_task_holds_ref_until_done():
    pool: set = set()

    async def main():
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(0.01)

        _spawn_pooled_task(work(), pool, "LEDGER_TASK_ERR")
        assert len(pool) == 1  # 在途任务被强引用,GC 无法回收
        await started.wait()
        await asyncio.sleep(0.05)
        assert len(pool) == 0  # done-callback 自清,池不无界增长

    asyncio.run(main())


def test_spawn_pooled_task_failure_discards_and_prints_tag(capsys):
    pool: set = set()

    async def main():
        async def boom():
            raise RuntimeError("disk full")

        _spawn_pooled_task(boom(), pool, "LEDGER_TASK_ERR")
        await asyncio.sleep(0.05)
        assert len(pool) == 0  # 失败同样自清

    asyncio.run(main())
    out = capsys.readouterr().out
    assert "LEDGER_TASK_ERR" in out
    assert "disk full" in out


def test_spawn_pooled_task_cancel_discards_without_err(capsys):
    pool: set = set()

    async def main():
        async def slow():
            await asyncio.sleep(10)

        _spawn_pooled_task(slow(), pool, "LEDGER_TASK_ERR")
        (task,) = pool
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert len(pool) == 0

    asyncio.run(main())
    assert "LEDGER_TASK_ERR" not in capsys.readouterr().out  # 取消≠失败,不打点


# ---- 2a. interpret 接线(source 断言;闭包函数不可直接引用,F5 手法) ----


def test_interp_entrypoint_routes_ledger_through_pool():
    from agent_runtime import interpret as interp

    src = inspect.getsource(interp.entrypoint)
    # 原文/译文两处账本位点全部经 _spawn_ledger,不再有裸 create_task
    assert "asyncio.create_task(_add_turn(" not in src
    assert src.count("_spawn_ledger(_add_turn(") == 2
    assert '_spawn_pooled_task(coro, _ledger_tasks, "LEDGER_TASK_ERR")' in src


def test_interp_shutdown_flushes_ledger_before_report():
    from agent_runtime import interpret as interp

    src = inspect.getsource(interp.entrypoint)
    # shutdown 前 gather 在途账本行(短超时),且先于 session report/settle
    flush_pos = src.find("asyncio.gather(*list(_ledger_tasks), return_exceptions=True)")
    assert flush_pos != -1
    report_pos = src.find("post_session_report")
    assert report_pos != -1 and report_pos > flush_pos


def test_agent_entrypoint_pools_remaining_sites():
    from agent_runtime import agent as agent_mod

    src = inspect.getsource(agent_mod.entrypoint)
    # 行锚定(^\s+):池化后的赋值形态(_end_task = asyncio.create_task(_end()))
    # 子串上仍含 create_task(_end()),裸语句判定必须锚行首。
    banned = (
        r"_async_update_context\(",
        r"_end\(\)",
        r"_background_flow_judge\(",
        r"cp\.qa_hit\(",
        r"_go\(\)",
        r"_watch\(\)",
        r"_prefix_prewarm_task\(",
    )
    for target in banned:
        pat = re.compile(rf"^\s+asyncio\.create_task\({target}", re.MULTILINE)
        assert not pat.search(src), f"裸 create_task 位点未池化: {target}"
    assert "_spawn_report(_async_update_context(" in src
    # _end 带延时睡眠,只补强引用挂 _SETTLE_TASKS(纯引用池不入 _close 的
    # gather——否则告别后客户提前挂断会干等剩余睡眠)。_end_on_shutdown 兜底不变。
    assert "_SETTLE_TASKS.add(_end_task)" in src
    assert "_spawn_report(_background_flow_judge(" in src
    assert "_spawn_report(cp.qa_hit(" in src
    assert "_spawn_report(_go())" in src
    assert "_spawn_report(_watch())" in src
    assert src.count("_spawn_report(_prefix_prewarm_task(") == 2
    # F5 回归:结算池仍在
    assert agent_mod._SETTLE_TASKS == set() or isinstance(agent_mod._SETTLE_TASKS, set)


# ---- 2b. livekit_plugins 接线(模块级位点) ----


def test_livekit_plugins_pool_wiring():
    from agent_runtime.providers import livekit_plugins as lkp

    assert "_spawn_bg(" in inspect.getsource(lkp._minimax_pool_discard)
    src_partial = inspect.getsource(lkp.Qwen3ASRLiveSTT.set_partial_ms)
    assert "asyncio.ensure_future(" not in src_partial
    assert "_spawn_bg(" in src_partial


class _FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def test_spawn_bg_holds_ref_and_discards():
    from agent_runtime.providers import livekit_plugins as lkp

    ws = _FakeWS()

    async def main():
        lkp._minimax_pool_discard(ws)
        assert lkp._BACKGROUND_TASKS, "弃置关闭任务必须被强引用"
        await asyncio.sleep(0.05)
        assert not lkp._BACKGROUND_TASKS  # 完成自清
        assert ws.closed

    asyncio.run(main())


def test_minimax_pool_discard_without_loop_is_noop():
    # 无事件循环(纯单测)直接放手:原 RuntimeError 守卫语义不变
    from agent_runtime.providers import livekit_plugins as lkp

    lkp._minimax_pool_discard(object())


def test_spawn_bg_discards_after_failure():
    from agent_runtime.providers import livekit_plugins as lkp

    async def main():
        async def boom():
            raise ValueError("x")

        lkp._spawn_bg(boom())
        await asyncio.sleep(0.05)
        assert not lkp._BACKGROUND_TASKS

    asyncio.run(main())


# ---- 3. P1-A: B 线 session-report 带 worker 来源标识 ----


def test_session_report_payload_worker_rev(monkeypatch):
    monkeypatch.setenv("INTERP_DIRECTION", "rev")
    src = {"llm_tokens": 3, "text": "hello"}
    payload = _session_report_payload(src)
    assert payload["worker"] == "bok-interp-rev"
    assert payload["llm_tokens"] == 3
    assert "worker" not in src  # caller 的 dict 不被 mutate


def test_session_report_payload_defaults_fwd(monkeypatch):
    monkeypatch.delenv("INTERP_DIRECTION", raising=False)
    assert _session_report_payload({})["worker"] == "bok-interp-fwd"


def test_session_report_payload_fwd_direction(monkeypatch):
    monkeypatch.setenv("INTERP_DIRECTION", "fwd")
    assert _session_report_payload({})["worker"] == "bok-interp-fwd"


def test_interp_shutdown_wires_worker_payload():
    from agent_runtime import interpret as interp

    src = inspect.getsource(interp.entrypoint)
    assert "_session_report_payload(report.to_dict())" in src
