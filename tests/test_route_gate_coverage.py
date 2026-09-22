"""CP 路由闸覆盖门（Mimosa 误阳治理配套，2026-09-23）。

「NestJS 视角扫描器看不见 FastAPI 中间件闸链」的仓库自证：每个路由必须在
``security/route-gates.json`` 登记（生成器 scripts/gen_route_gates.py +
人工审 reviewed/reason），新路由不登记=本测试红——闸链从「审过一次的文档」
变成「CI 守住的资产」。不判「安全」，只判「每条路由有人认领过闸链」。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATABASE_URL", "")  # in-memory repo

from fastapi.routing import APIRoute  # noqa: E402

from control_plane import main as cp  # noqa: E402

MANIFEST = ROOT / "security" / "route-gates.json"


def _app_routes() -> set[str]:
    return {
        getattr(r, "path", "")
        for r in cp.app.routes
        if isinstance(r, APIRoute)
    }


def _manifest() -> dict[str, dict]:
    data = json.loads(MANIFEST.read_text())
    return {r["path"]: r for r in data.get("routes", [])}


def test_every_app_route_is_registered():
    app_paths = _app_routes()
    reg = _manifest()
    missing = sorted(app_paths - reg.keys())
    assert not missing, f"未登记路由（跑 scripts/gen_route_gates.py 并人工审）: {missing}"


def test_no_stale_manifest_entries():
    app_paths = _app_routes()
    reg = _manifest()
    stale = sorted(set(reg.keys()) - app_paths)
    assert not stale, f"清单里已不存在的路由（再生成清掉）: {stale}"


def test_every_entry_reviewed_with_gate_or_exempt_reason():
    reg = _manifest()
    bad = []
    for path, r in sorted(reg.items()):
        if not r.get("reviewed"):
            bad.append(f"{path}: reviewed=false")
        elif not r.get("gates") and not str(r.get("note") or "").startswith("exempt"):
            bad.append(f"{path}: 无闸标记且非豁免")
    assert not bad, f"未完成审定的路由: {bad}"


def test_manifest_covers_all_methods_of_registered_routes():
    app_methods = {}
    for r in cp.app.routes:
        if isinstance(r, APIRoute):
            app_methods.setdefault(getattr(r, "path", ""), set()).update(
                m for m in getattr(r, "methods", []) if m not in ("HEAD", "OPTIONS")
            )
    reg = _manifest()
    drift = []
    for path, methods in sorted(app_methods.items()):
        reg_methods = set(reg.get(path, {}).get("methods") or [])
        if methods != reg_methods:
            drift.append(f"{path}: app={sorted(methods)} manifest={sorted(reg_methods)}")
    assert not drift, f"方法面漂移（再生成刷新）: {drift}"
