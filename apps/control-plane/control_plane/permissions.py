"""页面/管理权限目录（单一事实源）：话务员页面 8 键 + admin 管理面 6 键。

口径（B4 契约 §1/§2 + 2026-09-20 下发制修订）：
- 页面目录 8 键=话务员可有可无的页面面，主管按人配置；''/缺失=默认集。
- 管理面目录 6 键（settings/knowledge/personas/audit/users/supervisor）由 root
  逐键下发给 admin（「root 没下发就用不了」）；nodes/licenses 恒 root 专属，
  永不进任何目录。admin 存量行 permissions_json=''=全量（升级零变化），显式
  JSON=root 裁定集；新建 admin 由建号路径盖缺省章（运营默认集+管理键全关）。
- 存储 `users.permissions_json`（两角色共用一列）：逐请求查库（JWT 只装身份），
  改权限对已签发 token 即时生效。
- 执掌：root/admin 管理键 / 机器通道（BOK_CP_TOKEN）/无身份（auth-off）恒直通；
  user 缺键 403——admin 管理面直通改为下发制后由 _gate_management 逐键判定。

web 侧 PAGE_KEYS/PAGE_LABELS（apps/web/components/session-context.tsx）与两表
逐字对齐；顺序即契约表顺序（导航渲染顺序），勿重排。
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

# 管理面键目录（2026-09-20 下发制）：root 逐键下发给 admin。原则「root 没下发就
# 用不了」——被盗/越权的 admin 凭据只享受被授予的子集。存储复用
# users.permissions_json：''/缺失/非法 JSON = 存量全量（页键全集+管理键全集，
# 升级对存量 admin 行为零变化）；显式 JSON 数组 = root 裁定集。新建 admin 由
# create_user 盖默认章（运营默认集+管理键全关）。机器通道/无身份（auth-off）
# 不进本目录语义（各自直通，与 require_role 同判）。
MANAGEMENT_PERMISSIONS: dict[str, dict] = {
    "settings": {"label": "设置", "default": False},
    "knowledge": {"label": "知识库", "default": False},
    "personas": {"label": "人设", "default": False},
    "audit": {"label": "审计", "default": False},
    "users": {"label": "员工管理", "default": False},
    "supervisor": {"label": "主管台", "default": False},
}
MANAGEMENT_GRANTABLE: set[str] = set(MANAGEMENT_PERMISSIONS)
# 新建 admin 的缺省下发集：运营默认集（报表关，与话务员同基线）+ 管理键全关。
DEFAULT_ADMIN_PERMISSIONS: list[str] = list(DEFAULT_USER_PERMISSIONS)

_FULL_ADMIN_PERMISSIONS: list[str] = list(PAGE_PERMISSIONS) + list(MANAGEMENT_PERMISSIONS)


def effective_admin_permissions(permissions_json: str) -> list[str]:
    """admin 有效集（两目录序拼接）：''/缺失/非法 JSON = 存量全量；显式数组 =
    root 裁定集（未知键静默丢弃，与 user 读侧兜底同纪律）。"""
    raw = (permissions_json or "").strip()
    keys: list[str] | None = None
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            keys = [str(k) for k in parsed]
    if keys is None:
        return list(_FULL_ADMIN_PERMISSIONS)
    wanted = set(keys)
    return [
        k for k in _FULL_ADMIN_PERMISSIONS if k in wanted
    ]


def effective_permissions(role: str, permissions_json: str) -> list[str]:
    """有效权限集（目录序，供 web 导航与 _gate_page 判定共用）。

    root → 全部 grantable 键；admin → 下发制有效集（''=存量全量，显式 JSON=root
    裁定集，见 effective_admin_permissions）；user → ''/缺失/非法 JSON → 默认集，
    否则精确集合。非 grantable 键静默丢弃（写入路径已 400 拦截，这里是存量脏
    数据的读侧兜底，不是第二道校验）。
    """
    role = (role or "").strip()
    if role == "root":
        return sorted(GRANTABLE_PERMISSIONS)
    if role == "admin":
        return effective_admin_permissions(permissions_json)
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
