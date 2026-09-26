from __future__ import annotations

import os
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
):
    path = ROOT / part
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# 结算闲时门（2026-09-25 车道卫生）测试面全局关闭：settle 相关测试的临时库里
# 常有 ACTIVE/RINGING 状态的通话行，默认 300s 等待会把每个 settle 测试拖成
# 5 分钟级。门的时间语义由 test_settle_idle_gate.py 单独钉；需要开门的测试
# 显式 setenv 覆盖本 setdefault 即可。
os.environ.setdefault("BOK_SETTLE_IDLE_WAIT_S", "0")
