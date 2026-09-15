"""B4 页面权限目录（单一事实源）：主管按人配置话务员可见的页面/接口面。

口径（见 .superpowers/sdd/2026-09-14-b4-permissions/CONTRACT.md §1/§2）：
- 目录 8 键=话务员可有可无的页面面；主管专属面（settings/knowledge/personas/
  audit/supervisor/users/nodes）永不进目录，继续走 require_role(admin,root)。
- 存储 `users.permissions_json`：''/缺失=默认集（default=True 的 7 键）；否则=精确
  集合（'[]'=全关）。权限逐请求查库（JWT 只装身份），主管改权限对旧 token 即时生效。
- 执掌：root/admin/机器通道（BOK_CP_TOKEN）/无身份（auth-off）恒直通；user 缺键 403。

web 侧 PAGE_KEYS/PAGE_LABELS（apps/web/components/session-context.tsx）与本表逐字对齐；
顺序即契约表顺序（导航渲染顺序），勿重排。
"""
from __future__ import annotations

import json

PAGE_PERMISSIONS: dict[str, dict] = {
    "calls": {"label": "工作台", "grantable": True, "default": True},
    "roster": {"label": "名册", "grantable": True, "default": True},
    "campaigns": {"label": "外呼", "grantable": True, "default": True},
    "objects": {"label": "对象", "grantable": True, "default": True},
    "interpret": {"label": "同传", "grantable": True, "default": True},
    "templates": {"label": "话术", "grantable": True, "default": True},
    "qa": {"label": "快答库", "grantable": True, "default": True},
    # 报表默认关（主管按人开）——dataviz 口径含全账号聚合，不是话务员基线面。
    "reports": {"label": "报表", "grantable": True, "default": False},
}

# 默认集 = 目录里 default=True 的键（顺序即目录序）。
DEFAULT_USER_PERMISSIONS: list[str] = [
    key for key, meta in PAGE_PERMISSIONS.items() if meta["default"]
]
# 主管可授予 user 的键全集（写入校验白名单）。
GRANTABLE_PERMISSIONS: set[str] = {
    key for key, meta in PAGE_PERMISSIONS.items() if meta["grantable"]
}


def effective_permissions(role: str, permissions_json: str) -> list[str]:
    """有效权限集（目录序，供 web 导航与 _gate_page 判定共用）。

    role∈{admin,root} → 全部 grantable 键（排序输出，web 只查成员）；user →
    ''/缺失/非法 JSON → 默认集，否则精确集合。非 grantable 键静默丢弃（写入
    路径已 400 拦截，这里是存量脏数据的读侧兜底，不是第二道校验）。
    """
    if (role or "").strip() in ("admin", "root"):
        return sorted(GRANTABLE_PERMISSIONS)
    raw = (permissions_json or "").strip()
    keys: list[str] = list(DEFAULT_USER_PERMISSIONS)
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            keys = [str(k) for k in parsed]
    wanted = set(keys)
    return [
        key for key in PAGE_PERMISSIONS
        if key in wanted and key in GRANTABLE_PERMISSIONS
    ]
