"""fire-and-forget 任务收编回归门禁(2026-09-18,A-F4 收编)。

背景:裸 `create_task` 的任务只被事件循环弱引用,GC/teardown 可在中途掐掉
("Task was destroyed")=静默丢轮。结算域与 2026-09-17 全量 debug F4/P2-A 已把
apps/agent 全部调用点收编进强引用池(`_spawn_report`/`_SETTLE_TASKS`/
`_spawn_pooled_task`/`_spawn_bg`)。本档钉住两件不随实现漂移的事:

- 源码扫描:apps/agent 不得再有「结果不持有」的裸 create_task——新调用点必须
  走既有池化 helper / 持有结果 / 带 FIRE_FORGET_EXEMPT: 标记(test_cantonese_
  terminology 同款全仓扫描口径);
- WA 上报 `_report_whatsapp_once` 的取消/失败回滚语义:CancelledError 是
  BaseException,`except Exception` 接不住——teardown 掐杀在途请求时若无独立
  捕获回滚,`_wa_reported` 键被永久占用=这个号永远不再补报(AGENTS.md ⑦)。
  上报体从 entrypoint 内联闭包抽出为模块级函数,离线单测钉死。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import pytest  # noqa: E402


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
    """失败路径:回滚键 + 打点(server 幂等,后续轮补报)。"""

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


def test_wa_report_site_wires_module_function():
    """entrypoint 的 WA 上报调用点必须走 _report_whatsapp_once(语义单测才有意义)。
    防内联闭包复活:闭包版行为相同但不可测,F4 语义会重新退化成无钉状态。"""
    src = (Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime"
           / "agent.py").read_text(encoding="utf-8")
    assert src.count("_report_whatsapp_once(") >= 2  # 定义 + 调用点
    call_site = src[src.index("async def entrypoint"):]
    assert "_report_whatsapp_once(" in call_site


# ---------------------------------------------------------------------------
# 源码扫描:apps/agent 不得再有裸 fire-and-forget create_task
# ---------------------------------------------------------------------------

AGENT_ROOT = Path(__file__).resolve().parents[1] / "apps" / "agent"
_ASSIGN_RE = re.compile(r"(?:^|[^=!<>+\-*/%])=(?!=)")
_EXEMPT_MARKER = "FIRE_FORGET_EXEMPT:"


def _bare_create_task_offenders(root: Path) -> list[str]:
    """扫目录内全部源码:每个 create_task 调用点必须「结果被持有」
    (赋值/入池/列表)或带 FIRE_FORGET_EXEMPT: 标记(行内/上一行注释均可)。"""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#") or "create_task(" not in line:
                continue
            prev = lines[lineno - 2].strip() if lineno >= 2 else ""
            if _EXEMPT_MARKER in line or _EXEMPT_MARKER in prev:
                continue
            before = line.split("create_task(")[0]
            if _ASSIGN_RE.search(before):
                continue
            rel = path.relative_to(root.parent).as_posix()
            offenders.append(f"{rel}:{lineno}: {stripped}")
    return offenders


def test_no_bare_fire_and_forget_in_agent_runtime():
    offenders = _bare_create_task_offenders(AGENT_ROOT)
    assert not offenders, (
        "裸 create_task(结果不持有)只被事件循环弱引用,GC/teardown 中途可掐掉=静默丢轮。"
        "改用既有池化 helper(_spawn_report/_SETTLE_TASKS/_spawn_pooled_task/_spawn_bg)"
        f"或持有结果;确属有意 detach 的加 FIRE_FORGET_EXEMPT: 标记说明理由:\n"
        + "\n".join(offenders)
    )


def test_scanner_catches_bare_call(tmp_path):
    """扫描器本身要能抓裸调用(防扫描器退化成恒绿);持有/豁免形态正确放行。"""

    bare = tmp_path / "x.py"
    bare.write_text("async def f():\n    asyncio.create_task(g())\n", encoding="utf-8")
    held = tmp_path / "y.py"
    held.write_text("async def f():\n    t = asyncio.create_task(g())\n", encoding="utf-8")
    exempt = tmp_path / "z.py"
    exempt.write_text(
        "async def f():\n"
        "    asyncio.create_task(g())  # FIRE_FORGET_EXEMPT: held downstream\n",
        encoding="utf-8",
    )

    def scan(root: Path) -> list[str]:
        out = []
        for path in sorted(root.rglob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for lineno, line in enumerate(lines, start=1):
                stripped = line.strip()
                if stripped.startswith("#") or "create_task(" not in line:
                    continue
                prev = lines[lineno - 2].strip() if lineno >= 2 else ""
                if _EXEMPT_MARKER in line or _EXEMPT_MARKER in prev:
                    continue
                before = line.split("create_task(")[0]
                if _ASSIGN_RE.search(before):
                    continue
                out.append(f"{path.name}:{lineno}")
        return out

    assert scan(tmp_path) == ["x.py:2"]
