"""三层 RBAC 认证内核（路线 B1）：登录/密码/JWT/请求身份门禁。

三条认证通道严格分离（thin-node spec §7，2026-09-14 三层修订）：
- **用户 JWT**（人：root/admin/user，本模块）；
- `BOK_CP_TOKEN`（脚本/工具机器通道，沿用 optional_bearer_auth 语义）；
- `node_token`（节点心跳/指令，NodeStore sha256，与用户体系不同源）。

门禁开关：`BOK_AUTH_REQUIRED` 未设（默认）→ `identity_gate` 直通，单机/开发形态
零变化；置 1 后除豁免路径外全部要求有效用户 JWT 或机器 token。开认证时必须配置
`BOK_JWT_SECRET`（或回落 `BOK_CP_TOKEN`）——CP startup fail-closed，拒绝用可伪造
的 dev 密钥开认证。

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

# 豁免路径：健康检查 / 登录本身 / 节点心跳自鉴权 / API 文档。
_EXEMPT_PATHS = ("/health", "/api/auth/login", "/api/nodes/heartbeat", "/docs", "/openapi.json", "/redoc")


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
    return (os.environ.get("BOK_JWT_SECRET") or os.environ.get("BOK_CP_TOKEN") or "").strip()


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


def _unauthorized() -> Response:
    return Response(status_code=401, content=b'{"detail":"unauthorized"}', media_type="application/json")


async def identity_gate(request: Request, call_next):
    """全局身份门禁：auth-off 直通；auth-on 要求用户 JWT 或机器 token。"""
    request.state.identity = None
    path = request.url.path
    if path in _EXEMPT_PATHS or not auth_required():
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # 机器通道：BOK_CP_TOKEN 同值直通（脚本/工具/CI）。
    cp_token = os.environ.get("BOK_CP_TOKEN", "").strip()
    if cp_token and token == cp_token:
        return await call_next(request)
    if not token:
        return _unauthorized()
    try:
        identity = decode_token(token)
    except HTTPException:
        return _unauthorized()
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
