"""编排审计第二波 FIX 2:预热任务池化 + 可取消（first_turn / close）。

病灶（审计 F1）:TTS prewarm 是无线索的 `create_task(tts_provider.prewarm())`
（FIRE_FORGET_EXEMPT 注释），LLM 前缀预热经 `_spawn_report` 落在 `_report_tasks`
——①挂断收线时 `_close` 的 gather 会为仍在退避重试（5s/12s）的预热白等最多
10s;②无人取消，teardown 掐杀=无痕;③第一轮交付后预热再无消费点，却仍挂着。

修=独立强引用池 `_PREWARM_TASKS`（镜像 `_SETTLE_TASKS` 先例，job 进程一通一命）
+ `_spawn_prewarm`（spawn-and-report 语义同 `_spawn_report`，但落预热池）+
`_cancel_prewarm_tasks(reason)` 两处开火:首个 assistant 轮交付（once 门,
新一通纪元=池空 spawn 时重置）与 `_on_close`（含 SETTLE_WAIT_TIMEOUT 路径）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "apps" / "agent"))

import agent_runtime.agent as agent_mod  # noqa: E402

_SRC = (_REPO / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_pool():
    """池与 once 门逐测清空（模块级账本，跨测试不泄漏）。"""
    agent_mod._PREWARM_TASKS.clear()
    agent_mod._PREWARM_FIRST_TURN_DONE = False
    yield
    for t in list(agent_mod._PREWARM_TASKS):
        t.cancel()
    agent_mod._PREWARM_TASKS.clear()
    agent_mod._PREWARM_FIRST_TURN_DONE = False


async def _slow():
    await asyncio.sleep(60)


# ------------------------------------------------------------------ 池语义


def test_spawn_prewarm_pools_and_discards_on_done():
    async def main():
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(0.01)

        task = agent_mod._spawn_prewarm(work())
        assert task is not None
        assert task in agent_mod._PREWARM_TASKS, "在途预热必须被强引用（GC 掐杀=无痕）"
        await started.wait()
        await asyncio.sleep(0.05)
        assert agent_mod._PREWARM_TASKS == set(), "done-callback 自清，池不无界增长"

    asyncio.run(main())


def test_spawn_prewarm_without_running_loop_is_dropped():
    """无事件循环（纯单测/装配外路径）=就地丢弃，绝不 raise（旧 try/except 语义）。"""

    async def work():  # pragma: no cover - 不会执行
        pass

    assert agent_mod._spawn_prewarm(work()) is None
    assert agent_mod._PREWARM_TASKS == set()


def test_spawn_prewarm_failure_prints_tag_and_discards(capsys):
    async def main():
        async def boom():
            raise RuntimeError("prewarm boom")

        agent_mod._spawn_prewarm(boom())
        await asyncio.sleep(0.05)
        assert agent_mod._PREWARM_TASKS == set()

    asyncio.run(main())
    out = capsys.readouterr().out
    assert "PREWARM_TASK_ERR" in out and "prewarm boom" in out


# ------------------------------------------------------------------ 取消语义


def test_cancel_prewarm_tasks_cancels_pending_and_prints_once(capsys):
    async def main():
        t1 = agent_mod._spawn_prewarm(_slow())
        t2 = agent_mod._spawn_prewarm(_slow())
        await asyncio.sleep(0.01)
        n = await agent_mod._cancel_prewarm_tasks("first_turn")
        assert n == 2
        assert t1.cancelled() and t2.cancelled()
        assert agent_mod._PREWARM_TASKS == set()

    asyncio.run(main())
    out = capsys.readouterr().out
    assert "PREWARM_CANCELLED reason=first_turn n=2" in out
    assert "PREWARM_TASK_ERR" not in out, "取消≠失败，不打错误点"


def test_cancel_with_empty_pool_returns_zero_and_silent(capsys):
    async def main():
        assert await agent_mod._cancel_prewarm_tasks("close") == 0

    asyncio.run(main())
    assert "PREWARM_CANCELLED" not in capsys.readouterr().out


def test_first_turn_cancel_is_once_guarded():
    """once 门:本纪元第二次 first_turn 调用直接短路（在途任务不被二次取消）。

    白盒:任务直入池（不经 `_spawn_prewarm`=不触发新纪元重置），隔离「门」语义。
    """

    async def main():
        assert await agent_mod._cancel_prewarm_tasks("first_turn") == 0  # 空池也置门
        t = asyncio.create_task(_slow())
        agent_mod._PREWARM_TASKS.add(t)
        assert await agent_mod._cancel_prewarm_tasks("first_turn") == 0, "once 门短路"
        assert not t.done(), "门短路=在途任务保持（不重复取消）"
        assert await agent_mod._cancel_prewarm_tasks("close") == 1  # close 无门,收尾

    asyncio.run(main())


def test_spawn_into_empty_pool_starts_new_epoch_and_rearms_guard():
    """新一通纪元（池空时的 spawn）=once 门重置，下一通首轮照样取消自己的预热。"""

    async def main():
        assert await agent_mod._cancel_prewarm_tasks("first_turn") == 0  # 第一通开火过
        t = agent_mod._spawn_prewarm(_slow())  # 第二通装配 spawn（池空=新纪元）
        await asyncio.sleep(0.01)
        assert await agent_mod._cancel_prewarm_tasks("first_turn") == 1, "门应被新纪元重置"
        assert t.cancelled()

    asyncio.run(main())


def test_close_cancel_not_once_guarded():
    async def main():
        agent_mod._spawn_prewarm(_slow())
        await asyncio.sleep(0.01)
        assert await agent_mod._cancel_prewarm_tasks("close") == 1
        t = agent_mod._spawn_prewarm(_slow())
        await asyncio.sleep(0.01)
        assert await agent_mod._cancel_prewarm_tasks("close") == 1, "close 无 once 门"
        assert t.cancelled()

    asyncio.run(main())


def test_cancel_never_raises_on_already_done_tasks():
    async def main():
        async def quick():
            return 1

        agent_mod._spawn_prewarm(quick())
        await asyncio.sleep(0.05)  # 已完成（池已自清）
        assert await agent_mod._cancel_prewarm_tasks("close") == 0

    asyncio.run(main())


# ------------------------------------------------------------------ 源级 pin


def test_source_pin_tts_prewarm_pooled():
    assert "_spawn_prewarm(tts_provider.prewarm())" in _SRC
    assert "create_task(tts_provider.prewarm())" not in _SRC, "裸 create_task 形态不得复活"


def test_source_pin_prefix_prewarm_sites_pooled():
    assert _SRC.count("_spawn_prewarm(_prefix_prewarm_task(") == 2
    assert "_spawn_report(_prefix_prewarm_task(" not in _SRC, "预热不再占 _report_tasks"


def test_source_pin_first_turn_cancel_in_report_assistant_turn():
    i = _SRC.index("async def _report_assistant_turn(")
    seg = _SRC[i : i + 1100]
    # 验收修(2026-10-02 FLOW20 实弹):取消必须收紧到 gen=="llm"——无门的
    # 「首个 assistant 轮」会在开场白/罐头 ack 先行时掐掉在途预热,首个 LLM
    # 轮吃冷 prefill(FIRST_TOKEN_TIMEOUT×2 实弹)。
    assert 'if gen == "llm":' in seg
    assert '_cancel_prewarm_tasks("first_turn")' in seg
    assert "_reply_done_event.set()" in seg  # 仍在函数体开头（chokepoint 不漂移）


def test_source_pin_close_cancel_after_report_gather():
    i = _SRC.index("SETTLE_WAIT_TIMEOUT wait_s=")
    j = _SRC.index("await cp.settle(call_id)", i)
    seg = _SRC[i:j]
    assert '_cancel_prewarm_tasks("close")' in seg, (
        "收线取消必须落在 _report_tasks gather（含超时路径）之后、settle 之前"
    )


def test_fire_forget_lint_still_green():
    """17 波裸 create_task 扫描门禁:新池形态下 agent 运行面仍零 offender。"""
    from test_fire_forget_pool import AGENT_ROOT, _bare_create_task_offenders

    assert _bare_create_task_offenders(AGENT_ROOT) == []
