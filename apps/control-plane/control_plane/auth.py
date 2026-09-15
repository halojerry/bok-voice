"""三层 RBAC 认证内核（路线 B1）：登录/密码/JWT/请求身份门禁。

三条认证通道严格分离（thin-node spec §7，2026-09-14 三层修订）：
- **用户 JWT**（人：root/admin/user，本模块）；
- `BOK_CP_TOKEN`（脚本/工具机器通道，沿用 optional_bearer_auth 语义）；
- `node_token`（节点心跳/指令，NodeStore sha256，与用户体系不同源）。

门禁开关：`BOK_AUTH_REQUIRED` 未设（默认）→ `identity_gate` 直通，单机/开发形态
零变化；置 1 后除豁免路径外全部要求有效用户 JWT 或机器 token。开认证时必须配置
`BOK_JWT_SECRET`（≥32 字节随机、与 `BOK_CP_TOKEN` 异值）——CP startup fail-closed，
机器通道凭据不再回落作签名密钥（2026-09-16 深测 P1：回落令 CP token 泄露=离线
伪造 8h root JWT）。

注册顺序约定：identity_gate 必须**第一个**注册（Starlette 后注册者在外层），
保证它在 CorrelationMiddleware 内层运行——读取其 correlation 并覆写
`user_id=已验证身份`，审计 actor 由此自动落账。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import datetime
from dataclasses import dataclass

import jwt as pyjwt
from fastapi import HTTPException, Request, Response

from bok_voice_obs.context import Correlation, get_correlation, set_correlation

_JWT_ALGO = "HS256"
JWT_TTL_S = 8 * 3600
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

# 豁免路径：健康检查 / 登录本身 / 节点心跳与注册自鉴权 / LiveKit 服务端 webhook
#（webhook 由 LiveKit server 直调 CP，无用户也无机器 env，属基础设施通道；
# API 文档不再豁免——auth-on 生产由 FastAPI 条件参数直接关闭）。
_EXEMPT_PATHS = (
    "/health",
    "/api/auth/login",
    "/api/nodes/heartbeat",
    # register 的鉴权因子=license key（体内自证，节点没有 JWT/CP token）：
    # 加固模式下由端点内的 license 闸把关（无 key 即 401），与 heartbeat 用
    # node_token 自鉴权同构——中间件不重复预拦。
    "/api/nodes/register",
    "/api/webhook/livekit",
)


@dataclass
class Identity:
    """已验证用户身份（JWT claims 的类型化视图）。"""

    user_id: str
    username: str
    role: str  # root / admin / user
    org_id: str = ""
    account_id: str = ""


def auth_required() -> bool:
    return os.environ.get("BOK_AUTH_REQUIRED", "").strip() == "1"


def jwt_secret() -> str:
    """JWT 签名密钥：只认 BOK_JWT_SECRET（2026-09-16 深测 P1 删除 CP token 回落）。

    BOK_CP_TOKEN 需拷进每台 agent worker，任何一份泄露即离线伪造 8h root JWT；
    两值必须独立配置（startup 校验同值/缺失/弱密钥拒绝开认证）。
    """
    return (os.environ.get("BOK_JWT_SECRET") or "").strip()


def hash_password(password: str) -> str:
    """scrypt（stdlib，免 argon2 依赖）：`scrypt$<salt b64>$<dk b64>`。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = str(stored or "").split("$")
        if scheme != "scrypt":
            return False
        salt, want = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=len(want))
        return hmac.compare_digest(dk, want)
    except Exception:
        return False


def create_token(identity: Identity) -> str:
    secret = jwt_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="BOK_JWT_SECRET not configured — cannot issue tokens")
    now = datetime.datetime.now(datetime.timezone.utc)
    return pyjwt.encode(
        {
            "sub": identity.user_id,
            "name": identity.username,
            "role": identity.role,
            "org": identity.org_id,
            "account": identity.account_id,
            "iat": now,
            "exp": now + datetime.timedelta(seconds=JWT_TTL_S),
        },
        secret,
        algorithm=_JWT_ALGO,
    )


def decode_token(token: str) -> Identity:
    """校验并解出身份；任何失败一律 401（不泄露原因细节）。"""
    secret = jwt_secret()
    if not secret:
        raise HTTPException(status_code=401, detail="auth not configured")
    try:
        claims = pyjwt.decode(token, secret, algorithms=[_JWT_ALGO])
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    sub = str(claims.get("sub") or "")
    if not sub:
        raise HTTPException(status_code=401, detail="invalid token")
    return Identity(
        user_id=sub,
        username=str(claims.get("name") or sub),
        role=str(claims.get("role") or "user"),
        org_id=str(claims.get("org") or ""),
        account_id=str(claims.get("account") or ""),
    )


def identity_from_request(request: Request) -> Identity | None:
    """从 Authorization: Bearer 显式解身份（门禁之外的端点自取，如 /api/auth/me）。"""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    try:
        return decode_token(token)
    except HTTPException:
        return None


def current_identity(request: Request) -> Identity | None:
    """优先取门禁已解析的身份，否则显式解头部（便于无门禁环境下的测试/内部调用）。"""
    ident = getattr(request.state, "identity", None)
    return ident or identity_from_request(request)


