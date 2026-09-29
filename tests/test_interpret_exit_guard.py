"""B 线 job 退出路径守护（P1.c，spec 2026-09-29 v2 §4）。

病灶（2026-09-29 b8793951 实证）：interp job `process did not exit in time,
killing process`——`_shutdown` 各段（MT 排空/账本落地/report/settle/cp 关闭）
里 report 与 settle 走 CP client（timeout=15s），两段即可挂 30s+ 超过框架
10s 强杀窗。

修复契约：`_exit_stage(name, coro, timeout_s=5.0)`——wait_for 包裹 + 慢段
打点 `interp.exit_slow stage=<n> ms=<t>`（观测定位）+ 超时/取消吞掉（收尾
尽力而为：CP settle 幂等，丢了 job 死后回收器兜底，绝不挂死退出主链）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.interpret import _exit_stage  # noqa: E402


def test_slow_stage_times_out_and_logs(capsys):
    """超时段：wait_for 掐死（不抛）+ exit_slow 打点。"""

    async def hang():
        await asyncio.sleep(30)

    async def main():
        await _exit_stage("settle", hang(), timeout_s=0.05)

    asyncio.run(main())  # 不抛 TimeoutError
    out = capsys.readouterr().out
    assert "interp.exit_slow stage=settle" in out


def test_fast_stage_silent(capsys):
    async def ok():
        return 7

    async def main():
        assert await _exit_stage("report", ok(), timeout_s=5.0) == 7

    asyncio.run(main())
    assert "exit_slow" not in capsys.readouterr().out


def test_cancelled_stage_swallowed():
    """被取消的段不外抛（退出主链不被一段收尾卡死）。"""

    async def boom():
        raise asyncio.CancelledError()

    async def main():
        await _exit_stage("mt_drain", boom(), timeout_s=5.0)

    asyncio.run(main())  # 不抛


def test_exception_stage_swallowed_and_logged(capsys):
    """异常段吞掉 + 打点（收尾尽力而为语义）。"""

    async def boom():
        raise RuntimeError("cp gone")

    async def main():
        await _exit_stage("cp_close", boom(), timeout_s=5.0)

    asyncio.run(main())
    out = capsys.readouterr().out
    assert "interp.exit_slow stage=cp_close" in out
    assert "cp gone" in out
