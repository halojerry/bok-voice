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
):
    path = ROOT / part
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
