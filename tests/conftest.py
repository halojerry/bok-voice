from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for part in (
    "packages/core",
    "packages/business-db",
    "packages/knowledge",
    "packages/observability",
    "apps/control-plane",
    "apps/agent",
    # scripts/（2026-09-23，SSRF 守卫接线）：按路径 exec_module 加载探针脚本的
    # 测试（test_load_cp_db_reservation 等）没有 scripts/ 在 sys.path——脚本侧
    # `from urlguard_gate import gate` 会 ModuleNotFoundError。此处单点补齐
    # （直接 `python scripts/x.py` 运行时 sys.path[0] 本就是 scripts/，零影响）。
    "scripts",
):
    path = ROOT / part
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