def scoped_account(request: Request, requested: str = "") -> str:
    """列表端点的账号过滤值。

    语义：**有身份就按身份收紧**——user/admin 强制本账号（显式传别账号也被压回），
    root 可显式指定任意账号（空=跨账号全部）；无身份（auth-off 开发形态或机器通道）
    原样放行，与现状同信任级。
    """
    ident = current_identity(request)
    if ident is None or ident.role == "root":
        return requested
    return ident.account_id


def same_account(request: Request, row: dict | None) -> bool:
    """by-ID 资源归属判定：root/无身份（auth-off 或机器通道）恒真；user/admin 仅本账号。"""
    if row is None:
        return True
    ident = current_identity(request)
    if ident is None or ident.role == "root":
        return True
    return str(row.get("account_id") or "") == ident.account_id


def deny_cross_account(request: Request, row: dict | None) -> dict | None:
    """越权一律 404（不泄露资源存在性）；row=None 原样返回（由调用方 404）。"""
    if row is not None and not same_account(request, row):
        raise HTTPException(status_code=404, detail="not found")
    return row


def require_role(request: Request, *roles: str) -> Identity | None:
    """管理面角色闸（与页面矩阵对齐：报表/审计/知识库/人设/设置/主管操作等）。

    auth-on：无身份 401、身份不在允许角色 403；auth-off 无身份直通（开发形态）；
    机器通道（BOK_CP_TOKEN，state.machine）恒直通——agent 等内部服务不受角色闸误杀。
    """
    ident = current_identity(request)
    if ident is None:
        if getattr(request.state, "machine", False):
            return None
        if auth_required():
            raise HTTPException(status_code=401, detail="unauthorized")
        return None
    if roles and ident.role not in roles:
        raise HTTPException(status_code=403, detail="forbidden")
    return ident


def owner_scope_filter(request: Request, requested: str | None) -> str | None:
    """列表端点的 owner 过滤值（B3 话务员级资源）。

    语义与 scoped_account 同族：user 强制本人（仓库层展开为「共享+本人」）；
    admin/root 原样（默认 None=本账号全部）；无身份（auth-off/机器通道）原样——
    agent 机器通道显式传 owner_scope=建单人，CP 不做二次推断。
    """
    ident = current_identity(request)
    if ident is None or ident.role in ("admin", "root"):
        return requested
    return ident.user_id


def deny_foreign_owner(request: Request, row: dict | None, *, edit: bool = False) -> dict | None:
    """by-ID 资源的话务员级归属闸（B3）。

    user：别人的条目 404（不泄露存在性，与跨账号口径一致）；共享条目（owner=''）
    可读可引用，但**改动（edit=True）403**——共享基线只归 admin/root，防一个话务员
    改掉全组的话术/QA。admin/root/无身份放行。
    """
    if row is None:
        return row
    ident = current_identity(request)
    if ident is None or ident.role in ("admin", "root"):
        return row
    owner = str(row.get("owner_user_id") or "")
    if owner == "":
        if edit:
            raise HTTPException(status_code=403, detail="shared resource requires admin")
        return row
    if owner != ident.user_id:
        raise HTTPException(status_code=404, detail="not found")
    return row


def _unauthorized() -> Response:
    return Response(status_code=401, content=b'{"detail":"unauthorized"}', media_type="application/json")


async def identity_gate(request: Request, call_next):
    """全局身份门禁：auth-off 直通；auth-on 要求用户 JWT 或机器 token。"""
    request.state.identity = None
    path = request.url.path
    # 静态站（管理台 SPA）GET/HEAD 豁免：全部特权数据都在 /api/* 端点后面，
    # auth-on 不得把登录页自己也 401——云端 BOK_AUTH_REQUIRED=1 下裸 GET /
    # 曾被整站拦死（2026-09-15 compose 排练实测，B4 登录流程不可达）。
    static_get = request.method in ("GET", "HEAD") and not path.startswith("/api/")
    if path in _EXEMPT_PATHS or static_get or not auth_required():
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # 机器通道：BOK_CP_TOKEN 同值直通（脚本/工具/CI），并打 machine 标记——
    # require_role 等助手对机器请求放行（agent 等内部服务不受角色闸误杀）。
    cp_token = os.environ.get("BOK_CP_TOKEN", "").strip()
    if cp_token and token == cp_token:
        request.state.machine = True
        return await call_next(request)
    if not token:
        return _unauthorized()
    try:
        identity = decode_token(token)
    except HTTPException:
        return _unauthorized()
    lookup = getattr(getattr(request, "app", None), "state", None)
    lookup = getattr(lookup, "user_lookup", None) if lookup is not None else None
    if lookup is not None:
        row = lookup(identity.user_id) or {}
        if not row or str(row.get("status") or "") != "active":
            # 禁用/删号的旧 token 立即失效（此前最长 8h TTL 内照常全权调用）。
            return _unauthorized()
        # 角色以库为准：降权（admin→user）即时生效，require_role 不再信过期 claim。
        identity.role = str(row.get("role") or identity.role)
    request.state.identity = identity
    # 覆写 correlation.user_id（保留 CorrelationMiddleware 已设的其余字段）——
    # 审计 to_dict 的 actor 由此自动落成已验证身份。
    corr = get_correlation()
    set_correlation(
        Correlation(
            request_id=corr.request_id,
            call_id=corr.call_id,
            account_id=corr.account_id or identity.account_id,
            object_id=corr.object_id,
            persona_id=corr.persona_id,
            user_id=identity.username,
            span_id=corr.span_id,
        )
    )
    return await call_next(request)
