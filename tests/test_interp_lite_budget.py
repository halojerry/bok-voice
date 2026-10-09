"""interp_lite LOC 硬预算门（2026-10-09 立法；蓝图 docs/superpowers/plans/2026-10-09-interp-lite.md §4）。

参照锚：金喜商用同传全 worker 71 模块/12,694 行、translate-channel 域 2,179 行——
薄线全链（编排+适配+guard+账本胶水，不含 tests/）预算 **Σ ≤3000 行、单文件 ≤600 行**。
超标=测试红；调额只能改本文件常量=评审可见（与 doc_anchor 基线棘轮同精神，
但预算是绝对值不是基线——防重新膨胀是本门的唯一职责）。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "apps" / "agent" / "agent_runtime" / "interp_lite"

# 调额=评审认账（PR 里这两个数字的变更就是讨论点）。
BUDGET_TOTAL_LINES = 3000
BUDGET_SINGLE_FILE_LINES = 600


def _py_files() -> list[Path]:
    return sorted(PKG.rglob("*.py"))


def test_budget_total_lines():
    files = _py_files()
    assert files, "interp_lite 包不存在——目录被挪走/改名时本测试应红"
    total = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in files)
    assert total <= BUDGET_TOTAL_LINES, (
        f"interp_lite ΣLOC={total} > 预算 {BUDGET_TOTAL_LINES}——拆文件/删补偿，"
        f"或带证据调额（蓝图 §8 审计表）"
    )


def test_budget_single_file():
    worst = max(_py_files(), key=lambda p: len(p.read_text(encoding="utf-8").splitlines()))
    n = len(worst.read_text(encoding="utf-8").splitlines())
    assert n <= BUDGET_SINGLE_FILE_LINES, (
        f"interp_lite 单文件超限：{worst.name}={n} > {BUDGET_SINGLE_FILE_LINES}——一域一文件纪律"
    )


def test_budget_report():
    """观测面：当前体量快照（不设阈值，跑 -s 可见逐文件行数）。"""
    rows = [
        (str(p.relative_to(PKG)), len(p.read_text(encoding="utf-8").splitlines()))
        for p in _py_files()
    ]
    total = sum(n for _, n in rows)
    print(f"\n[interp-lite budget] total={total}/{BUDGET_TOTAL_LINES}")
    for name, n in sorted(rows, key=lambda r: -r[1]):
        print(f"  {n:>5}  {name}")
