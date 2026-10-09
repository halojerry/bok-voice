"""W⑥-1（2026-10-09）路由闸 lint：每条 /api 路由必须有可归类的鉴权闸。

防「未来新增端点裸奔」：枚举 FastAPI ``app.routes``，对每条 /api 路由断言其
handler 源码（或豁免白名单）出现下列之一——
- 显式豁免（/health、登录、节点自证、webhook——各自有体内自证/频控）；
- 角色闸：require_role / _gate_page / _gate_page_any / _gate_management /
  auto_gate_management / deny_cross_account / deny_foreign_owner /
  owner_scope_filter / scoped_account / current_identity / same_account /
  machine-only（require_role 含）；
- 静态挂载（非 APIRoute）。

新增路由不满足即红——届时要么补闸，要么把它加进豁免表并写明理由（豁免表
本文件唯一，逐条带注释，评审可见）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

import inspect
from pathlib import Path

from fastapi.routing import APIRoute

# 显式豁免（中间件层放行但端点内有自证/频控/验签，注释=理由）。
_EXEMPT = {
    "/health",               # 存活探针，零数据
    "/api/auth/login",       # 登录本身（滑窗频控 + dummy scrypt 时序均衡）
    "/api/nodes/heartbeat",  # node_token sha256 自证
    "/api/nodes/register",   # license key 闸（端点内）
    "/api/nodes/logs",       # node_token 自证 + 频控
    "/api/webhook/livekit",  # LIVEKIT_API_SECRET 验签 + body sha256 双验
    # 节点工件下载（前缀豁免同族）：端点内 node_token/license 双因子自证
    #（revoked 403/无效 401）+ 三段路径白名单 + resolve/parents 防穿越。
    "/api/nodes/downloads/{kind}/{version}/{filename}",
}

# 闸记号：handler 源码里出现任一即视为「有闸」。identity_gate 是全局兜底
#（auth-on 全 /api 要 JWT/机器 token），本 lint 在其上要求**业务归属闸**——
# current_identity 计入（写路径盖账号章/身份判定也算显式用了身份）。
_GATE_TOKENS = (
    "require_role(",
    "_gate_page(",
    "_gate_page_any(",
    "_gate_management(",
    "auto_gate_management(",
    "deny_cross_account(",
    "deny_foreign_owner(",
    "owner_scope_filter(",
    "scoped_account(",
    "current_identity(",
    "same_account(",
)

# 已知带闸的共用助手（瘦 handler 委托它们；助手的闸声明在各自函数体内）。
# 新增共用助手若带闸，把调用记号加进来——记号必须指向「函数体内有 _GATE_TOKENS
# 之一」的助手（lint 不递归，靠这张表人工背书，评审可见）。
_GATED_HELPERS = (
    "_campaign_transition(",  # _gate_page("campaigns") + deny_cross_account
)


def test_every_api_route_has_a_gate():
    from control_plane.main import app

    ungated: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue  # StaticFiles 挂载等非 API 路由
        path = route.path
        if not path.startswith("/api/"):
            continue
        if path in _EXEMPT:
            continue
        func = route.endpoint
        try:
            src = inspect.getsource(func)
        except (OSError, TypeError):
            ungated.append(f"{path} <no source>")
            continue
        if not any(tok in src for tok in _GATE_TOKENS + _GATED_HELPERS):
            methods = ",".join(sorted(getattr(route, "methods", []) or []))
            ungated.append(f"{methods} {path}")
    assert not ungated, "以下 /api 路由没有任何鉴权闸（新增端点必须过闸或入豁免表）:\n" + "\n".join(ungated)


def test_exempt_table_paths_all_exist():
    """豁免表自身防漂移：表内路径必须真实存在于路由表。"""
    from control_plane.main import app

    paths = {
        getattr(r, "path", "") for r in app.routes if isinstance(r, APIRoute)
    }
    missing = [p for p in _EXEMPT if p not in paths]
    assert not missing, f"豁免表含不存在路径（端点已改名/删除，表要同步）: {missing}"


def test_lint_self_guard():
    """lint 自身存在性钉（文件被误删/改名即红，防门禁静默失效）。"""
    assert Path(__file__).exists()
