"""D7 止血:结算 gather 自适应等待窗(2026-09-20,A_LINE_LOGIC §7 D7)。

旧版 `_close` 硬码 10s gather `_report_tasks`;意图判据 `_background_intent_judge`
的 LLM 总预算 timeout=20s(review N9 实测依据)同池——慢判定轮收线时 10s 窗先
到,wait_for 掐死 gather,判定结果静默丢失(只剩一行 REPORT_TASK_ERR)。

修复后:无在途慢任务保持 10s;有(slow_s 登记)抬到 max(10, 预算+5s);
`BOK_SETTLE_WAIT_S` 显式 >0 优先(0/非法=按规则算);超时归因留痕
SETTLE_WAIT_TIMEOUT(wait_s/slow_deadline_s/tasks)。

离线面:模块级纯函数 `_settle_wait_s` + 源码 pin(接线形态)。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime import agent as ag  # noqa: E402

_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py"
).read_text(encoding="utf-8")


def test_wait_default_no_slow_task(monkeypatch):
    """无在途慢任务=基线 10s(快速收线不被拖慢);env 缺省同。"""
    monkeypatch.delenv("BOK_SETTLE_WAIT_S", raising=False)
    assert ag._settle_wait_s(0.0) == 10.0
    assert ag._settle_wait_s() == 10.0


def test_wait_slow_task_gets_budget_plus_margin(monkeypatch):
    """意图判据预算 20s → 25s(20+5 余量);更大的预算取 max(10, 预算+5)。"""
    monkeypatch.delenv("BOK_SETTLE_WAIT_S", raising=False)
    assert ag._settle_wait_s(20.0) == 25.0
    assert ag._settle_wait_s(30.0) == 35.0
    # 预算小于基线时基线兜底(负/零/微小值都不可能把窗压到 10s 以下)
    assert ag._settle_wait_s(3.0) == 10.0
    assert ag._settle_wait_s(-1.0) == 10.0


def test_wait_env_override(monkeypatch):
    """显式 BOK_SETTLE_WAIT_S>0 优先于规则;0=回规则档;非法值=回规则档。"""
    monkeypatch.setenv("BOK_SETTLE_WAIT_S", "40")
    assert ag._settle_wait_s(20.0) == 40.0
    assert ag._settle_wait_s(0.0) == 40.0
    monkeypatch.setenv("BOK_SETTLE_WAIT_S", "0")
    assert ag._settle_wait_s(20.0) == 25.0
    monkeypatch.setenv("BOK_SETTLE_WAIT_S", "abc")
    assert ag._settle_wait_s(20.0) == 25.0
    monkeypatch.setenv("BOK_SETTLE_WAIT_S", "  ")
    assert ag._settle_wait_s(20.0) == 25.0


def test_intent_judge_spawn_tagged_slow():
    """源码 pin:意图判据入池必须带 slow_s=20(它的 LLM 总预算),漏登记=修复失效。"""
    assert "slow_s=20.0" in _SRC
    # slow_s 缺省 0:常规上报(轮次/上下文/垫话)不抬窗——10s 快速档零漂移
    assert ag._settle_wait_s(0.0) == 10.0


def test_close_timeout_not_silent():
    """源码 pin:超时不得回到静默 pass——SETTLE_WAIT_TIMEOUT 必须带归因字段。"""
    assert "SETTLE_WAIT_TIMEOUT" in _SRC
    assert "_task_label" in _SRC
    body = _SRC[_SRC.index("async def _close():") : _SRC.index("def _close_done")]
    assert "await cp.settle(call_id)" in body  # 超时后结算照走(不阻收线)
    assert "slow_deadline_s" in body


def test_task_label_returns_coro_name():
    """归因名可读:普通协程任务取函数名;取不到回退 repr(不抛)。"""

    async def _main() -> str:
        async def _some_report() -> None:
            await asyncio.sleep(0)

        task = asyncio.ensure_future(_some_report())
        try:
            return ag._task_label(task)
        finally:
            task.cancel()

    assert asyncio.run(_main()) == "_some_report"
