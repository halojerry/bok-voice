"""生成 security/route-gates.json：枚举 CP 全部 FastAPI 路由 + 源码闸标记提取。

维护工具（Mimosa 误阳治理配套，2026-09-23）：把「NestJS 视角扫描器看不见
FastAPI 中间件闸链」变成仓库自证资产——

  python scripts/gen_route_gates.py            # 生成/刷新 security/route-gates.json
  pytest tests/test_route_gate_coverage.py     # CI 门：未登记路由=红

提取启发式（inspect.getsource 正则，保守漏报优于误报）：
  _gate_page(        → page 闸（页面权限键）
  require_role(      → role 闸
  auto_gate_management( → mgmt-auto（管理面下发制）
  _gate_management(  → mgmt（root/机器面）
  _gate_intent_rule_row( → row 闸（by-ID 404→角色闸）
  deny_cross_account / deny_foreign_owner → account 闸
  scoped_account(    → 账号收窄（列表过滤）
都看不见 → identity-gate-only（全局 identity_gate 中间件仍罩着；生成后须
人工逐条补 reason 才算 reviewed——测试只认 reviewed=true 的条目）。
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "control-plane"))
for part in ("packages/core", "packages/business-db", "packages/knowledge", "packages/observability"):
    p = ROOT / part
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

OUT = ROOT / "security" / "route-gates.json"

_MARKERS: list[tuple[str, str]] = [
    (r"\b_gate_page\s*\(", "page"),
    (r"\brequire_role\s*\(", "role"),
    (r"\bauto_gate_management\s*\(", "mgmt-auto"),
    (r"\b_gate_management\s*\(", "mgmt"),
    (r"\b_gate_intent_rule_row\s*\(", "row-gate"),
    (r"\bdeny_cross_account\b", "account"),
    (r"\bdeny_foreign_owner\b", "owner"),
    (r"\bscoped_account\s*\(", "scoped-account"),
    # handler 内自证/委托形态（2026-09-23 实审补齐：闸在但不在标记面）
    (r"\b_require_user_admin\s*\(", "user-admin"),
    (r"\b_campaign_transition\s*\(", "campaign-transition(page+account)"),
    (r"\bresolve_node_token\s*\(", "node-token"),
    (r"\bcurrent_identity\s*\(", "identity-self"),
]

# 全局豁免面（identity_gate 的 _EXEMPT_PATHS 家族 + 机器通道特性端点）
_EXEMPT = {
    "/health": "存活探针，无敏感数据",
    "/api/auth/login": "登录端点（本身即凭据交换）",
    "/api/nodes/heartbeat": "节点心跳（指纹绑定在 handler 内）",
    "/api/nodes/register": "节点注册（指纹+license 在 handler 内）",
    "/api/webhook/livekit": "LiveKit 签名 webhook（外部回调面）",
}


def _gates_of(func) -> list[str]:
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return []
    gates: list[str] = []
    for pat, name in _MARKERS:
        if re.search(pat, src):
            gates.append(name)
    return gates


def main() -> int:
    from fastapi.routing import APIRoute

    from control_plane import main as cp

    # 同 path 多装饰器（GET/POST 分列 @app.get/@app.post）=多个 APIRoute 对象
    # ——按 path 聚合成一行：方法并集、闸并集、handler 记逗连。
    merged: dict[str, dict] = {}
    for route in cp.app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = getattr(route, "path", "")
        methods = sorted(m for m in getattr(route, "methods", []) if m not in ("HEAD", "OPTIONS"))
        func = getattr(route, "endpoint", None)
        name = getattr(func, "__name__", "")
        gates = _gates_of(func) if func else []
        row = merged.setdefault(path, {
            "path": path, "methods": [], "handlers": [], "gates": [], "note": "",
        })
        for m in methods:
            if m not in row["methods"]:
                row["methods"].append(m)
        row["methods"].sort()
        if name and name not in row["handlers"]:
            row["handlers"].append(name)
        for g in gates:
            if g not in row["gates"]:
                row["gates"].append(g)
        if not row["note"]:
            if not gates and path in _EXEMPT:
                row["note"] = f"exempt:{_EXEMPT[path]}"
            elif not gates:
                row["note"] = "identity-gate-only"
    rows: list[dict] = []
    for path, r in merged.items():
        r["handlers"] = ",".join(r["handlers"])
        r["reviewed"] = bool(r["gates"])
        r["reason"] = ""
        rows.append(r)
    rows.sort(key=lambda r: r["path"])
    # 保留人工审：同 path 的既有 reviewed/reason 不被再生覆盖——启发式字段
    # （gates/note/handlers）刷新，人工判定只增不减。
    prev: dict[str, dict] = {}
    if OUT.is_file():
        try:
            for r in json.loads(OUT.read_text()).get("routes", []):
                old = prev.get(r["path"])
                if old is None:
                    prev[r["path"]] = dict(r)
                else:  # 旧版按方法分行的清单：聚并 reviewed/reason
                    old["reviewed"] = old.get("reviewed") or r.get("reviewed")
                    old["reason"] = old.get("reason") or r.get("reason")
        except (ValueError, KeyError):
            pass
    for r in rows:
        old = prev.get(r["path"])
        if old:
            if old.get("reviewed") and not r["gates"]:
                r["gates"] = old.get("gates") or []
            if old.get("reviewed"):
                r["reviewed"] = True
            if old.get("reason"):
                r["reason"] = old["reason"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"schemaVersion": "route-gates/v1", "routes": rows},
        ensure_ascii=False, indent=1,
    ) + "\n")
    n_ig = sum(1 for r in rows if r["note"] == "identity-gate-only")
    n_g = sum(1 for r in rows if r["gates"])
    n_e = sum(1 for r in rows if r["note"].startswith("exempt"))
    print(f"[gen] routes={len(rows)} 启发式闸命中={n_g} 豁免={n_e} identity-gate-only={n_ig}（须人工审）")
    print(f"[gen] 写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
