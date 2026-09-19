from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import re
import sys
import uuid
import wave
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from bok_voice_core.flow_graph import validate_flow_graph
from bok_voice_core.intent_rules import validate_conditions
from bok_voice_core.providers import BusinessRepository
from bok_voice_core.policies import select_session_manifest
from bok_voice_core.qa_text import mine_qa_pairs
from bok_voice_core.types import CallMode, CallStatus, Role, SessionManifest, TurnEvent

from bok_voice_core.settlement import SettlementTrigger
from bok_voice_core.embeddings import CharHashEmbedding, HybridLexicalEmbedding
from bok_voice_knowledge.knowledge import DefaultKnowledgeService
from bok_voice_knowledge.markdown_source import LocalMarkdownSource
from bok_voice_knowledge.vector_store import InMemoryVectorStore
from bok_voice_business_db.vector_store import SqlVectorStore
from bok_voice_obs.audit import AuditEvent, AuditStore, audit_store
from bok_voice_obs.context import get_correlation
from bok_voice_obs.logging import configure_logging, get_logger
from bok_voice_obs.middleware import CorrelationMiddleware

from .campaign import (
    parse_call_windows,
    redispatch_due,
    redispatch_harvest_updates,
    redispatch_policy,
    _utcnow_naive,
)
from .deps import build_engine, build_repository, build_session_factory
from .dispatch_utils import cleanup_dispatch, has_active_dispatch
from .nodes_store import HEARTBEAT_INTERVAL_S, LicenseError, NodeStore
from .permissions import GRANTABLE_PERMISSIONS, PAGE_PERMISSIONS, effective_permissions
from .pregen import persona_pregen_status
from . import pregen as pregen_mod
from . import qa_cluster as qa_cluster_mod
from .auth import (
    Identity,
    JWT_TTL_S,
    auth_required,
    create_token,
    current_identity,
    deny_cross_account,
    deny_foreign_owner,
    hash_password,
    identity_gate,
    owner_scope_filter,
    require_role,
    scoped_account,
    verify_password,
)
from .schemas import (
    AssistRequest,
    CreateCallRequest,
    CreateObjectRequest,
    DialNowRequest,
    ImportRequest,
    DialResultRequest,
    IntentRuleCreate,
    IntentRulePatch,
    ListenStopRequest,
    LoginRequest,
    ChangePasswordRequest,
    CreateUserRequest,
    UpdateUserRequest,
    PersonaRequest,
    QaClusterRequest,
    QaEntryCreate,
    QaEntryPatch,
    RosterClaimRequest,
    RosterHandledRequest,
    TemplateRequest,
    UpdateTemplateRequest,
    UpdateObjectRequest,
    UpdatePersonaRequest,
    SettingsRequest,
    SiteCreateRequest,
    TokenRequest,
    TokenResponse,
    TransferSipRequest,
    TrunkRegisterRequest,
    WhatsAppCaptureRequest,
    WhatsAppHandledRequest,
)

# 通话终态集合：模块级常量（dial-result 端点与下方重派段共用）。
# 只含真终态——CallStatus.FAILED 是通话级失败终态(与拨号失败 disposition="failed" 同名不同义)。
_TERMINAL_CALL_STATUSES = (CallStatus.ENDED.value, CallStatus.FAILED.value)

# 登录时序均衡（2026-09-16 深测 P3）：用户不存在时也跑一次同价位 scrypt 校验。
# 旧版短路令「存在且 active」可被 ~20× 响应差探测（1.1ms vs 25.5ms 实测）。
_DUMMY_PASSWORD_HASH = hash_password("bok-dummy-login-timing-equalizer")


app = FastAPI(
    title="Bok Voice Control Plane",
    version="0.1.0",
    # auth-on 生产关 API 文档（深测 P3：/openapi.json /docs /redoc 匿名可读=全
    # API 面暴露）。auth-on 必须在 CP 进程 env 先行设置（与下方 startup 校验同一
    # 要求）；auth-off 开发形态文档照常。
    docs_url=None if auth_required() else "/docs",
    redoc_url=None if auth_required() else "/redoc",
    openapi_url=None if auth_required() else "/openapi.json",
)
# 注册顺序=洋葱层次（后注册者在最外层）。identity_gate 必须**第一个**注册（最内层）：
# 它要在 CorrelationMiddleware 内层运行——读取其 correlation 并覆写 user_id=已验证
# 身份，审计 actor 由此自动落账（见 auth.py 模块注释）。
app.middleware("http")(identity_gate)
_cors_origins = [o.strip() for o in os.environ.get("BOK_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],  # 云端部署设 BOK_CORS_ORIGINS 收敛到管理台 origin
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CorrelationMiddleware)


@app.middleware("http")
async def optional_bearer_auth(request: Request, call_next):
    """可选 Bearer 鉴权（R2）：BOK_CP_TOKEN 未设=全放行（本机单用户形态零变化）。

    设置后除 /health、/api/nodes/heartbeat 与 /api/nodes/register（license 自证，
    端点内 license 闸把关）外全部端点要求
    `Authorization: Bearer <BOK_CP_TOKEN>`——暴露到局域网/云之前必须设置；
    agent(worker env)与 web 需同步带同值。心跳豁免：该端点用注册时签发的
    node_token 自鉴权（sha256 比对，与 CP token 不同源），CP 门禁会把它拦死
    令节点注册表失联；register/list 属管理操作，仍在门禁内。
    """
    if auth_required():
        # BOK_AUTH_REQUIRED=1 时门禁统一由 identity_gate 承担（用户 JWT 或机器
        # token 二选一）；本中间件若照旧比对 CP token 会把用户 JWT 误杀在门外。
        return await call_next(request)
    expected = os.environ.get("BOK_CP_TOKEN", "").strip()
    _prefix_exempt = any(request.url.path.startswith(p)
                         for p in ("/api/nodes/downloads/",))
    if expected and request.url.path not in (
            "/health", "/api/nodes/heartbeat", "/api/nodes/register",
            "/api/nodes/logs", "/api/webhook/livekit") and not _prefix_exempt:
        # webhook 豁免与 identity_gate _EXEMPT_PATHS 对齐:LiveKit 签名 webhook
        # 不带 CP token,CP-token-only 模式曾在此 401——agent 崩溃重派队静默死
        # (端点内另有签名校验,豁免的只是机器 token 门,2026-09-17 全量 debug F2 修)。
        # 静态站 GET/HEAD 同豁免（同 identity_gate：登录页不得被机器 token 门拦死，
        # 特权数据全在 /api/* 后面）。
        static_get = (request.method in ("GET", "HEAD")
                      and not request.url.path.startswith("/api/"))
        provided = request.headers.get("authorization", "")
        if not static_get and not (
            provided.startswith("Bearer ")
            and hmac.compare_digest(provided[7:].strip(), expected)
        ):
            return Response(status_code=401, content=b'{"detail":"unauthorized"}',
                             media_type="application/json")
    return await call_next(request)


# 云端窒息点路径（site-delivery M1）：revoked node_token 打这两个通话面端点即 403。
_KILLSWITCH_PATHS = frozenset({"/api/calls", "/api/token"})


@app.middleware("http")
async def node_revoked_gate(request: Request, call_next):
    """熔断窒息点（site-delivery M1，2026-09-16）：root 吊销的 node_token 打云端
    通话面（建单 /api/calls、发房 token /api/token）即刻 403 "node revoked"。

    注册位置铁律=**两个门禁之外**（后注册者在外层，这里最后注册=最外层）：
    auth-on 下 node_token 不是用户 JWT，内层 identity_gate 只会给 401——窒息
    语义要求 403 且与认证模式无关（auth-off 开放流同样要拦）。只对 POST 且路径
    命中时解 Bearer 为 node_token（sha256 查注册表；空 Bearer/用户 JWT/CP token
    解不出节点=零命中直通，行为零变化）。revoked 节点打其余端点仍走各自门禁
    （心跳 401 附 shutdown 指令由端点自己塑形）。"""
    if request.method == "POST" and request.url.path in _KILLSWITCH_PATHS:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if token:
            node = _node_store().resolve_node_token(token)
            if node is not None and node.get("revoked"):
                # 取证审计（site-delivery fixwave）：窒息点每次命中落一条（按尝试计，
                # revoked 节点已在停栈流程中，量级天然有界）。_audit 为运行时名字
                # 解析，中间件注册先于其定义无碍。
                _audit("node.chokepoint_denied", subject_type="node",
                       subject_id=node.get("node_id", ""), outcome="denied",
                       account_id="", detail={"path": request.url.path})
                return JSONResponse(status_code=403, content={"detail": "node revoked"})
    return await call_next(request)

control_log = get_logger("control-plane", component="control-plane", service="control-plane")


def _repo() -> BusinessRepository:
    # SQL 路径：每个 _repo() 调用都拿独立 Session（已提交数据对所有新 Session 可见），
    # 根除“共享 Session 并发操作 InvalidRequestError / PendingRollbackError”。
    factory = getattr(app.state, "session_factory", None)
    if factory is not None:
        from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

        return SqlAlchemyBusinessRepository(factory())
    return app.state.repo


def _node_store() -> NodeStore:
    """节点注册表（_repo() 同款访问姿势：startup 与 repo 同一 engine 装配）。

    engine=None（dev/tests 无 DATABASE_URL）→ NodeStore 内存双模，见 nodes_store。
    """
    return app.state.node_store


def _sidecar_url(env_name: str, default: str) -> str:
    return (os.environ.get(env_name) or default).rstrip("/")


def _qwen3_tts_url() -> str:
    return _sidecar_url("QWEN3_TTS_BASE_URL", "http://127.0.0.1:8788")


def _qwen3_asr_url() -> str:
    return _sidecar_url("QWEN3_ASR_BASE_URL", "http://127.0.0.1:8787")


def _seed_root_user() -> None:
    """BOK_ROOT_USERNAME/BOK_ROOT_PASSWORD 置定时幂等种 root 账号（云端部署入口）。

    环境变量未置=不种子（单机/开发形态无用户体系，行为零变化）；同名用户已存在
    =跳过（幂等，重启不重置密码）。
    """
    username = os.environ.get("BOK_ROOT_USERNAME", "").strip()
    password = os.environ.get("BOK_ROOT_PASSWORD", "")
    if not username or not password:
        return
    if _repo().get_user_by_username(username):
        return
    _repo().create_user(
        username=username, password_hash=hash_password(password), role="root",
        display_name="Platform Root",
    )
    control_log.warning(
        "root_user_seeded",
        extra={"event": "auth.root.seeded", "data": {"username": username}},
    )


@app.on_event("startup")
def _startup() -> None:
    configure_logging(level=os.environ.get("BOK_LOG_LEVEL", "INFO"))
    # 云托管管理台运行时 CP 地址注入（见 _write_web_runtime_config docstring）：
    # 在首个请求前落盘，缺省/本地形态零动作。
    _public_cp_url = (os.environ.get("BOK_CP_PUBLIC_URL") or "").strip()
    if _public_cp_url:
        _write_web_runtime_config(_public_cp_url, _web_static_dir)
    engine = build_engine()
    app.state.repo = build_repository(engine)
    app.state.node_store = NodeStore(engine)  # 与 repo 同一 engine；None → 内存双模
    # token 生命周期（2026-09-16 深测 P2）：identity_gate 解码后按 sub 查库——
    # 禁用/删号立即 401、role 以库为准（降权即时生效），不再吃满 8h TTL。
    app.state.user_lookup = lambda user_id: _repo().get_user(user_id)
    app.state.session_factory = build_session_factory(engine)
    app.state.lk_key = os.environ.get("LIVEKIT_API_KEY", "")
    app.state.lk_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    app.state.lk_url = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if auth_required():
        secret = (os.environ.get("BOK_JWT_SECRET") or "").strip()
        cp_token = (os.environ.get("BOK_CP_TOKEN") or "").strip()
        if not secret:
            # fail-closed：拒绝无签名密钥开认证（机器通道 CP token 不再回落）。
            raise RuntimeError(
                "BOK_AUTH_REQUIRED=1 但未配置 BOK_JWT_SECRET —— 拒绝开启认证"
                "（机器通道 CP token 不得兼作签名密钥）")
        if cp_token and cp_token == secret:
            # 2026-09-16 深测 P1：CP token 会拷进每台 agent worker，同值=泄露即
            # 离线伪造 root JWT。
            raise RuntimeError(
                "BOK_CP_TOKEN 与 BOK_JWT_SECRET 同值 —— 机器通道凭据不得兼作"
                " JWT 签名密钥，拒绝开启认证")
        if len(secret.encode("utf-8")) < 32:
            raise RuntimeError("BOK_JWT_SECRET 强度不足（<32 字节）—— 拒绝开启认证")
    _seed_root_user()
    vault = os.environ.get("VAULT_ROOT", "./data/vault")
    embedder = CharHashEmbedding(384)
    if engine is not None and getattr(engine.dialect, "name", "") != "sqlite":
        from sqlalchemy.orm import sessionmaker

        session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        vector = SqlVectorStore(session_factory(), embedder)
    else:
        vector = InMemoryVectorStore(HybridLexicalEmbedding(512))
    app.state.knowledge = DefaultKnowledgeService(
        markdown=LocalMarkdownSource(vault),
        vector=vector,
    )
    if isinstance(vector, InMemoryVectorStore):
        # SQLite 路径：业务数据落盘，知识向量启动时从 vault 重建（幂等）。
        try:
            asyncio.get_event_loop().create_task(_rebuild_in_memory_knowledge(vector, vault))
        except Exception as exc:  # pragma: no cover
            control_log.warning("knowledge_rebuild_failed", extra={"data": {"error": str(exc)}})
    # 僵尸通话回收(QA B4):启动先扫一遍,再 60s 周期。
    try:
        asyncio.get_event_loop().create_task(_reaper_loop())
    except Exception as exc:  # pragma: no cover
        control_log.warning("reaper_start_failed", extra={"data": {"error": str(exc)}})
    # 外呼战役串行循环(spec 2026-09-12 Wave3):5s 巡检收割终态→起下一通→判 done。
    # 函数体内延迟 import:campaign 模块反查 main(_repo/_lkapi_client/_create_call_in),
    # 模块级 import 会成环。
    try:
        from .campaign import _campaign_loop

        asyncio.get_event_loop().create_task(_campaign_loop())
    except Exception as exc:  # pragma: no cover
        control_log.warning("campaign_start_failed", extra={"data": {"error": str(exc)}})
    app.state.settlement = SettlementTrigger()
    # Mirror every JSONL audit event into the repository (SQL or in-memory) so
    # /api/audit is queryable without scraping the file sink.

    def _audit_tap(event):
        try:
            repo = _repo()
            if hasattr(repo, "append_audit"):
                repo.append_audit(event.to_dict())
        except Exception as exc:  # pragma: no cover - audit tap must not break the hot path
            control_log.warning("audit_db_tap_failed", extra={"event": "audit.tap.error", "data": {"error": str(exc)}})

    audit_store(AuditStore(directory=_audit_dir(), tap=_audit_tap))


async def _rebuild_in_memory_knowledge(vector: InMemoryVectorStore, vault_root: str) -> None:
    """Rebuild the in-memory knowledge index from vault markdown files.

    Deterministic path-based ids keep the index idempotent across restarts.
    """
    root = Path(vault_root)
    if not root.exists():
        return
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root).as_posix()
        parts = rel.split("/")
        if len(parts) < 4 or parts[0] != "accounts" or parts[2] != "knowledge":
            continue
        account_id = parts[1]
        try:
            content = md.read_text(encoding="utf-8")
        except Exception:
            continue
        # 与 import_document 共用同一分块函数（2026-09-07 对齐:治「重启后 chunk
        # 粒度回退整文件」的双路径漂移）;id 加序号保确定性,账户隔离不变。
        from bok_voice_knowledge.knowledge import _chunk_content

        chunks = _chunk_content(content)
        await vector.upsert(
            # path 存完整 vault 相对路径（accounts/acc-001/knowledge/...），
            # 与 import_document 的 path、delete 里 markdown.forget(path) 一致——
            # 否则 delete 找不到 vault 文件，重启后知识从 vault 复活。
            [{"id": f"md:{rel}:{i}", "text": c, "path": rel, "source": "vault"}
             for i, c in enumerate(chunks)],
            account_id,
        )


def _audit_dir():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    return base / "BokVoice" / "audit"


def _audit(action: str, *, subject_type: str = "", subject_id: str = "", outcome: str = "ok", account_id: str = "", call_id: str = "", detail: dict | None = None) -> dict:
    """Emit an audit event (JSONL + optional DB copy) from a request context.

    call_id 显式传入优先(服务端已知时就别依赖调用方带头):correlation 头
    只覆盖 agent/web 上报路径,E2E/脚本直调端点时不带头,故端点自己传。
    """
    detail = detail or {}
    event = audit_store().emit(
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        outcome=outcome,
        account_id=account_id,
        call_id=call_id,
        detail=detail,
    )
    return event.to_dict()


def _utcnow_iso() -> str:
    """UTC 墙钟 naive ISO 串（落库/比较统一口径，与 campaign._utcnow_iso 同族）。

    内存仓 `updated_at` 存字符串、SQL 仓 DateTime 列 `fromisoformat` 收串，两后端
    都吃这一种形态；带 `+00:00` 后缀会与读侧 naive 比较产生偏移，故剥 tz。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "bok-voice-control-plane"}


@app.get("/api/settings")
def get_settings(request: Request, internal: bool = False) -> dict:
    # 设置=节点运维面（含云端凭据），话务员不可见；agent 机器通道直通。
    require_role(request, "admin", "root")
    raw = _repo().get_settings()
    if internal:
        return raw
    masked = {k: _mask_secrets(v) for k, v in raw.items() if k != "policy"}
    masked["policy"] = raw.get("policy", "offline_first")
    return masked


@app.put("/api/settings")
def put_settings(req: SettingsRequest, request: Request) -> dict:
    require_role(request, "admin", "root")
    existing = _repo().get_settings()
    new_values = {
        "asr": req.asr.model_dump(),
        "llm": req.llm.model_dump(),
        "tts": req.tts.model_dump(),
        "vad": req.vad.model_dump(),
        "sip": req.sip.model_dump(),
        "policy": req.policy,
    }
    secret_keys = {"api_key", "access_token", "token", "auth_password", "secret"}
    if req.sms is not None:
        # 通知域（W5-T1）：请求未带 sms 键 → 段不动（照 campaign 先例，
        # 仓库层缺键保留语义接管）。先入 new_values 再走下面 secret 保留循环，
        # PUT 传空 secret=保留旧值。
        new_values["sms"] = req.sms.model_dump()
    for kind in ("asr", "llm", "tts", "vad", "sip", "sms"):
        if kind not in new_values:
            continue  # 段未随请求带上（sms=None 等）→ 不动，secret 保留语义也不适用
        old = existing.get(kind, {})
        new = new_values[kind]
        for key in secret_keys:
            if not new.get(key) and old.get(key):
                new[key] = old[key]
        new_values[kind] = new
    if req.campaign is not None:
        # 全局外呼时段窗（T3b）：归一后落库（非法项静默丢弃、≤3 组），
        # 空/全非法=不限时段。请求未带 campaign 键（None）→ 段不动（仓库层
        # 缺键保留语义接管，不清运营已配的全局窗）。
        new_values["campaign"] = {"call_windows": parse_call_windows(req.campaign.call_windows)}
    raw = new_values
    saved = _repo().save_settings(raw)
    _audit("settings.save", subject_type="global_settings", subject_id="global", detail={"llm_provider": raw.get("llm", {}).get("provider", "")})
    masked = {k: _mask_secrets(v) for k, v in saved.items() if k != "policy"}
    masked["policy"] = saved.get("policy", "offline_first")
    return masked


def _mask_secrets(config: dict) -> dict:
    out = dict(config)
    secret_keys = {"api_key", "access_token", "token", "auth_password", "secret"}
    for key in secret_keys:
        if out.get(key):
            out[key] = ""
            out[f"has_{key}"] = True
    return out


def _sms_settings() -> dict:
    """settings.sms 段读取（W5-T1）：端点与挂断钩子共用，缺段=空 dict=未配置。"""
    return (_repo().get_settings() or {}).get("sms") or {}


def _sms_configured(cfg: dict) -> bool:
    """webhook provider 三要素齐备才算配置：总闸开 + webhook_url + secret。"""
    return bool(
        cfg.get("enabled")
        and str(cfg.get("webhook_url") or "").strip()
        and str(cfg.get("secret") or "").strip()
    )


async def _send_sms_webhook(cfg: dict, to: str, text: str) -> int:
    """webhook provider 发送内核（W5-T1 骨架，真实网关未来对接）。

    POST settings.sms.webhook_url，body={"to","text"}；secret 非空时附
    `X-Bok-Signature: hmac_sha256(secret, body)` hex 摘要（接收方以同一密钥
    重算校验，参照 CP webhook 验签的「摘要绑定 body」思路反向运用）。
    返回上游 status_code；网络/HTTP 失败抛异常由调用方收敛（端点 502 /
    挂断钩子打点吞掉）。
    """
    url = str(cfg.get("webhook_url") or "").strip()
    secret = str(cfg.get("secret") or "")
    body = json.dumps({"to": to, "text": text}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Bok-Signature"] = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, content=body, headers=headers)
        resp.raise_for_status()
        return resp.status_code


@app.post("/api/notify/sms")
async def notify_sms(payload: dict, request: Request) -> dict:
    """短信 webhook provider（W5-T1 骨架）：真实网关未来对接，webhook_url 即对接点。

    发送烧真实外部配额，auth-on 时归管理面（同 tts.preview 判据）。
    body {call_id, to, text}：to 空=按 call_id 反查 contact_phone，都空 400；
    未配置（disabled/空 webhook_url/空 secret）→ 503；上游失败一律 502。
    审计 sms.send（detail 带 chars/status_code，secret/webhook_url 永不落审计）。
    """
    require_role(request, "admin", "root")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text 必填")
    to = str(payload.get("to") or "").strip()
    call_id = str(payload.get("call_id") or "").strip()
    if not to and call_id:
        call = _repo().get_call(call_id)
        if not call:
            raise HTTPException(404, "call not found")
        to = str(call.get("contact_phone") or "").strip()
    if not to:
        raise HTTPException(400, "to 与 call_id 至少给一个（且该通话须有 contact_phone）")
    cfg = _sms_settings()
    if not _sms_configured(cfg):
        raise HTTPException(503, "短信 webhook 未配置：请到「设置 → 通知」填写 webhook URL 与 secret 并启用")
    try:
        status_code = await _send_sms_webhook(cfg, to, text)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"webhook 发送失败: {exc}") from exc
    _audit("sms.send", subject_type="sms", subject_id=(call_id or to)[:64],
           call_id=call_id, detail={"chars": len(text), "status_code": status_code, "source": "api"})
    return {"ok": True, "provider": "webhook", "status_code": status_code}


@app.get("/api/asr/health")
async def asr_health(request: Request) -> dict:
    # P3-A（2026-09-17 全量 debug）：sidecar 诊断读面归管理面（settings 键）——
    # auth-on 下任意 user JWT 曾可探 sidecar 健康/读克隆音色清单。auth-off 直通。
    _gate_page(request, "settings")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{_qwen3_asr_url()}/health")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/tts/health")
async def tts_health(request: Request) -> dict:
    _gate_page(request, "settings")  # P3-A：诊断读面归管理面
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{_qwen3_tts_url()}/health")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/tts/speakers")
async def tts_speakers(request: Request) -> list[str]:
    _gate_page(request, "settings")  # P3-A：诊断读面归管理面
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_qwen3_tts_url()}/v1/speakers")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/tts/voices")
async def tts_voices(request: Request) -> list[dict]:
    _gate_page(request, "settings")  # P3-A：诊断读面归管理面（克隆音色清单不外泄）
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_qwen3_tts_url()}/v1/voices")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/tts/voices/{voice_id}")
async def tts_delete_voice(voice_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    """删除已克隆音色（preset 不可删；同步清理 registry/缓存/参考音频）。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(f"{_qwen3_tts_url()}/v1/voices/{voice_id}")
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail=f"clone voice not found: {voice_id}")
            resp.raise_for_status()
            body = resp.json()
            # 清理引用了该音色的人设（reference_audio 是 {lang: voice_id} JSON，去掉该 voice_id 键）。
            try:
                for p in _repo().list_personas(""):
                    raw = p.get("reference_audio") or ""
                    if voice_id not in raw:
                        continue
                    try:
                        mapping = json.loads(raw)
                        if isinstance(mapping, dict):
                            before = dict(mapping)
                            mapping = {k: v for k, v in mapping.items() if v != voice_id}
                            if mapping != before:
                                _repo().update_persona(p["id"], {"reference_audio": json.dumps(mapping, ensure_ascii=False)})
                    except Exception:
                        continue
            except Exception as exc:  # pragma: no cover - 引用清理失败不阻塞删除
                print(f"[cp] clean persona refs failed: {exc!r}", flush=True)
            _audit(
                "voice.delete",
                subject_type="tts_voice",
                subject_id=voice_id,
                detail={"voice_id": voice_id},
            )
            return body
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/tts/voices")
async def tts_register_voice(
    request: Request,
    file: UploadFile = File(...),
    voice_id: str = Form(...),
    ref_text: str = Form(...),
    language: str = Form("zh"),
) -> dict:
    require_role(request, "admin", "root")
    try:
        files = {"file": (file.filename or "reference.wav", await file.read())}
        data = {
            "voice_id": voice_id,
            "ref_text": ref_text,
            "language": language,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{_qwen3_tts_url()}/v1/voices/register",
                files=files,
                data=data,
            )
            resp.raise_for_status()
            body = resp.json()
            _audit(
                "voice.clone",
                subject_type="tts_voice",
                subject_id=voice_id,
                detail={"language": language, "ref_text_len": len(ref_text), "voice_id": voice_id},
            )
            return body
    except Exception as exc:
        _audit(
            "voice.clone",
            subject_type="tts_voice",
            subject_id=voice_id,
            outcome="error",
            detail={"language": language, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---- MiniMax 云端声音克隆（路线 B，2026-09-18）----
# 官方两步：/v1/files/upload(purpose=voice_clone) → /v1/voice_clone。克隆音色与
# 系统音色走同一 voice_setting.voice_id 通道（t2a_v2 / bidi 双支持）——运行时
# 零改动，B 线会话级音色选择器/设置页三键天然可挂。
# 拍板「先克隆不激活」（2026-09-18）：克隆请求不带 text/model（不触发合成=
# 0 费用）；MiniMax 规则——克隆后 7 天内未用于合成会被删除、首次用于合成才收
# ¥9.9/音色复刻费，即试听或首次会话使用即激活计费。账号需实名/企业认证（未
# 认证 2038）。清单存 tts.minimax_clones_json（settings blob，免 DB 迁移）。


def _minimax_clone_base() -> str:
    """克隆端点基址（…/v1）：MINIMAX_BASE_URL 覆盖优先（容忍 /v1/t2a_v2 全形态，
    剥回 /v1）；否则按 region——cn=api.minimax.cn（现行国内）；intl 沿用 TTS 同款
    遗留域 api.minimax.chat（海外现行文档为 api.minimax.io，账号区不通时 env 覆盖）。"""
    base = os.environ.get("MINIMAX_BASE_URL", "").strip().rstrip("/")
    if base:
        return base[: -len("/t2a_v2")] if base.endswith("/t2a_v2") else base
    region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
    return "https://api.minimax.chat/v1" if region in {"intl", "global", "chat"} else "https://api.minimax.cn/v1"


def _minimax_api_key() -> str:
    """照 /api/tts/preview 同源：settings 持久化 tts.api_key 优先，env 兜底。"""
    tts_settings = (_repo().get_settings() or {}).get("tts") or {}
    return (str(tts_settings.get("api_key") or "").strip()) or os.environ.get("MINIMAX_API_KEY", "")


def _minimax_clones_list(tts_settings: dict) -> list[dict]:
    raw = str(tts_settings.get("minimax_clones_json") or "[]")
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _save_minimax_clones(clones: list[dict]) -> None:
    # save_settings 是整段替换语义——读全量、只改 tts 段、原样回写其余 sections。
    settings = _repo().get_settings() or {}
    tts_blob = dict(settings.get("tts") or {})
    tts_blob["minimax_clones_json"] = json.dumps(clones, ensure_ascii=False)
    settings["tts"] = tts_blob
    _repo().save_settings(settings)


@app.get("/api/tts/minimax-voices")
async def tts_minimax_voices_list(request: Request) -> list[dict]:
    # 克隆清单是本地面板数据（settings blob），读面归管理面（同 tts_voices）。
    _gate_page(request, "settings")
    return _minimax_clones_list((_repo().get_settings() or {}).get("tts") or {})


@app.post("/api/tts/minimax-voices")
async def tts_minimax_voice_clone(
    request: Request,
    file: UploadFile = File(...),
    label: str = Form(""),
    sample_lang: str = Form("zh"),
) -> dict:
    """参考音频 → MiniMax 云端克隆 voice_id（不激活、0 费用；见节首注释）。"""
    require_role(request, "admin", "root")
    label = label.strip()[:64]
    sample_lang = (sample_lang.strip().lower() or "zh")[:16]
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="参考音频为空")
    if len(audio) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="参考音频超过 20MB 上限（官方规则 ≤20MB，10s~5min）")
    api_key = _minimax_api_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="MiniMax API Key 未配置：请到「设置 → TTS 语音合成」填写 API Key 并保存")
    base = _minimax_clone_base()
    headers = {"Authorization": f"Bearer {api_key}"}
    # 命名规则（官方）：[8,256]、首字符字母、仅字母/数字/-/_、尾字符不可 -/_；
    # bokclone 前缀避开本地 Qwen3 克隆前缀 agent-/acceptance-（B/A 线
    # _cloud_voice 过滤闸会把它当本地音色剔除，2054 防线不受影响）。
    voice_id = f"bokclone{uuid.uuid4().hex[:8]}"

    def _audit_clone(outcome: str = "ok", **extra: Any) -> None:
        _audit("voice.clone", subject_type="minimax_voice", subject_id=voice_id,
               outcome=outcome, detail={"label": label, "sample_lang": sample_lang, **extra})

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            up = await client.post(
                f"{base}/files/upload",
                headers=headers,
                data={"purpose": "voice_clone"},
                files={"file": (file.filename or "reference.wav", audio)},
            )
            up.raise_for_status()
            up_body = up.json()
            file_id = ((up_body.get("file") or {}) if isinstance(up_body, dict) else {}).get("file_id")
            if not file_id:
                _audit_clone("error", step="files.upload")
                raise HTTPException(status_code=502, detail=f"MiniMax 参考音频上传失败: {json.dumps(up_body, ensure_ascii=False)[:256]}")
            clone = await client.post(
                f"{base}/voice_clone",
                headers={**headers, "Content-Type": "application/json"},
                json={"file_id": file_id, "voice_id": voice_id},
            )
            clone.raise_for_status()
            clone_body = clone.json()
            base_resp = clone_body.get("base_resp") or {}
            code = base_resp.get("status_code")
            if code != 0:
                msg = str(base_resp.get("status_msg") or f"status_code={code}")
                _audit_clone("error", step="voice_clone", minimax_code=code)
                if code == 2038:
                    raise HTTPException(status_code=403, detail="MiniMax 账号无复刻权限（2038）：请先在 MiniMax 平台完成实名/企业认证")
                raise HTTPException(status_code=502, detail=f"MiniMax voice_clone 失败: {msg}")
    except HTTPException:
        raise
    except Exception as exc:
        _audit_clone("error", step="http", error=str(exc)[:256])
        raise HTTPException(status_code=502, detail=f"MiniMax 克隆请求失败: {exc}") from exc

    clones = _minimax_clones_list((_repo().get_settings() or {}).get("tts") or {})
    clones.append({
        "voice_id": voice_id,
        "label": label or voice_id,
        "sample_lang": sample_lang,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # 未激活：7 天内首次合成（试听/会话）才计费 ¥9.9 并正式生效。
        "activated": False,
    })
    _save_minimax_clones(clones)
    _audit_clone()
    return {"voice_id": voice_id, "label": label or voice_id, "sample_lang": sample_lang,
            "activated": False, "note": "未激活：7 天内首次合成（试听/会话使用）即激活并计费 ¥9.9/音色"}


@app.delete("/api/tts/minimax-voices/{voice_id}")
async def tts_minimax_voice_delete(voice_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    api_key = _minimax_api_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="MiniMax API Key 未配置：请到「设置 → TTS 语音合成」填写 API Key 并保存")
    base = _minimax_clone_base()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base}/delete_voice",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"voice_type": "voice_cloning", "voice_id": voice_id},
            )
            resp.raise_for_status()
            body = resp.json()
            base_resp = body.get("base_resp") or {}
            if base_resp.get("status_code") != 0:
                raise HTTPException(status_code=502, detail=f"MiniMax delete_voice 失败: {base_resp.get('status_msg') or base_resp}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MiniMax 删除音色失败: {exc}") from exc
    # 本地清单移除 + 人设引用清理（reference_audio 是 {lang: voice_id} JSON）。
    clones = [c for c in _minimax_clones_list((_repo().get_settings() or {}).get("tts") or {})
              if str(c.get("voice_id")) != voice_id]
    _save_minimax_clones(clones)
    for p in _repo().list_personas(""):
        raw = p.get("reference_audio") or ""
        if voice_id not in raw:
            continue
        try:
            mapping = json.loads(raw)
            if isinstance(mapping, dict):
                before = dict(mapping)
                mapping = {k: v for k, v in mapping.items() if v != voice_id}
                if mapping != before:
                    _repo().update_persona(p["id"], {"reference_audio": json.dumps(mapping, ensure_ascii=False)})
        except Exception:
            continue
    _audit("voice.delete", subject_type="minimax_voice", subject_id=voice_id, detail={"voice_id": voice_id})
    return {"ok": True, "voice_id": voice_id}


@app.get("/api/tts/filler-preview")
def tts_filler_preview(lang: str = "zh", i: int = 0, request: Request = None) -> Response:
    """垫话资产试听（2026-09-11 症状④）：直接吐源码 wav（随包分发,零云调用）。

    i=池内索引(取模轮换),web 端随机传即「换一句试听」。浏览器按 wav 头原生
    播放=正确速率;房间内 48k 混音器错配是 agent 播放路径问题,与此端点无关。"""
    # P3-A（2026-09-17 全量 debug）：资产文件读面归管理面（settings 键）；
    # request 默认 None 兜底（历史直接调用不炸）——FastAPI 路由恒注入。
    if request is not None:
        _gate_page(request, "settings")
    from fastapi.responses import FileResponse

    assets = Path(__file__).resolve().parents[2] / "agent" / "agent_runtime" / "assets" / "fillers"
    manifest_path = assets / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="filler assets not found")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        pool = manifest.get(lang) or manifest.get("zh") or []
        if not pool:
            raise HTTPException(status_code=404, detail=f"no filler pool for lang={lang}")
        entry = pool[int(i) % len(pool)]
        wav = assets / str(entry.get("file") or "")
        if not wav.exists():
            raise HTTPException(status_code=404, detail="filler wav missing")
        return FileResponse(str(wav), media_type="audio/wav", filename=str(entry.get("file") or "filler.wav"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"filler preview failed: {exc}") from exc


@app.post("/api/tts/preview")
async def tts_preview(payload: dict, request: Request) -> Response:
    # 试听会真实消耗云端 TTS 配额（MiniMax），auth-on 时归管理面。
    require_role(request, "admin", "root")
    """试听一段 TTS。provider=qwen3_tts 走本地 sidecar；provider=minimax 走云端 MiniMax
    （voice 是 MiniMax 音色 ID，如 Cantonese_Male_news_anchor_vv2）。返回 WAV。"""
    provider = str(payload.get("provider") or "qwen3_tts").lower()
    sample_rate = int(payload.get("sample_rate") or 24000)
    text = str(payload.get("text") or "")
    voice = str(payload.get("voice") or "")
    language = str(payload.get("language") or "zh")
    # 试听烧真金(MiniMax 云配额)却从不留痕——voice.clone/delete 同族操作都审计
    # (2026-09-17 全量 debug F10 补齐)。
    _audit("tts.preview", subject_type="tts_voice", subject_id=voice[:128],
           detail={"provider": provider, "language": language, "chars": len(text)})
    if not voice and provider in ("minimax", "minimax_streaming"):
        # qa_entries.voice_id 可空(罐头物化时音色取自人设而非词条字段)——试听
        # 按语言回落 agent 同一套默认音色(_MINIMAX_DEFAULT_VOICES 同步,2026-09-12
        # zh 换克隆 moss),否则空 voice 被 MiniMax 拒。
        voice = {
            "zh": "moss_audio_aaa1346a-7ce7-11f0-8e61-2e6e3c7ee85d",
            "cantonese": "Cantonese_crisp_news_anchor_vv2",
            "en": "English_magnetic_voiced_man",
        }.get(language, "moss_audio_aaa1346a-7ce7-11f0-8e61-2e6e3c7ee85d")
    try:
        if provider in ("minimax", "minimax_streaming"):
            # 与 agent 一致：优先读设置库里持久化的 tts.api_key，环境变量仅作兜底，
            # 否则「设置页已保存 Key 却在 bok serve 重启后试听 503」。
            tts_settings = (_repo().get_settings() or {}).get("tts") or {}
            api_key = (str(tts_settings.get("api_key") or "").strip()) or os.environ.get("MINIMAX_API_KEY", "")
            if not api_key:
                raise HTTPException(status_code=503, detail="MiniMax API Key 未配置：请到「设置 → TTS 语音合成」填写 API Key 并保存")
            region = os.environ.get("MINIMAX_REGION", "cn").strip().lower()
            base = os.environ.get("MINIMAX_BASE_URL", "").strip().rstrip("/")
            if not base:
                base = "https://api.minimax.chat/v1/t2a_v2" if region in {"intl", "global", "chat"} else "https://api.minimax.cn/v1/t2a_v2"
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    base,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": os.environ.get("MINIMAX_MODEL", "speech-2.8-hd"),
                        "text": text,
                        # 语速与运行时同一条语言档规则(zh/粤 1.2,见 agent_runtime
                        # minimax_speed_for;CP 进程不引 agent 包,同规则内联)——
                        # 否则试听节奏与真通话不一致,试了白试。
                        "voice_setting": {
                            "voice_id": voice,
                            "speed": 1.2 if language in ("zh", "cantonese") else 1.0,
                            "vol": 1,
                            "pitch": 0,
                        },
                        "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                audio_hex = (body.get("data") or {}).get("audio") or ""
                if not audio_hex:
                    base_resp = body.get("base_resp") or {}
                    code = base_resp.get("status_code")
                    if code == 2054:
                        # 无效音色 ID(如本地 Qwen3 克隆名/过期 id)逐句 beep 的根源;
                        # 直接点明,唔再抛看不懂的 502 "minimax empty"。
                        raise HTTPException(
                            status_code=400,
                            detail="MiniMax 音色 ID 无效（voice id not exist）：请从音色列表中选择，或核对粘贴的 ID。",
                        )
                    raise HTTPException(status_code=502, detail=f"minimax empty: {base_resp}")
                pcm = bytes.fromhex(audio_hex)
        else:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{_qwen3_tts_url()}/v1/audio/speech",
                    json={
                        "input": text,
                        "voice": voice,
                        "language": language,
                        "instruct": payload.get("instruct", ""),
                        "sample_rate": sample_rate,
                        "response_format": "pcm",
                    },
                )
                resp.raise_for_status()
                pcm = resp.content
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        return Response(content=buffer.getvalue(), media_type="audio/wav")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---- 三层 RBAC 认证（路线 B1）：登录 / 身份 / 改密 / 账号管理 ----
# 门禁开关 BOK_AUTH_REQUIRED（见 auth.py）：未设=开放（单机形态零变化）。
# 账号管理端点带 Authorization 头时按身份收紧（root 全域 / admin 仅本账号 user /
# user 无权）；无头（auth-off 开发形态）放行，与该形态其余端点同信任级。


def _user_public(user: dict) -> dict:
    """users dict 出仓形态：剥凭据材料 + 换算出仓权限键。

    password_hash 任何情况下不出仓；B4 的 `permissions_json` 是存储细节（原始串）
    同样不外泄，出仓键 `permissions` 恒为有效集（见 permissions.effective_permissions）。
    """
    out = {k: v for k, v in user.items() if k not in ("password_hash", "permissions_json")}
    out["permissions"] = effective_permissions(
        str(user.get("role") or ""), str(user.get("permissions_json") or "")
    )
    return out


def _gate_page(request: Request, key: str) -> None:
    """B4 页面权限闸：主管按人配置 user 的可见面，逐请求查库（改权限即时生效）。

    语义（CONTRACT §2）：无身份（auth-off/机器通道）直通；admin/root（token 角色）
    直通；user → 查库算有效集，缺键 403。用户行的 role 参与计算（行上被提升为
    admin 的旧 token 也不误杀），行缺失按 '' 兜底=默认集。本闸只判页面面，
    与 B2 账号闸 / B3 归属闸叠加（本闸先判，逐请求查库改权限即时生效）。
    """
    ident = current_identity(request)
    if ident is None or ident.role in ("admin", "root"):
        return
    user = _repo().get_user(ident.user_id) or {}
    perms = effective_permissions(
        str(user.get("role") or ident.role), str(user.get("permissions_json") or "")
    )
    if key not in perms:
        raise HTTPException(status_code=403, detail="forbidden")


def _permissions_payload(permissions: list[str] | None) -> str:
    """写入形态：None → ''（默认集）；list → 目录序去重的 JSON 数组串（'[]'=全关）。

    校验（键白名单 / 目标角色）在 `_validate_user_permissions`，调用方先过闸。
    """
    if permissions is None:
        return ""
    wanted = {str(k) for k in permissions}
    return json.dumps([k for k in PAGE_PERMISSIONS if k in wanted], ensure_ascii=False)


def _validate_user_permissions(target_role: str, permissions: list[str] | None) -> None:
    """B4 权限写入校验：只对 role=user 目标；键必须 ⊆ grantable（主管专属永不进目录）。"""
    if permissions is None:
        return
    if target_role != "user":
        raise HTTPException(400, "permissions 仅适用于 role=user 的账号")
    unknown = sorted({str(k) for k in permissions} - GRANTABLE_PERMISSIONS)
    if unknown:
        raise HTTPException(400, f"未知权限键: {', '.join(unknown)}")


def _hardened_auth() -> bool:
    """加固模式判定:auth-on 或 BOK_CP_TOKEN 单设任一(node_license_required 同判据)。"""
    return auth_required() or bool(os.environ.get("BOK_CP_TOKEN", "").strip())


# P1-C（2026-09-17 全量 debug）：/api/token 按调用方身份频控——防「持 calls 页键
# 的 user JWT 批量签发/撞房间名」资源滥用。30 次/分钟/身份，进程内滑动窗口
# （CP 单进程形态够用）；身份键=user:<id>/machine/anon，互不挤占（P3-A web_logs
# 全局窗教训同族：单 deque 全局共享时一个调用方可压干所有调用方的额度）。
_TOKEN_RATE_LIMIT = 30
_TOKEN_RATE_WINDOW_S = 60.0
_token_issue_times: dict[str, deque] = {}


def _token_rate_limit(request: Request) -> None:
    """/api/token per-identity 滑动窗口频控：超限 429，放行记时间戳。"""
    ident = current_identity(request)
    if ident is not None:
        key = f"user:{ident.user_id}"
    elif getattr(request.state, "machine", False):
        key = "machine"
    else:
        key = "anon"
    import time as _time

    now = _time.monotonic()
    dq = _token_issue_times.setdefault(key, deque())
    while dq and now - dq[0] >= _TOKEN_RATE_WINDOW_S:
        dq.popleft()
    if len(dq) >= _TOKEN_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="token issuance rate limited (30/min per identity)")
    dq.append(now)


def _require_user_admin(identity: Identity | None, target_role: str, target_account: str) -> None:
    if identity is None:
        # 加固模式下 identity=None=机器通道(BOK_CP_TOKEN 持有者)——账号管理面
        # 不得放行,否则任意 agent worker 进程可铸 root/改 root 密码(2026-09-17
        # 全量 debug F1 修复;require_role 的机器直通语义只该覆盖业务工作台,
        # 不该覆盖 users 管理面)。真 auth-off(双关,本机单用户)原样直通。
        if _hardened_auth():
            raise HTTPException(403, "machine channel not allowed on user admin")
        return
    if identity.role == "root":
        return
    if identity.role == "admin":
        if target_role != "user":
            raise HTTPException(403, "admin 只能创建/管理 user 角色")
        if target_account and target_account != identity.account_id:
            raise HTTPException(403, "admin 只能管理本账号成员")
        return
    raise HTTPException(403, "无账号管理权限")


@app.post("/api/auth/login")
def auth_login(req: LoginRequest) -> dict:
    user = _repo().get_user_by_username(req.username.strip())
    stored = str((user or {}).get("password_hash") or "")
    # 时序均衡且 fail-closed：user 缺失或其 hash 为空（数据异常）都跑同价位
    # dummy scrypt（均衡），但空 hash 的真实账号恒判失败（不得用公开 dummy 口令通过）。
    ok = verify_password(req.password, stored or _DUMMY_PASSWORD_HASH) and bool(stored)
    if not user or user.get("status") != "active" or not ok:
        _audit("auth.login_failed", subject_type="user", subject_id=req.username[:64], outcome="denied")
        raise HTTPException(401, "用户名或密码不正确")
    identity = Identity(
        user_id=str(user["id"]),
        username=str(user["username"]),
        role=str(user["role"]),
        org_id=str(user.get("org_id") or ""),
        account_id=str(user.get("account_id") or ""),
    )
    token = create_token(identity)
    _audit("auth.login", subject_type="user", subject_id=str(user["id"]))
    return {"token": token, "token_type": "bearer", "expires_in": JWT_TTL_S, "user": _user_public(user)}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(401, "missing or invalid token")
    # B4：权限逐请求查库（JWT 只装身份）——主管改权限对已签发 token 即时生效；
    # 行缺失（删号后旧 token）按 '' 兜底不炸（=默认集，见 permissions.py 模块注释）。
    user = _repo().get_user(identity.user_id) or {}
    return {
        "user_id": identity.user_id,
        "username": identity.username,
        "display_name": str(user.get("display_name") or ""),
        "role": identity.role,
        "org_id": identity.org_id,
        "account_id": identity.account_id,
        "permissions": effective_permissions(
            str(user.get("role") or identity.role), str(user.get("permissions_json") or "")
        ),
    }


@app.post("/api/auth/change-password")
def auth_change_password(req: ChangePasswordRequest, request: Request) -> dict:
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(401, "missing or invalid token")
    if len(req.new_password) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    user = _repo().get_user(identity.user_id)
    if not user or not verify_password(req.old_password, str(user.get("password_hash") or "")):
        raise HTTPException(401, "原密码不正确")
    _repo().update_user(user["id"], password_hash=hash_password(req.new_password))
    _audit("auth.password_changed", subject_type="user", subject_id=str(user["id"]))
    return {"changed": True}


@app.post("/api/users")
def create_user(req: CreateUserRequest, request: Request) -> dict:
    identity = current_identity(request)
    if req.role not in ("root", "admin", "user"):
        raise HTTPException(400, "role 必须是 root/admin/user")
    if len(req.password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    _require_user_admin(identity, req.role, req.account_id)
    # B4 页面权限：仅 role=user 目标可带，键 ⊆ grantable；缺省=None → 存 ''（默认集）。
    _validate_user_permissions(req.role, req.permissions)
    if _repo().get_user_by_username(req.username.strip()):
        raise HTTPException(409, "用户名已存在")
    user = _repo().create_user(
        username=req.username.strip(),
        password_hash=hash_password(req.password),
        role=req.role,
        org_id=req.org_id or (identity.org_id if identity else ""),
        # admin 建话务员：不传 account 时默认落 admin 本账号。
        account_id=req.account_id or (identity.account_id if identity and identity.role == "admin" else ""),
        display_name=req.display_name,
        permissions_json=_permissions_payload(req.permissions),
    )
    _audit("user.create", subject_type="user", subject_id=str(user["id"]),
           account_id=str(user.get("account_id") or ""))
    return _user_public(user)


@app.get("/api/users")
def list_users(request: Request, account_id: str = "") -> dict:
    identity = current_identity(request)
    if identity is None:
        # 加固模式下机器通道/裸 token 不得列全账号用户(与 _require_user_admin 同洞)。
        if _hardened_auth():
            raise HTTPException(403, "machine channel not allowed on user admin")
    elif identity.role == "user":
        raise HTTPException(403, "无账号管理权限")
    # admin 强制本账号视角；root/auth-off 按参过滤（空=全部）。
    acct = identity.account_id if (identity and identity.role == "admin") else account_id
    return {"users": [_user_public(u) for u in _repo().list_users(acct)]}


@app.patch("/api/users/{user_id}")
def update_user(user_id: str, req: UpdateUserRequest, request: Request) -> dict:
    identity = current_identity(request)
    if identity is None and _hardened_auth():
        # identity=None 在加固模式下=机器通道:不得改任意用户(root 密码重置路径)。
        raise HTTPException(403, "machine channel not allowed on user admin")
    target = _repo().get_user(user_id)
    if not target:
        raise HTTPException(404, "user not found")
    if identity:
        if identity.role == "admin":
            if target.get("account_id") != identity.account_id or target.get("role") == "root":
                raise HTTPException(403, "admin 只能管理本账号的非 root 成员")
        elif identity.role != "root":
            raise HTTPException(403, "无账号管理权限")
    if req.role:
        if not identity or identity.role != "root":
            raise HTTPException(403, "仅 root 可改角色")
        if req.role not in ("root", "admin", "user"):
            raise HTTPException(400, "role 必须是 root/admin/user")
    if req.password and len(req.password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if req.status and req.status not in ("active", "disabled"):
        raise HTTPException(400, "status 必须是 active/disabled")
    fields: dict = {}
    if req.password:
        fields["password_hash"] = hash_password(req.password)
    if req.status:
        fields["status"] = req.status
    if req.display_name:
        fields["display_name"] = req.display_name
    if req.role:
        fields["role"] = req.role
    if req.permissions is not None:
        # B4：目标角色取**现存**角色（改角色与改权限同 request 时以旧角色判，
        # 免一次请求内绕过「permissions 仅限 user 目标」）；None=不改。
        _validate_user_permissions(str(target.get("role") or ""), req.permissions)
        fields["permissions_json"] = _permissions_payload(req.permissions)
    updated = _repo().update_user(user_id, **fields)
    _audit("user.update", subject_type="user", subject_id=user_id,
           account_id=str(target.get("account_id") or ""),
           detail={"fields": sorted(fields.keys()), "status": req.status})
    return _user_public(updated or target)


@app.post("/api/token", response_model=TokenResponse, status_code=201)
def token(req: TokenRequest, request: Request) -> TokenResponse:
    """签发参与者 token——LiveKit 官方 TokenSource endpoint 契约。

    请求体兼容两种形态:官方 TokenSourceRequest(snake_case: room_name /
    participant_identity,由 TokenSource.endpoint / 任意官方 SDK 直发)与本项目
    业务字段(call_id/role)。响应即官方 TokenSourceResponse({serverUrl,
    participantToken}),任何按标准实现的客户端(playground/Swift/Flutter…)可直接消费。
    """
    # B2：auth-on 时房 token 需要身份（机器通道直通）。auth-off 开发形态全部放行。
    # 注意用 _ident：本函数后文的 `identity` 是参与者身份字符串（官方字段），
    # 会遮蔽此处的用户身份对象。supervisor 语义 403 闸见下方角色解析之后——
    # 必须等 role/is_listen 定型（含 participant_identity 前缀反推）才判得准。
    _ident = current_identity(request)
    _machine = getattr(request.state, "machine", False)
    if auth_required() and _ident is None and not _machine:
        raise HTTPException(status_code=401, detail="unauthorized")
    key = getattr(app.state, "lk_key", "") or os.environ.get("LIVEKIT_API_KEY", "")
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    url = getattr(app.state, "lk_url", "") or os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if not key or not secret:
        # 必须走真实 JWT：旧 sha256 兜底会让前端 decodeTokenPayload 抛错、
        # LiveKit 服务器 401，A 线永远接不通。缺凭据时显式失败，不静默回退。
        raise HTTPException(status_code=503, detail="LiveKit credentials not configured")
    # 房间名:官方 room_name 优先,旧 call_id 兼容;房间名 == call_id 是全栈约定
    # (agent/前端按它反查 CallSession)。不再为空请求悄悄造随机房——token 必须绑定已知通话。
    room = (req.room_name or req.call_id or "").strip()
    if not room:
        raise HTTPException(status_code=400, detail="room_name (or call_id) is required")
    # P1-C 频控插在凭据校验之后：缺凭据 503 行为不被频控扰动（load_cp_concurrency
    # 场景 D 依赖「无凭据稳定 503」），频控只保护真实签发路径。
    _token_rate_limit(request)
    import datetime
    from livekit import api

    # 角色:官方路径(显式 participant_identity)按我方身份前缀反推;否则用业务 role 字段。
    # 同传(B 线 v2)双端:me=我方端(通常也是房间创建者),other=对方端;
    # A 线 operator / 主管 supervisor。前缀是 interpreter agent 判定"听谁的麦/译文给谁"的约定。
    identity_input = (req.participant_identity or "").strip()
    role = (req.role or "operator").strip().lower()
    if identity_input:
        if identity_input.startswith("me-"):
            role = "me"
        elif identity_input.startswith("other-"):
            role = "other"
        elif identity_input.startswith("supervisor-"):
            role = "supervisor"
        else:
            role = "operator"
    # 旁听专线（purpose=listen）：主管静默旁听——角色钉 supervisor（只订阅不发布）。
    # 官方路径带 participant_identity 时不覆盖（身份由调用方保证）。
    _purpose = (req.purpose or "").strip().lower()
    is_listen = _purpose == "listen"
    if is_listen and not identity_input:
        role = "supervisor"
    # supervisor 语义闸（2026-09-16 深测 P2）：**移到角色解析之后**——旧闸只看
    # req.role/req.purpose 字段，participant_identity="supervisor-<room>" 走前缀
    # 反推在闸后改写 role=supervisor，话务员可自签主管身份房 token（第三条绕过路）。
    # 显式字段与前缀两条路都由最终 role 统一把关；is_listen 单列（grants 语义不同）。
    if _ident is not None and _ident.role not in ("root", "admin") and (
        role == "supervisor" or is_listen
    ):
        raise HTTPException(status_code=403, detail="forbidden")
    if role == "me":
        name = "Bok Interpret Me"
    elif role == "other":
        name = "Bok Interpret Other"
    elif role == "supervisor":
        name = "Bok Voice Supervisor"
    else:
        name = "Bok Voice Operator"
    identity = identity_input or (
        f"me-{room}" if role == "me"
        else f"other-{room}" if role == "other"
        else f"supervisor-{room}" if role == "supervisor"
        else f"operator-{req.account_id}-{room}"
    )
    # P1-C（2026-09-17 全量 debug）：先取通话记录，记录缺失（新建流/竞态/编造
    # 房间名）的房间降为 subscribe-only 且不挂 agent dispatch——旧版对任意编造
    # room 名签 can_publish=True token 并触发 CP 创建真实 dispatch（资源空转、
    # 幽灵房 reaper 不可见——无 call 记录可循）。改前 grep scripts/ 全量核查 E2E
    # 依赖：三套 E2E（trilingual/barge-in/edge_cases）与全部 probe/soak/acceptance
    # 都是先 POST /api/calls 建单、再拿 call["id"] 当房名取 token——记录恒存在，
    # 发布+dispatch 语义不变；唯一无记录用例是 scripts/load_cp_concurrency.py
    # 场景 D（load-{i}×50）：其自起 CP 无 LiveKit 凭据、依赖缺凭据 503（该检查
    # 在频控之前，行为不变），有凭据下拿 subscribe-only token 也只是不出 500，
    # 脚本断言不受扰；doctor 的 token 探针只验三段式 JWT，同样不受扰。
    _call: dict = {}
    try:
        _call = _repo().get_call(room) or {}
    except Exception:
        _call = {}
    _recordless = not _call
    # 旁听专线 grants：can_publish/can_publish_data 全关——主管旁听绝不向通话
    # 注入音频或数据；can_subscribe 保证能收双方音轨与字幕流。P1-C：无记录房间
    # 同款 subscribe-only（is_listen 本来就是零发布，两档合一）。
    _grants = (
        api.VideoGrants(room_join=True, room=room, can_publish=False,
                        can_subscribe=True, can_publish_data=False)
        if (is_listen or _recordless)
        else api.VideoGrants(room_join=True, room=room, can_publish=True,
                             can_subscribe=True, can_publish_data=True)
    )
    at = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(req.participant_name or name)
        .with_grants(_grants)
        .with_ttl(datetime.timedelta(seconds=3600))
    )
    if req.participant_metadata:
        at = at.with_metadata(req.participant_metadata)
    # 业务维度放 participant attributes(官方机制):agent/前端按属性判定角色,
    # 替代对 identity 前缀的字符串嗅探;SIP 接入时同通道补 bok.* 属性。
    at = at.with_attributes({"bok.role": role, "bok.account_id": req.account_id})
    # 熔断产品路径（site-delivery M1）：通话建单时绑定了承载节点（call.node_id）
    # → 签发房 token 前校验该节点未吊销——覆盖「坐席 JWT 建单、非 node_token 通道」
    # 的 thin-node 拓扑（窒息点中间件只拦 node_token Bearer，这条拦通话绑定）。
    # 绑定节点查不到（已删/竞态）保守放行，与幽灵守卫同口径；只有存在且 revoked 才拒。
    _bound_node = str(_call.get("node_id") or "")
    if _bound_node:
        _bnode = _node_store().node_by_id(_bound_node)
        if _bnode is not None and _bnode.get("revoked"):
            raise HTTPException(status_code=403, detail="node revoked")
    # B4 页面权限：A 线通话归 calls 键，同传会话（kind=interpret）归 interpret 键
    # （与建单同口径）；通话记录缺失（新建流/竞态）按 A 线默认面处理。
    _gate_page(request, "interpret" if str(_call.get("kind") or "") == "interpret" else "calls")
    # B2 归属：有身份且非 root 时，只能给本账号的通话签房 token（记录缺失保守放行，
    # 沿用 C2 幽灵守卫同口径）。
    if _ident is not None and _ident.role != "root" and _call:
        _call_acct = str(_call.get("account_id") or "")
        if _call_acct and _call_acct != _ident.account_id:
            raise HTTPException(status_code=404, detail="not found")
    # C2 闸3·幽灵重连源(2026-09-13,call-6bd59b40):agent-ui TokenSource 带
    # 自动续签,房间被删后 livekit 全量重连会再来要 token——旧版照签,operator
    # 重连重建房 → 幽灵 job 重放开场白。A 线明确知道已 ended → 拒签(409);
    # B 线 interpret 不拦——0912 定案契约「断线重连客户端仍可取 token,但终态
    # 不翻」(test_token_does_not_revive_terminated_call 钉死);CP 读不到通话
    # 记录(新建流/竞态)保守放行。
    if (
        str(_call.get("status") or "") == "ended"
        and str(_call.get("kind") or "") != "interpret"
    ):
        raise HTTPException(
            status_code=409,
            detail="call has ended — token refused (ghost rejoin guard)",
        )
    kind = str(_call.get("kind") or "")

    # 同传房间:我方端是创建者,token 里挂 RoomConfiguration 显式分发两个方向的
    # interpreter agent(RoomAgentDispatch 只在首个参与者建房时生效,所以只挂 me 端)。
    # metadata 带精确 identity(CP 已知房间名,不再让 agent 拼前缀)与语言对。
    # P1-C：`not _recordless` 收口——无记录房间一律不挂 dispatch（kind 派生自
    # _call,此分支对 recordless 结构性不可达,显式钉死防未来改动破闸）。
    if kind == "interpret" and role == "me" and not _recordless:
        src = (_call.get("language") or "zh").strip() or "zh"
        tgt = (_call.get("target_lang") or "en").strip() or "en"
        # 术语表随 dispatch metadata 下发(P0-2):建单已截 1000 字,这里原样透传
        # (双向同带——fwd 译给对方、rev 译给我方,各自按源语词条取用)。
        glossary = str(_call.get("glossary") or "")
        # 会话级音色(2026-09-17)同管道透传:双向同带 JSON map,fwd/rev 各按自己
        # target_lang 取键(_build_tts_provider 最优先档);空=跟随设置。
        voices_json = str(_call.get("voices_json") or "")
        from livekit.api import RoomAgentDispatch, RoomConfiguration

        at = at.with_room_config(
            RoomConfiguration(
                agents=[
                    RoomAgentDispatch(
                        agent_name="bok-interp-fwd",
                        metadata=json.dumps({
                            "listen_identity": f"me-{room}",
                            "deliver_identity": f"other-{room}",
                            "source_lang": src,
                            "target_lang": tgt,
                            "glossary": glossary,
                            "voices": voices_json,
                        }),
                    ),
                    RoomAgentDispatch(
                        agent_name="bok-interp-rev",
                        metadata=json.dumps({
                            "listen_identity": f"other-{room}",
                            "deliver_identity": f"me-{room}",
                            "source_lang": tgt,
                            "target_lang": src,
                            "glossary": glossary,
                            "voices": voices_json,
                        }),
                    ),
                ]
            )
        )
    elif not is_listen and kind != "interpret" and role in ("operator", "supervisor") and not _recordless:
        # 旁听 token 不加 RoomConfiguration：主管通常后于坐席进房（无副作用），
        # 但若先到，挂 dispatch 会替房间建房并拉起 agent——旁听必须零副作用。
        # P1-C：无记录房间同样零副作用（不建房不拉 agent，只发 subscribe-only
        # token——连不连得上取决于 LiveKit 房间是否真有人建）。
        # A 线显式分发(官方推荐,隐式 dispatch 已废除):worker 以 agent_name="bok-voice"
        # 注册,只有挂了本 dispatch 的房间会拉起客服 agent——顺带杜绝「同传房被
        # A 线 agent 隐式抢派」。metadata 带 call_id,取代已删除的 AGENT_CALL_ID env 旁路。
        from livekit.api import RoomAgentDispatch, RoomConfiguration

        at = at.with_room_config(
            RoomConfiguration(
                agents=[
                    RoomAgentDispatch(
                        agent_name="bok-voice",
                        metadata=json.dumps({"call_id": room}),
                    ),
                ]
            )
        )
    participant_token = at.to_jwt()
    # When the operator connects an existing call, flip it to ACTIVE so the supervisor
    # "active calls" view reflects the real live room.
    # 旁听不翻状态：主管听一通 paused 通话，不得把它悄悄恢复成 active。
    if req.call_id and not is_listen:
        try:
            # 终态守卫（2026-09-11 同传审计 P0）:断线重连的客户端在 hangup 后再取
            # token（call-a9511563 实证 hangup 200 后 +18ms 一发），无条件写 ACTIVE
            # 会把 ENDED/FAILED 复活——最后一写者胜令「挂断不结算」成立。
            # 原子化（2026-09-14 call-c76832ac）:旧版 Python 层「读-判断-写」两步
            # 在并发请求交错时仍有窗口——hangup 提交 ended 后 5ms 的同通话 token
            # 重签读到旧状态、写在提交之后，把终态翻回 active；下游 webhook 崩溃
            # 补位据此往已挂断空房补派幽灵 agent（白跑 64s、预热开场白烧 TTS）。
            # 改仓储层单条条件 UPDATE，终态判定与写入同语句求值，并发签发不可复活。
            # started_at 落点（2026-09-17 仪表盘口径）:真翻成功才补——首次转 ACTIVE
            # 时刻；已有值（dial-result 先行）不覆盖，读改写竞态最坏=挂断后补写
            # started_at 而 duration_s 停留 0（仪表盘不进时长桶，无害）。
            if _repo().mark_active_if_live(req.call_id):
                _cur = _repo().get_call(req.call_id) or {}
                if not _cur.get("started_at"):
                    _repo().update_call(req.call_id, started_at=_utcnow_naive())
        except Exception:
            pass
    _audit("token.issue", subject_type="call", subject_id=req.call_id or "",
           account_id=req.account_id, call_id=req.call_id or "",
           detail={"role": role, "purpose": _purpose})
    if _recordless:
        # P1-C 审计：无记录房间签发降级单独留痕（room+调用方身份），与上面的
        # token.issue 全量行互补——降级路径可独立检索（查滥用/查幽灵房来源）。
        _audit("token.issued", subject_type="room", subject_id=room,
               detail={"recordless": True, "role": role, "purpose": _purpose,
                       "caller": _ident.user_id if _ident else ("machine" if _machine else "anon")})
    return TokenResponse(serverUrl=url, participantToken=participant_token)


@app.post("/api/calls")
def create_call(req: CreateCallRequest, request: Request) -> dict:
    # B4 页面权限：同传建单（kind=interpret）归 interpret 键，其余客服通话归 calls。
    _gate_page(request, "interpret" if (req.kind or "").strip() == "interpret" else "calls")
    # 熔断产品路径（site-delivery M1）：显式绑定承载节点的建单先验节点——
    # 未知 404（既有 404 约定）、revoked 403（root 熔断即刻断供，含坐席 JWT 通道）。
    _req_node = (req.node_id or "").strip()
    if _req_node:
        _node = _node_store().node_by_id(_req_node)
        if _node is None:
            raise HTTPException(status_code=404, detail="node not found")
        if _node.get("revoked"):
            raise HTTPException(status_code=403, detail="node revoked")
        req = req.model_copy(update={"node_id": _req_node})
    identity = current_identity(request)
    created_by = ""
    if identity is not None:
        if identity.role != "root":
            # 话务员/主管建通话强制落本账号（root 可显式指定）。
            req = req.model_copy(update={"account_id": identity.account_id})
        # 建单人盖章（B3）：运行时 QA 检索按「共享+建单人个人」收窄；战役建单无身份=''。
        created_by = identity.user_id
        # 对象归属（2026-09-16 深测 P2）：跨账号对象 404——旧版照常建单，对象卡
        # 话术快照外泄他账号话术 id，agent 装配线（机器通道）更会把他账号客户
        # 档案读进 prompt。root 例外；对象不存在沿用旧行为（无模板快照）。
        if req.object_id:
            obj = _repo().get_object(req.object_id)
            if obj is not None and str(obj.get("account_id") or "") != req.account_id:
                raise HTTPException(status_code=404, detail="not found")
    return _create_call_in(_repo(), req, created_by=created_by)


def _create_call_in(repo, req: CreateCallRequest, created_by: str = "") -> dict:
    """建通话（会话清单装配 + 审计）；repo 由调用方给出（端点= `_repo()`）。

    抽成函数便于 campaign 循环在**注入的 repo** 上建通话（campaign_tick 的 repo
    参数与 app.state 可不同源，单测注入内存仓时不能走 `_repo()`）。
    """
    # 会话清单：读取全局策略(offline_first/cloud_first)与已配置 provider，
    # 并把话术快照到 call（审计「这场用了哪版话术」）。
    # 话术优先级：显式指定（外呼战役/话务员自选）> 对象卡绑定。
    settings = repo.get_settings()
    policy = (settings or {}).get("policy") or "offline_first"
    providers = _effective_providers(settings or {})
    template_id = str(req.template_id or "")
    if not template_id and req.object_id:
        obj = repo.get_object(req.object_id)
        template_id = (obj or {}).get("template_id", "") or ""
    manifest = select_session_manifest(
        session_id=f"call-{uuid.uuid4().hex[:8]}",
        account_id=req.account_id,
        object_id=req.object_id,
        persona_id=req.persona_id,
        mode=req.mode,
        direction=req.direction,
        language=req.language,
        providers=providers,
        policy=policy,
        template_id=template_id,
        tts_reference_voice=req.tts_reference_voice,
        kind=req.kind,
        target_lang=req.target_lang,
        created_by=created_by,
        # 通话绑定节点(site-delivery M1,2026-09-16):显式指定才落,token 签发时校验。
        node_id=(req.node_id or "").strip(),
        # B 线同传术语表(P0-2,2026-09-16):1000 字硬截(防 metadata/prefill 膨胀,
        # agent 侧另有 400 字 prompt 护栏);A 线建单恒空。
        glossary=(req.glossary or "")[:1000],
        # B 线会话级音色(2026-09-17):我方/对方各一把的 JSON map,512 字硬截
        # (两只音色 ID 富余);空=跟随设置页三键/硬编码默认。A 线建单恒空。
        voices_json=(req.voices_json or "")[:512],
    )
    call = repo.create_call(manifest)
    _audit("call.create", subject_type="call", subject_id=call.get("id", ""),
           account_id=req.account_id, call_id=call.get("id", ""),
           detail={"mode": req.mode, "kind": req.kind, "language": req.language, "template_id": template_id,
                   "created_by": created_by})
    return call


def _effective_providers(settings: dict) -> dict:
    """根据全局设置推导会话锁定的 provider 清单（vad/asr/llm/tts）。"""
    return {
        "vad": (settings.get("vad") or {}).get("provider") or "silero",
        "asr": (settings.get("asr") or {}).get("provider") or "qwen3_asr",
        "llm": (settings.get("llm") or {}).get("provider") or "local_openai",
        "tts": (settings.get("tts") or {}).get("provider") or "qwen3_tts",
    }


@app.get("/api/calls")
def list_calls(request: Request, account_id: str = "acc-001", status: str = "") -> list[dict]:
    _gate_page(request, "calls")
    account_id = scoped_account(request, account_id)
    calls = _repo().list_calls(account_id, status)
    stats = _repo().turn_stats()
    for c in calls:
        st = stats.get(c.get("id") or "", {})
        c["turn_count"] = st.get("turns", 0)
        c["avg_latency_ms"] = st.get("avg_latency_ms", 0)
    return calls


@app.get("/api/calls/{call_id}")
def get_call(call_id: str, request: Request) -> dict:
    _gate_page(request, "calls")
    call = deny_cross_account(request, _repo().get_call(call_id))
    if not call:
        raise HTTPException(404, "call not found")
    return call


@app.delete("/api/calls/{call_id}")
def delete_call(call_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    if not _repo().delete_call(call_id):
        raise HTTPException(404, "call not found")
    _audit("call.delete", subject_type="call", subject_id=call_id, detail={})
    return {"deleted": True, "call_id": call_id}


@app.delete("/api/calls")
def clear_ended_calls(request: Request, account_id: str = "acc-001") -> dict:
    """清空该账号下已结束(ended)的通话历史。活跃/进行中的通话不删。"""
    require_role(request, "admin", "root")
    _gate_page(request, "calls")
    account_id = scoped_account(request, account_id)
    calls = _repo().list_calls(account_id, status=CallStatus.ENDED.value)
    removed = 0
    for c in calls:
        cid = str(c.get("id") or "")
        if cid and _repo().delete_call(cid):
            removed += 1
    _audit("call.clear_ended", subject_type="call", detail={"removed": removed, "account_id": account_id})
    return {"deleted": removed}


# ---- 僵尸通话回收器(QA B4,2026-09-09):状态机流转原本挂在「下一个请求」上,
# agent 崩溃/浏览器直接关页会让 ringing/active 永久停摆(supervisor 曾显示 26 路
# 假活跃)。启动扫一遍 + 60s 周期:
#   ①ringing 且 created_at>10min → FAILED(从未接通,无 token 无 turns);
#   ②active/paused 且 LiveKit 房间已无参与者 → ENDED(abandoned)+ 兜底 settle
#     (幂等,existing 短路——agent 实时结算为主,这里只扫尾)。
_STALE_RINGING_S = 600
_REAP_INTERVAL_S = 60


def _lkapi_client():
    """按 app.state/env 凭据构造一次性 LiveKitAPI 客户端；无凭据返回 None。

    dispatch 相关三处调用点（崩溃重派防重 / 挂断断房回收 / reaper 回收）共用
    同一获取方式；客户端自带 aiohttp session，用完必须 `await aclose()`
    （调用方负责）。URL 统一归一为 http(s)，与 `_room_has_participants` 一致。
    """
    key = getattr(app.state, "lk_key", "") or os.environ.get("LIVEKIT_API_KEY", "")
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    if not key or not secret:
        return None
    from livekit import api

    url = getattr(app.state, "lk_url", "") or os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    http_url = url.replace("ws://", "http://").replace("wss://", "https://").rstrip("/")
    return api.LiveKitAPI(url=http_url, api_key=key, api_secret=secret)


async def _cleanup_room_dispatch(room_name: str) -> None:
    """best-effort 回收房间的 explicit dispatch（僵尸通话 P1 收口）；无凭据时 no-op。

    cleanup_dispatch 自吞 LiveKit API 异常；这里再兜一层（含 aclose），
    保证回收失败绝不外溢到挂断/回收主流程。
    """
    lkapi = _lkapi_client()
    if lkapi is None:
        return
    try:
        try:
            await cleanup_dispatch(lkapi, room_name)
        finally:
            await lkapi.aclose()
    except Exception as exc:  # pragma: no cover - 回收失败不阻挂断/回收主流程
        print(f"[cp] dispatch cleanup skipped ({room_name}): {exc!r}", flush=True)


def _is_agent_identity(identity: str) -> bool:
    """agents SDK 真实 job 入房 identity=agent-<jobid>；A 线另兼容旧 bok-voice 直名。"""
    return identity == "bok-voice" or identity.startswith("agent-")


def _has_human_participants(participants) -> bool:
    """房里是否有真人（me-/other-/operator/supervisor 等）。

    只数真人（2026-09-11 同传审计 P1）：被误派进去的 agent 自己就是 participants，
    旧判定「len>0 即有人」令纯 agent 殭尸房永远不满足回收条件、reaper 兜底失效。"""
    return any(not _is_agent_identity(str(getattr(p, "identity", "") or "")) for p in participants or [])


async def _room_has_participants(room_name: str) -> bool:
    """房间存在且有真人 → True;房间不存在/服务不可用/只剩 agent → False(可回收)。"""
    key = getattr(app.state, "lk_key", "") or os.environ.get("LIVEKIT_API_KEY", "")
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    url = getattr(app.state, "lk_url", "") or os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if not key or not secret or not room_name:
        return False
    http_url = url.replace("ws://", "http://").replace("wss://", "https://").rstrip("/")
    try:
        import aiohttp

        from livekit.api import ListParticipantsRequest
        from livekit.api.room_service import RoomService

        async with aiohttp.ClientSession() as session:
            svc = RoomService(session, http_url, key, secret)
            res = await svc.list_participants(ListParticipantsRequest(room=room_name))
            return _has_human_participants(res.participants)
    except Exception:
        # 房间不存在(NotFound)→ 无人 → False;鉴权/网络异常同样按可回收处理
        # (比「永远卡 active」好;回收带 disposition=abandoned 可追溯)。
        return False


def _created_before(call: dict, seconds: float) -> bool:
    raw = call.get("created_at")
    if not raw:
        return False
    try:
        import datetime as _dt

        if isinstance(raw, str):
            ts = _dt.datetime.fromisoformat(raw)
        elif isinstance(raw, _dt.datetime):
            ts = raw
        else:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        import time as _time

        return (_time.time() - ts.timestamp()) >= seconds
    except Exception:
        return False


async def _reap_stale_calls_once() -> dict:
    out = {"failed": 0, "ended": 0, "settled": 0}
    for c in _repo().list_calls("", status=CallStatus.RINGING.value):
        if _created_before(c, _STALE_RINGING_S):
            _repo().update_call(c["id"], status=CallStatus.FAILED.value, disposition="abandoned",
                                **_call_end_fields(c))
            out["failed"] += 1
    for st in (CallStatus.ACTIVE.value, CallStatus.PAUSED.value):
        for c in _repo().list_calls("", status=st):
            if await _room_has_participants(c["id"]):
                continue
            _repo().update_call(c["id"], status=CallStatus.ENDED.value, disposition="abandoned",
                                **_call_end_fields(c))
            out["ended"] += 1
            try:
                await _settle_core(c["id"])  # 幂等:已结算直接 existing 短路
                out["settled"] += 1
            except Exception as exc:  # pragma: no cover - 兜底结算失败不阻回收
                print(f"[cp] reaper settle skipped ({c['id']}): {exc!r}", flush=True)
            # 通话已 ENDED 且房间确认空:主动回收 explicit dispatch(防同 id
            # 重开会房时旧 dispatch 立刻带起 agent,P1 僵尸通话收口)。
            await _cleanup_room_dispatch(c["id"])
    # ③ended 但无结算的历史通话补结算(存量 172 通;幂等,每轮限量防风暴)。
    patched = 0
    for c in _repo().list_calls("", status=CallStatus.ENDED.value):
        if patched >= 10:
            break
        if not _created_before(c, 3600):
            continue
        if _repo().get_settlement(c["id"]):
            continue
        try:
            await _settle_core(c["id"])
            patched += 1
            out["settled"] += 1
        except Exception as exc:  # pragma: no cover
            print(f"[cp] reaper backfill settle skipped ({c['id']}): {exc!r}", flush=True)
    return out


async def _reaper_loop() -> None:
    while True:
        try:
            r = await _reap_stale_calls_once()
            if any(r.values()):
                print(f"[cp] reaper {r}", flush=True)
        except Exception as exc:  # pragma: no cover - 回收失败不阻服务
            print(f"[cp] reaper error {exc!r}", flush=True)
        await asyncio.sleep(_REAP_INTERVAL_S)


_disconnect_room_tasks: set = set()


def _disconnect_room_background(room_name: str) -> None:
    """断房+回收 dispatch 后台化：调用方（hangup/transfer/supervisor_end）都已先置
    终态，断房本质是 best-effort 的收尾——LiveKit 慢/短暂故障时同步 await 每发
    ~3s（2026-09-10 实证 7 连发各 3012-3023ms），期间 leave 无防抖连点放大风暴。
    fire-and-forget 持强引用防事件循环弱引用 GC 中途回收。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - 无事件循环的调用上下文
        return
    t = loop.create_task(_disconnect_livekit_room(room_name))
    _disconnect_room_tasks.add(t)
    t.add_done_callback(_disconnect_room_tasks.discard)


@app.post("/api/calls/{call_id}/hangup")
async def hangup(call_id: str, request: Request) -> dict:
    _gate_page(request, "calls")
    existing = _repo().get_call(call_id)
    deny_cross_account(request, existing)
    if not existing:
        raise HTTPException(404, "call not found")
    call = _repo().update_call(call_id, status=CallStatus.ENDED.value,
                               **_call_end_fields(existing))
    if not call:
        raise HTTPException(404, "call not found")
    # 真正断开 LiveKit 房间：主管台/任意端挂断后 agent 与监听端都会被服务端踢出，
    # agent 侧 on_close 触发结算。房间不存在/服务不可用时不阻塞（DB 已置 ENDED）；
    # 后台执行（2026-09-11 同传审计 P0）：挂断响应不再等 LiveKit。
    _disconnect_room_background(call_id)
    _audit("call.hangup", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id)
    return {"call_id": call_id, "status": call["status"], "disconnected": True}


async def _disconnect_livekit_room(room_name: str) -> None:
    """调用 LiveKit RoomService 删除房间（房间名 = call_id），容错。"""
    if not room_name:
        return
    key = getattr(app.state, "lk_key", "") or os.environ.get("LIVEKIT_API_KEY", "")
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    url = getattr(app.state, "lk_url", "") or os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if key and secret:
        http_url = url.replace("ws://", "http://").replace("wss://", "https://").rstrip("/")
        try:
            import aiohttp

            from livekit.api.room_service import DeleteRoomRequest, RoomService

            async with aiohttp.ClientSession() as session:
                svc = RoomService(session, http_url, key, secret)
                await svc.delete_room(DeleteRoomRequest(room=room_name))
        except Exception as exc:  # pragma: no cover - 房间不存在/livekit 未起都不阻塞挂断
            print(f"[cp] livekit room delete skipped ({room_name}): {exc!r}", flush=True)
    # 通话已 ENDED（三个调用方 hangup/transfer/supervisor_end 都先置 ENDED）:
    # 房间拆除后主动回收 explicit dispatch——房间删除失败/livekit 短暂不可用
    # 也不影响回收尝试(无凭据时 _cleanup_room_dispatch 自行 no-op)。
    await _cleanup_room_dispatch(room_name)


@app.post("/api/calls/{call_id}/turns")
def add_turn(
    call_id: str,
    request: Request,
    role: str,
    transcript: str,
    emotion: str = "",
    provider: str = "",
    latency_ms: int = 0,
    language: str = "",
    # 分析账本列（spec 2026-09-10 §6.1）：可选带缺省，旧调用方（只报
    # role/transcript）零破坏；org/线别/说话人/生成源/话术步/时间轴/perceived_ms。
    org_id: str = "",
    line: str = "a",
    speaker: str = "",
    gen: str = "",
    template_step: int = 0,
    started_ms: int = 0,
    ended_ms: int = 0,
    perceived_ms: int = 0,
) -> dict:
    _gate_page(request, "calls")
    # B2 归属闸（agent 机器上报无身份恒过）；先于 turn_id 说明注释。
    deny_cross_account(request, _repo().get_call(call_id))
    # turn_id 用 uuid 而非 len(get_turns()) 序号：并发写时序号竞态产生重复
    # turn_id → 主键冲突 → IntegrityError 幂等分支吞成 200（静默丢数据，QA
    # 压测 30 并发丢 30-37% 实证）。uuid 根除竞态（#20 同期修 provider/latency
    # 落库但保留了竞态序号，本合并补齐）；agent 上报的 provider/latency_ms
    # 此前被端点签名忽略（turns 两列恒空），一并收参。
    turn = TurnEvent(
        trace_id=call_id,
        call_id=call_id,
        turn_id=uuid.uuid4().hex[:12],
        role=role,
        transcript=transcript,
        emotion=emotion,
        provider=provider,
        latency_ms=latency_ms,
        language=language,
        org_id=org_id,
        line=line,
        speaker=speaker,
        gen=gen,
        template_step=template_step,
        started_ms=started_ms,
        ended_ms=ended_ms,
        perceived_ms=perceived_ms,
    )
    return _repo().create_turn(turn)


@app.post("/api/calls/{call_id}/whatsapp")
def report_whatsapp(call_id: str, req: WhatsAppCaptureRequest, request: Request) -> dict:
    """Agent 偵測到客戶俾 WhatsApp。number 有值 → captured(客戶讀出自己號碼);
    空 → offered(客戶應承加專員,未俾號碼)。升級規則:offered→captured 容許、
    captured 唔覆寫、handled 後唔再降級(避免專員已對接又彈返出嚟)。
    """
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    number = (req.number or "").strip()
    cur_status = str(call.get("whatsapp_status") or "")
    if cur_status == "handled":
        return call
    if cur_status == "captured":
        # 已捉到號碼,新嘅空 offered 唔會降級;同號碼唔重寫。
        if not number:
            return call
        if str(call.get("customer_whatsapp") or "") == number:
            return call
        # 客戶改口/補一個唔同號碼:覆寫並保留 captured。
        fields = {"customer_whatsapp": number, "whatsapp_status": "captured"}
    else:
        fields = {"whatsapp_status": "captured" if number else "offered"}
        if number:
            fields["customer_whatsapp"] = number
    # 意向规则引擎(W4-T1):WhatsApp 对接触发=人工协助信号——assist_status 为空
    # 顺手置 notified(不改已有值;同一次 update_call,不走第二条写路径)。
    if not str(call.get("assist_status") or ""):
        fields["assist_status"] = "notified"
    updated = _repo().update_call(call_id, **fields) or call
    if number:
        # 名册自动入册（Wave1）：captured 带号码 → upsert；channel 归一——
        # 优先 agent 上报（客户原话渠道词），缺省按对象 contact_channel 推断。
        channel = (req.channel or "").strip().lower()
        if channel not in ("whatsapp", "wechat"):
            obj_channel = ""
            if call.get("object_id"):
                obj = _repo().get_object(call.get("object_id")) or {}
                obj_channel = str(obj.get("contact_channel") or "")
            channel = "wechat" if "微信" in obj_channel or "wechat" in obj_channel.lower() else "whatsapp"
        display_name = ""
        summary = ""
        try:
            obj = _repo().get_object(call.get("object_id") or "") or {}
            display_name = str(obj.get("display_name") or "")
            settlement = _repo().get_settlement(call_id) or {}
            summary = str(settlement.get("summary") or "")[:300]
            if not summary:
                # spec §4.3 兜底：captured 常发生在通话进行中/结算未生成时（settlement
                # 由收线后异步产出），摘要恒空 → 名册条目只剩号码不可用。退「末轮客户
                # 转写」（repo.get_turns 返回 TurnEvent 对象，属性访问，同 /turns 端点）。
                turns = _repo().get_turns(call_id)
                customer = [
                    t for t in turns
                    if str(getattr(t, "speaker", "") or "") == "customer"
                ]
                if customer:
                    summary = str(getattr(customer[-1], "transcript", "") or "")[:300]
        except Exception:
            pass
        _repo().upsert_roster_entry(
            account_id=call.get("account_id", "acc-001"), call_id=call_id,
            object_id=call.get("object_id", ""), channel=channel, number=number,
            display_name=display_name, summary=summary,
        )
    _audit("call.whatsapp_captured", subject_type="call", subject_id=call_id,
           account_id=call.get("account_id", "acc-001"),
           detail={"status": fields["whatsapp_status"], "number": (number or "")[:3] + "***"})
    return updated


@app.post("/api/calls/{call_id}/assist")
def assist_notify(call_id: str, req: AssistRequest, request: Request) -> dict:
    """人工协助通知面(W4-T1,2026-09-19):意向规则命中/WhatsApp 捕获 → 打铃
    (notified);人工接手 → done。幂等:当前 done 不降级 notified(其余覆盖)。
    闸链照 whatsapp capture 端点:机器通道直通(无身份不过 _gate_page)、
    跨账号 deny_cross_account。审计 call.assist_notify。
    """
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    status = (req.status or "").strip().lower()
    if status not in ("notified", "done"):
        raise HTTPException(status_code=400, detail="status must be 'notified' or 'done'")
    if str(call.get("assist_status") or "") == "done" and status != "done":
        return call  # 幂等:人工已接手,notified 不降级
    source = (req.source or "").strip()[:16]
    updated = _repo().update_call(call_id, assist_status=status) or call
    _audit("call.assist_notify", subject_type="call", subject_id=call_id,
           account_id=call.get("account_id", "acc-001"),
           detail={"status": status, "source": source})
    return updated


@app.post("/api/calls/{call_id}/dial-result")
def report_dial_result(call_id: str, req: DialResultRequest, request: Request) -> dict:
    """Agent 外呼拨号结果上报:answered→ACTIVE;三失败态→ENDED+disposition。

    幂等：已终态(ended/failed)的通话直接原样返回，不复活也不改写 disposition
    （重派/重复上报时 4B 侧时序抖动不会把已收线的通话抬回 ACTIVE）。
    status 空/未知同样 no-op（仍记审计，便于排查上游漏配）。
    """
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    if str(call.get("status") or "") in _TERMINAL_CALL_STATUSES:
        return call
    status = str(req.status or "").strip()
    if status == "answered":
        # 接通即首次转 ACTIVE：started_at 落点（仪表盘 duration 口径起点）。
        # coalesce（T5-M1）：重复上报/重派不重置起点（与 resume-agent 路径同款）。
        updated = _repo().update_call(
            call_id, status=CallStatus.ACTIVE.value,
            started_at=call.get("started_at") or _utcnow_naive(),
        ) or call
    elif status in ("no_answer", "rejected", "failed"):
        updated = _repo().update_call(call_id, status=CallStatus.ENDED.value,
                                      disposition=status,
                                      **_call_end_fields(call)) or call
    else:
        updated = call
    # 战役名单联动（Wave3）：通话若由某个 campaign item 拨出（call_id 反查），把
    # 拨号结果同步到 item。只认「进行中」item（dialing/in_call）——pending 阶段的
    # call_id 是预写占位、终态 item 已收割过，都不能被迟到的上报改写。
    if status in ("answered", "no_answer", "rejected", "failed"):
        item = _repo().find_item_by_call(call_id)
        if item and str(item.get("status") or "") in ("dialing", "in_call"):
            if status == "answered":
                _repo().update_item(
                    str(item["id"]),
                    status="in_call",
                    last_error=(req.detail or "")[:250],
                    updated_at=_utcnow_iso(),
                )
            else:
                # 失败三态先过重联回队判定（终审 C-1）：命中（result ∈ policy.on 且
                # attempts < max）回 pending 等 interval——旧版直写终态令收割段（只扫
                # 在途 item）永远等不到它，重拨在生产主路静默 no-op。last_error 与
                # 收割路同源=通话 disposition；不命中维持终态直写（last_error 仍带
                # agent detail）。
                campaign = _repo().get_campaign(str(item.get("campaign_id") or ""))
                redispatch = redispatch_harvest_updates(item, status, campaign or {})
                if redispatch:
                    _repo().update_item(
                        str(item["id"]),
                        **redispatch,
                        last_error=str(updated.get("disposition") or status)[:250],
                    )
                else:
                    _repo().update_item(
                        str(item["id"]),
                        status=status,
                        last_error=(req.detail or "")[:250],
                        updated_at=_utcnow_iso(),
                    )
    _audit("call.dial_result", subject_type="call", subject_id=call_id,
           account_id=str(call.get("account_id", "acc-001")),
           detail={"status": status, "detail": (req.detail or "")[:120]})
    return updated


@app.post("/api/calls/{call_id}/whatsapp/handled")
def mark_whatsapp_handled(call_id: str, req: WhatsAppHandledRequest, request: Request) -> dict:
    """專員喺操作台標記已對接 → status=handled,爆閃停止(AI 通話不受影響)。"""
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    if req.handled:
        updated = _repo().update_call(call_id, whatsapp_status="handled") or call
    else:
        # 撤銷:回到有冇號碼嘅狀態
        status = "captured" if str(call.get("customer_whatsapp") or "").strip() else "offered"
        updated = _repo().update_call(call_id, whatsapp_status=status) or call
    _audit("call.whatsapp_handled", subject_type="call", subject_id=call_id,
           account_id=call.get("account_id", "acc-001"), detail={"handled": req.handled})
    return updated


@app.get("/api/roster")
def list_roster(request: Request, account_id: str = "acc-001", status: str = "", channel: str = "") -> list[dict]:
    """名册认领池列表；status/channel 空=不过滤。"""
    _gate_page(request, "roster")
    account_id = scoped_account(request, account_id)
    return _repo().list_roster(account_id, status=status, channel=channel)


@app.post("/api/roster/{entry_id}/claim")
def roster_claim(entry_id: str, req: RosterClaimRequest, request: Request) -> dict:
    """认领名册条目：status=claimed + claimed_by/claimed_at（naive UTC，与读侧 ISO 对齐）。

    B2：claimed_by 有身份时取用户名（并修正旧版把 claimed_by 误写进审计
    account 列的用法）；跨账号条目一律 404。
    """
    _gate_page(request, "roster")
    entry = deny_cross_account(request, _repo().get_roster_entry(entry_id))
    if not entry:
        raise HTTPException(404, "roster entry not found")
    ident = current_identity(request)
    holder = str(entry.get("claimed_by") or "")
    if holder and ident is not None and ident.role == "user" and holder != ident.username:
        # 认领保护（深测 P3）：话务员只能认领无人/自己持有的条目；403 而非 404
        # ——认领人本就在同账号名册池可见，无存在性泄露。主管/机器通道不受限。
        raise HTTPException(403, "claimed by another operator")
    claimed_by = ident.username if ident else req.claimed_by
    entry = _repo().update_roster_entry(
        entry_id, status="claimed", claimed_by=claimed_by,
        claimed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    if not entry:
        raise HTTPException(404, "roster entry not found")
    _audit("roster.claim", subject_type="roster", subject_id=entry_id,
           account_id=str(entry.get("account_id") or ""), detail={"claimed_by": claimed_by})
    return entry


@app.post("/api/roster/{entry_id}/unclaim")
def roster_unclaim(entry_id: str, request: Request) -> dict:
    """释放认领：status=unclaimed、claimed_by 清空。

    claimed_at 必须传空串（repo 契约 None=不修改、""=清空）——空串在双后端
    都映射回「未认领」的读侧契约（SQL 存 NULL / InMemory 存 ""，读侧都渲染 ""）。
    """
    _gate_page(request, "roster")
    entry = deny_cross_account(request, _repo().get_roster_entry(entry_id))
    if not entry:
        raise HTTPException(404, "roster entry not found")
    ident = current_identity(request)
    holder = str(entry.get("claimed_by") or "")
    if holder and ident is not None and ident.role == "user" and holder != ident.username:
        raise HTTPException(403, "claimed by another operator")
    entry = _repo().update_roster_entry(entry_id, status="unclaimed", claimed_by="", claimed_at="")
    if not entry:
        raise HTTPException(404, "roster entry not found")
    _audit("roster.unclaim", subject_type="roster", subject_id=entry_id,
           account_id=str(entry.get("account_id") or ""))
    return entry


@app.post("/api/roster/{entry_id}/handled")
def roster_handled(entry_id: str, req: RosterHandledRequest, request: Request) -> dict:
    """操作台标「已对接」/撤销，联动来源通话 whatsapp_status（与通话内横幅同源语义）。

    true → 名册 handled + 通话 whatsapp_status=handled（爆闪停止）；
    false → 名册回 unclaimed，通话按有无号码回 captured/offered。
    来源通话缺失/已删除不阻塞名册状态变更。
    """
    _gate_page(request, "roster")
    deny_cross_account(request, _repo().get_roster_entry(entry_id))
    entry = _repo().get_roster_entry(entry_id)
    if not entry:
        raise HTTPException(404, "roster entry not found")
    if req.handled:
        entry = _repo().update_roster_entry(entry_id, status="handled") or entry
        _sync_call_whatsapp_status(entry.get("call_id"), "handled")
    else:
        entry = _repo().update_roster_entry(
            entry_id, status="unclaimed", claimed_by="", claimed_at="") or entry
        call = _repo().get_call(str(entry.get("call_id") or "")) or {}
        back = "captured" if str(call.get("customer_whatsapp") or "").strip() else "offered"
        _sync_call_whatsapp_status(entry.get("call_id"), back)
    _audit("roster.handled", subject_type="roster", subject_id=entry_id,
           account_id=str(entry.get("account_id") or ""), detail={"handled": req.handled})
    return entry


def _sync_call_whatsapp_status(call_id: Any, status: str) -> None:
    """名册动作回写来源通话横幅状态；通话不存在/存储异常都不阻塞名册主流程。"""
    if not call_id:
        return
    try:
        _repo().update_call(str(call_id), whatsapp_status=status)
    except Exception as exc:  # pragma: no cover - 存储抖动不阻断名册状态
        print(f"[cp] roster whatsapp_status sync skipped ({call_id}): {exc!r}", flush=True)


# ---- campaigns（外呼战役：建波次 / 启停 / 进度）----
# 名单项由 repo.create_campaign 按 object_ids 一次建仓（无电话的对象落 skipped）。
# 状态机三档（`_campaign_transition`）：running←draft/paused、paused←running、
# stopped←running/paused/draft；`done` 由 campaign 循环名单跑尽时置，不可手动迁。
_CAMPAIGN_SCENARIOS = ("answer", "no_answer", "reject", "hangup_mid")


class CampaignCreateRequest(BaseModel):
    account_id: str = "acc-001"
    name: str = ""
    object_ids: list[str] = []
    template_id: str = ""
    persona_id: str = ""
    language: str = "zh"
    gap_seconds: int = 5
    scenarios: dict[str, str] = {}
    # mock 演练台词（object_id → [句子]），与 scenarios 同 spirit 的测试钩子：
    # campaign 级存 `scripts_json`，起拨时按 object_id 取出来进 dial 块 `script`。
    # 生产真实通话恒空（真 SIP 对端是真客户）。
    scripts: dict[str, list[str]] = {}
    # mock 客户台词句间隔秒（0=子进程默认 6s）。E2E 要把客户报号句对齐到 AI 的
    # 收号步时调大（AI 每轮处理+播报 8-12s）。
    mock_speak_interval_s: float = 0.0
    # 电话边缘站点（spec 2026-09-13 P1.5）：空串=不挂站点（dial 块走 settings
    # `sip` 兜底，单站点旧行为零变化）；挂站点时 dial 块 trunk 取站点注册值。
    site_id: str = ""
    # 8kHz 窄带档（T5 前置门）：mock 客户话音走电话频带，重验窄带下的 ASR。
    # 与句间隔同一份 scripts_json 保留键（`__narrowband__`），只加键不加列。
    narrowband: bool = False
    # 调度三字段（2026-09-17）：时段窗（≤3 组，非法项由 parse_call_windows 静默
    # 丢弃）、并发（0=不限，缺省 1=旧串行）、重拨策略（空=不重拨）。
    call_windows: list[dict] = []
    # `| None` 是 T1-M1 防呆：显式 `"max_concurrency": null` 收口为缺省 1（串行），
    # 不落 0（不限）——端点在 `is None` 时传 1。
    max_concurrency: int | None = 1
    redispatch: dict = {}


@app.post("/api/campaigns")
def create_campaign(req: CampaignCreateRequest, request: Request) -> dict:
    """建战役（draft）+ 名单项；object_ids 空=400（空波次无意义）。

    scenarios 值白名单过滤（answer/no_answer/reject/hangup_mid）：运营表单里
    残留的非法值静默丢弃，不 4xx——名单本身仍照建，避免一个错字废掉整波。
    scripts 同样清洗成 `dict[str, list[str]]`（Pydantic 已保证形状，这里只剔
    空白句并丢空数组，免得 dial 块带一堆空串）。
    """
    _gate_page(request, "campaigns")
    identity = current_identity(request)
    if identity is not None and identity.role != "root":
        # 话务员/主管建波强制落本账号（root 可显式指定）。
        req = req.model_copy(update={"account_id": identity.account_id})
    # 名单归属（2026-09-16 深测 P2）：任一对象属他账号 → 整单 404（fail-closed）。
    # 旧版 items[].phone 直接回显他账号客户手机号，且战役循环会真实外呼该号码。
    if identity is not None and identity.role != "root":
        for oid in req.object_ids:
            obj = _repo().get_object(oid)
            if obj is not None and str(obj.get("account_id") or "") != req.account_id:
                raise HTTPException(status_code=404, detail="not found")
    if not req.object_ids:
        raise HTTPException(400, "object_ids 不能为空")
    scripts = {
        str(k): [str(s) for s in v if str(s).strip()]
        for k, v in req.scripts.items() if isinstance(v, list)
    }
    scripts = {k: v for k, v in scripts.items() if v}
    # 句间隔与台词同源存进 scripts_json（保留键 `__speak_interval__`）：只加列
    # 不加宽、不加新表，起拨时 campaign._start_call 从同一份 JSON 取。
    if float(req.mock_speak_interval_s or 0) > 0:
        scripts["__speak_interval__"] = float(req.mock_speak_interval_s)
    # 窄带档（T5）：假值不发键——旧战役的 scripts_json 逐字节零变化。
    if req.narrowband:
        scripts["__narrowband__"] = True
    camp = _repo().create_campaign(
        req.account_id, name=req.name, template_id=req.template_id,
        persona_id=req.persona_id, language=req.language,
        gap_seconds=req.gap_seconds, object_ids=req.object_ids,
        scenarios={k: v for k, v in req.scenarios.items() if v in _CAMPAIGN_SCENARIOS},
        scripts=scripts,
        site_id=req.site_id,
        call_windows=parse_call_windows(req.call_windows),
        # 显式 null / 缺省 → 1（旧串行）；绝不向 repo 传 None（T1-M1 防呆）。
        max_concurrency=1 if req.max_concurrency is None else max(0, int(req.max_concurrency)),
        redispatch=_clean_redispatch(req.redispatch),
    )
    _audit("campaign.create", subject_type="campaign", subject_id=camp["id"],
           account_id=req.account_id, detail={"objects": len(req.object_ids)})
    return camp


@app.get("/api/campaigns")
def list_campaigns(request: Request, account_id: str = "acc-001") -> list[dict]:
    """战役列表，每条带 progress 汇总（列表页免二次请求）。"""
    _gate_page(request, "campaigns")
    account_id = scoped_account(request, account_id)
    out = []
    for camp in _repo().list_campaigns(account_id):
        camp["progress"] = _progress(_repo().list_items(camp["id"]))
        out.append(camp)
    return out


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, request: Request) -> dict:
    """战役详情：campaign + items + progress。"""
    _gate_page(request, "campaigns")
    camp = deny_cross_account(request, _repo().get_campaign(campaign_id))
    if not camp:
        raise HTTPException(404, "campaign not found")
    items = _repo().list_items(campaign_id)
    camp["items"] = items
    camp["progress"] = _progress(items)
    return camp


@app.post("/api/campaigns/{campaign_id}/start")
def campaign_start(campaign_id: str, request: Request) -> dict:
    """启波（draft/paused → running；循环巡检即刻接手首通）。"""
    return _campaign_transition(request, campaign_id, "running")


@app.post("/api/campaigns/{campaign_id}/pause")
def campaign_pause(campaign_id: str, request: Request) -> dict:
    """暂停（running → paused）：循环不再起新通，进行中的一路不打断。"""
    return _campaign_transition(request, campaign_id, "paused")


@app.post("/api/campaigns/{campaign_id}/stop")
def campaign_stop(campaign_id: str, request: Request) -> dict:
    """终止（running/paused/draft → stopped，终态不可再启）。"""
    return _campaign_transition(request, campaign_id, "stopped")


def _campaign_transition(request: Request, campaign_id: str, status: str) -> dict:
    """战役状态机唯一入口（三个启停端点共用），非法迁移 409、未找到 404。"""
    _gate_page(request, "campaigns")
    camp = deny_cross_account(request, _repo().get_campaign(campaign_id))
    if not camp:
        raise HTTPException(404, "campaign not found")
    cur = str(camp.get("status") or "")
    allowed = {"running": ("draft", "paused"), "paused": ("running",),
               "stopped": ("running", "paused", "draft")}
    if cur not in allowed[status]:
        raise HTTPException(409, f"cannot {status} from {cur}")
    updated = _repo().update_campaign(campaign_id, status=status) or camp
    _audit(f"campaign.{status}", subject_type="campaign", subject_id=campaign_id,
           account_id=str(camp.get("account_id", "acc-001")))
    return updated


@app.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: str, request: Request) -> dict:
    """删战役（含名单项）。running 拒删（409）——先停止再删，防误删在跑波次。"""
    _gate_page(request, "campaigns")
    camp = deny_cross_account(request, _repo().get_campaign(campaign_id))
    if not camp:
        raise HTTPException(404, "campaign not found")
    status = str(camp.get("status") or "")
    if status == "running":
        raise HTTPException(409, "campaign is running — stop it first")
    items = _repo().list_items(campaign_id)
    _repo().delete_campaign(campaign_id)
    _audit("campaign.delete", subject_type="campaign", subject_id=campaign_id,
           account_id=str(camp.get("account_id") or ""),
           detail={"items": len(items), "status": status})
    return {"campaign_id": campaign_id, "deleted": True, "items_removed": len(items)}


_REDISPATCH_OUTCOMES = ("no_answer", "rejected", "failed")


def _clean_redispatch(raw: dict | None) -> dict:
    """重拨策略清洗：max_attempts≥0、interval_minutes>0 才有意义、on 白名单剔除。"""
    if not isinstance(raw, dict):
        return {}
    try:
        max_attempts = max(0, int(raw.get("max_attempts") or 0))
    except (TypeError, ValueError):
        return {}
    try:
        interval = float(raw.get("interval_minutes") or 0)
    except (TypeError, ValueError):
        return {}
    if max_attempts <= 0 or interval <= 0:
        return {}
    on = [str(x) for x in (raw.get("on") or []) if str(x) in _REDISPATCH_OUTCOMES]
    return {"max_attempts": max_attempts, "interval_minutes": interval, "on": on}


class CampaignUpdateRequest(BaseModel):
    name: str | None = None
    template_id: str | None = None
    persona_id: str | None = None
    language: str | None = None
    gap_seconds: int | None = None
    site_id: str | None = None
    call_windows: list[dict] | None = None
    max_concurrency: int | None = None
    redispatch: dict | None = None


@app.put("/api/campaigns/{campaign_id}")
def update_campaign(campaign_id: str, req: CampaignUpdateRequest, request: Request) -> dict:
    """改战役配置：running 拒改（409，运行中时段/并发锁定——对齐惜客通语义）；
    draft/paused/stopped 可改。字段只增不改默认语义（None=不碰该字段）。"""
    _gate_page(request, "campaigns")
    camp = deny_cross_account(request, _repo().get_campaign(campaign_id))
    if not camp:
        raise HTTPException(404, "campaign not found")
    if str(camp.get("status") or "") == "running":
        raise HTTPException(409, "campaign is running — pause it first")
    fields: dict = {k: v for k, v in {
        "name": req.name, "template_id": req.template_id, "persona_id": req.persona_id,
        "language": req.language, "gap_seconds": req.gap_seconds, "site_id": req.site_id,
    }.items() if v is not None}
    if req.call_windows is not None:
        fields["call_windows_json"] = json.dumps(
            parse_call_windows(req.call_windows), ensure_ascii=False)
    if req.max_concurrency is not None:
        fields["max_concurrency"] = max(0, int(req.max_concurrency))
    if req.redispatch is not None:
        cleaned = _clean_redispatch(req.redispatch)
        fields["redispatch_json"] = json.dumps(cleaned, ensure_ascii=False) if cleaned else ""
    updated = _repo().update_campaign(campaign_id, **fields) or camp
    _audit("campaign.update", subject_type="campaign", subject_id=campaign_id,
           account_id=str(camp.get("account_id") or ""),
           detail={"fields": sorted(fields)})
    return updated


def _call_end_fields(call: dict) -> dict:
    """通话终态时间戳落点（2026-09-17 仪表盘口径）：ended_at=now、duration_s=ended-started。

    started_at 为空（未接通/从未 ACTIVE）→ duration_s=0。全部 UTC naive，与 created_at 同域。
    """
    from .campaign import _parse_updated_at, _utcnow_naive
    ended = _utcnow_naive()
    started = _parse_updated_at(call.get("started_at"))
    duration = int((ended - started).total_seconds()) if started and ended >= started else 0
    return {"ended_at": ended, "duration_s": duration}


def _progress(items: list[dict]) -> dict:
    """名单进度汇总：8 个状态计数 + answered 粗口径（拨出去有结果的三态之和）。"""
    p = {"total": len(items)}
    for key in ("pending", "dialing", "in_call", "done", "no_answer", "rejected",
                "failed", "skipped"):
        p[key] = sum(1 for i in items if i.get("status") == key)
    p["answered"] = p["done"] + p["no_answer"] + p["rejected"]
    return p


@app.post("/api/sip/sites")
def create_sip_site(req: SiteCreateRequest, request: Request) -> dict:
    """建站（spec 2026-09-13 P1.5 T7 收尾：面板「+ 新建站点」入口）。

    P1.5 起站点表只有 repo 层入口时，面板站点下拉在空库只能提示「请先在后端登记
    站点」；本端点把建行收进 HTTP 面（name 必填 + livekit_url 两字段即可建最小
    站点，后续 trunk 注册/战役挂 site_id 都按这个 id 走）。

    **幂等**（T7 定案，非 409）：同 `account_id` + `name` 已存在时直接返回既有行
    ——建站是引导期动作，面板双击/脚本重跑不该堆出同名重复行；同账号要用两个
    同名站点无实际意义（站点选择按 id，名字只给人看）。不覆盖既有行字段（改字段
    走 update_site；重名幂等不承担 upsert 语义，避免误清已注册 trunk_id）。
    失败面：name 空=400；sip_edge 越界（值域 none|local|cloud）=400；
    numbers 非 list[str]=422（Pydantic 严格类型，T1 审查同款防线）。
    """
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "name 必填")
    sip_edge = (req.sip_edge or "local").strip() or "local"
    if sip_edge not in ("none", "local", "cloud"):
        raise HTTPException(400, f"sip_edge 只支持 none|local|cloud（收到：{sip_edge}）")
    # 建站=基础设施引导（B2 管理面语义）：user 不可建；admin 非 root 强制本账号。
    require_role(request, "admin", "root")
    account_id = (req.account_id or "acc-001").strip() or "acc-001"
    identity = current_identity(request)
    if identity is not None and identity.role != "root":
        account_id = identity.account_id or account_id
    repo = _repo()
    for row in repo.list_sites(account_id):
        if str(row.get("name") or "") == name:
            return row  # 幂等命中：返回既有行，不重复建、不改写
    site = repo.create_site(
        account_id, name=name, livekit_url=(req.livekit_url or "").strip(),
        sip_edge=sip_edge, trunk_id=(req.trunk_id or "").strip(),
        numbers=[str(n).strip() for n in req.numbers if str(n).strip()],
        region=(req.region or "").strip(),
    )
    _audit("sip.site_created", subject_type="site", subject_id=str(site.get("id") or ""),
           account_id=account_id,
           detail={"name": name, "livekit_url": site.get("livekit_url", ""),
                   "sip_edge": sip_edge})
    return site


@app.get("/api/sip/sites")
def list_sip_sites(request: Request, account_id: str = "acc-001") -> list[dict]:
    """站点列表（P1.5 T1 repo 直通）：外呼页/设置页「注册 trunk 到站点」的下拉数据源。

    只返回库中真站点——虚拟兜底站点 `site-local`（`get_default_site` 恒合成、
    不入库）不在列表里：它没有行可回填 trunk_id，注册动作对它无意义。
    """
    _gate_page(request, "campaigns")
    account_id = scoped_account(request, account_id)
    return _repo().list_sites(account_id)


@app.post("/api/sip/sites/{site_id}/trunk")
async def register_sip_trunk(site_id: str, req: TrunkRegisterRequest, request: Request) -> dict:
    """把 SIP 供应商凭据注册成 LiveKit outbound trunk，并把返回 id 回填站点。

    站点生命周期的一次性引导动作（spec 2026-09-13 P1.5 T3）：调 LiveKit SIP 服务
    `CreateSIPOutboundTrunk` 建 trunk → `site.trunk_id` = 返回的 `sip_trunk_id`
    ——T2 的 campaign 按 site 优先取 trunk（无站点时才回退 settings），agent 侧零改。
    `address`/`numbers` 必填（400）；`auth_*` 可空 = IP 白名单模式；**密码不回显**。
    失败（凭据缺 / SIP 服务未部署 / 不可达 / 返回空 id）一律 502 且**绝不写**
    site.trunk_id——宁可报错也不静默清空站点已注册的 trunk。
    """
    # trunk 注册携带 SIP 凭据=基础设施动作（B2 管理面）；跨账号站点一律 404。
    require_role(request, "admin", "root")
    site = deny_cross_account(request, _repo().get_site(site_id))
    if not site:
        raise HTTPException(404, "site not found")
    address = (req.address or "").strip()
    numbers = [str(n).strip() for n in req.numbers if str(n).strip()]
    if not address or not numbers:
        raise HTTPException(400, "address 与 numbers 必填")
    client = _lkapi_client()
    if client is None:
        raise HTTPException(502, "LiveKit 凭据未配置——livekit-sip 未部署或不可达")
    # SDK 1.2+ 的写法：CreateSIPOutboundTrunkRequest(trunk=SIPOutboundTrunkInfo)；
    # `create_sip_outbound_trunk` 是同一调用的弃用名（1.2 起 warnings.warn）。
    from livekit.api import CreateSIPOutboundTrunkRequest, SIPOutboundTrunkInfo

    create = CreateSIPOutboundTrunkRequest(
        trunk=SIPOutboundTrunkInfo(
            name=f"outbound-{site_id[:8]}-{int(datetime.now(timezone.utc).timestamp())}",
            address=address,
            numbers=numbers,
            auth_username=req.auth_username,
            auth_password=req.auth_password,
        )
    )
    try:
        info = await client.sip.create_outbound_trunk(create)
    except Exception as exc:
        raise HTTPException(
            502, f"trunk 注册失败——livekit-sip 未部署或不可达: {exc}"
        ) from exc
    finally:
        await client.aclose()  # 一次性客户端（自带 aiohttp session）必须关
    trunk_id = str(getattr(info, "sip_trunk_id", "") or "")
    if not trunk_id:
        raise HTTPException(502, "trunk 注册失败——livekit-sip 未返回 trunk id")
    updated = _repo().update_site(site_id, trunk_id=trunk_id) or site
    _audit("sip.trunk_registered", subject_type="site", subject_id=site_id,
           account_id=str(site.get("account_id") or ""),
           detail={"trunk_id": trunk_id, "address": address})
    return {"trunk_id": trunk_id, "site": updated}


class MockCalleeRequest(BaseModel):
    room: str
    number: str
    identity: str = ""
    scenario: str = "answer"  # answer | no_answer | reject | hangup_mid
    language: str = "cantonese"
    script: list[str] = []
    ring_delay_s: float = 3.0
    ringing_window_s: float = 35.0
    # 句间隔秒（默认 6≈一轮问答）：E2E/演练要把客户台词对齐到 AI 的话术步进时
    # 调大（AI 每轮处理+播报可能 8-12s，太密会令报号句落在收号步之外）。
    speak_interval_s: float = 6.0
    # 8kHz 窄带档（spec 2026-09-13 §6 前置门）：客户话音按电话频带（3.4kHz
    # 抗混叠 → 8k → 升回 16k）再推流，模拟运营商 PCMU/PCMA 窄带线路。
    narrowband: bool = False


@app.post("/api/sip/mock/callee")
def spawn_mock_callee(req: MockCalleeRequest, request: Request) -> dict:
    """模拟联调档:派生 mock 客户子进程(同 pregen detached 姿势,失败不阻拨号主链)。

    联调/演练工具面：user 不开放（防话务员自己派 mock 客户刷通话），
    agent 机器通道直通（campaign 派发链在用）。

    真语音被叫——子进程进房后按剧本(四型)TTS 轮播客户话音,agent 侧
    dialer._dial_mock 靠 participant identity 认它。无 LiveKit 凭据=404
    (与 /api/token 同语义:缺凭据不静默回退)。
    """
    import subprocess

    require_role(request, "admin", "root")
    if not req.room or not req.number:
        raise HTTPException(400, "room and number are required")
    identity = req.identity or f"sip-mock-{req.number}"
    key = getattr(app.state, "lk_key", "") or os.environ.get("LIVEKIT_API_KEY", "")
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    if not key or not secret:
        raise HTTPException(404, "livekit credentials not configured")
    lk_url = (
        getattr(app.state, "lk_url", "") or os.environ.get("LIVEKIT_URL", "")
        or "ws://127.0.0.1:7880"
    )
    from livekit import api as lk_api

    import datetime as _dt

    at = (
        lk_api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name("Mock Callee")
        .with_grants(lk_api.VideoGrants(
            room_join=True, room=req.room,
            can_publish=True, can_subscribe=True, can_publish_data=True,
        ))
        .with_ttl(_dt.timedelta(seconds=3600))
        .with_attributes({"bok.role": "customer", "bok.mock": "1"})
    )
    token = at.to_jwt()

    repo_root = Path(__file__).resolve().parents[3]
    script_path = repo_root / "scripts" / "mock_callee.py"
    if not script_path.exists():
        raise HTTPException(404, "scripts/mock_callee.py not found (packaged runtime)")
    cmd = [
        sys.executable, str(script_path),
        "--url", lk_url, "--token", token, "--identity", identity,
        "--scenario", req.scenario, "--language", req.language,
        "--script-json", json.dumps(req.script, ensure_ascii=False),
        "--ring-delay", str(req.ring_delay_s),
        "--ringing-window", str(req.ringing_window_s),
        "--speak-interval", str(req.speak_interval_s),
    ]
    if req.narrowband:
        cmd.append("--narrowband")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    log_path = repo_root / "runtime" / "logs" / "mock-callee.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(  # noqa: S603 - 固定脚本+参数,无 shell
                cmd, cwd=str(repo_root), env=env, stdout=logf,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
    except OSError as exc:
        raise HTTPException(500, f"failed to spawn mock callee: {exc}") from exc
    # 确定性收尸(pregen._spawn_detached 同款):不排 daemon reaper 的话,子进程
    # 退出后留僵尸直到进程表被别处顺手 wait —— mock 被叫每次拨号一发,长跑会累积。
    import threading

    threading.Thread(target=proc.wait, daemon=True, name=f"mock-callee-reap-{proc.pid}").start()
    # P3-B（2026-09-17 全量 debug）：审计账号按 room 对应通话的真实归属盖章——
    # 旧硬编码 "acc-001" 会把任意账号的 mock 派发记错账；记录查不到（演练房无
    # 建单）落空串，绝不回退硬编码 id。
    try:
        _mock_acct = str((_repo().get_call(req.room) or {}).get("account_id") or "")
    except Exception:  # pragma: no cover - 读不到归属宁可空串不阻主链
        _mock_acct = ""
    _audit("sip.mock_callee_spawn", subject_type="room", subject_id=req.room,
           account_id=_mock_acct,
           detail={"scenario": req.scenario, "pid": proc.pid, "identity": identity,
                   "narrowband": req.narrowband})
    return {"ok": True, "pid": proc.pid, "identity": identity}


class NodeRegisterRequest(BaseModel):
    name: str = ""
    platform: str = ""
    org_id: str = ""
    version: str = ""
    # P1 节点鉴权（加固模式必填）：license_key=root 签发的节点许可证；
    # fingerprint=机器指纹（node-agent 采集的 sha256，非原始序列号）。
    license_key: str = ""
    fingerprint: str = ""


class NodeHeartbeatRequest(BaseModel):
    metrics: dict = {}
    fingerprint: str = ""
    # P3 commands 通道（2026-09-17）：version=节点当前包版本（写回 nodes.version
    # + update 指令收敛关单）；acks=已领指令的执行回执 [{id, ok, result}]。
    version: str = ""
    acks: list = []


class NodeCommandRequest(BaseModel):
    """root 下发节点指令入参：action 白名单（update/restart/shutdown）。

    update 必带 version（目标包版本，工件须已上传 CP downloads）；force=True
    跳过「该节点有在途通话」的拒绝检查（默认拒绝——强更会掐活通话）。"""

    action: str
    version: str = ""
    force: bool = False


class NodeLicenseCreateRequest(BaseModel):
    """root 签发节点许可证入参：max_nodes 配额、归属 org/account、备注。"""

    org_id: str = ""
    account_id: str = ""
    max_nodes: int = 1
    note: str = ""


@app.post("/api/nodes/register")
def register_node(req: NodeRegisterRequest) -> dict:
    """节点注册（spec §4.2）：签发 node_token，明文只在本次响应出现一次。

    P1 节点鉴权：**加固模式（BOK_AUTH_REQUIRED=1 或 BOK_CP_TOKEN 已设）下必须携带
    有效 license_key**（root 签发、配额内、未吊销），并绑定机器指纹——同
    (license, fingerprint) 重注册幂等复用 node_id 换新 token；配额满且指纹不同
    =403（克隆/挪机检出）；本地双关全空=开放注册（单机形态零变化）。此闸叠加在
    CP token/JWT 门禁之上（中间件先挡无凭据请求，这里挡「有凭据但无许可」）。
    """
    store = _node_store()
    license_id = ""
    if node_license_required():
        license_key = (req.license_key or "").strip()
        # 先 license 后指纹（401 优先于 400）：裸注册（无任何凭据）保持旧 401
        # 「unknown or missing license key」语义（tests/test_nodes_registry.py
        # 契约：register 裸注册被 license 闸拒）；带了 license 才要求指纹。
        if not license_key:
            raise HTTPException(401, "unknown or missing license key")
        # 指纹必填（2026-09-16 深测 P1）：空指纹曾以 " " 兜底查询=永不命中复用，
        # 任何机器心跳全过、克隆检测结构性永不触发——1 配额=无限台机器。
        fingerprint = (req.fingerprint or "").strip()
        if not fingerprint:
            raise HTTPException(400, "fingerprint is required in hardened mode")
        try:
            lic, node_id, token = store.register_licensed(
                license_key=license_key, fingerprint=fingerprint,
                name=req.name, platform=req.platform, org_id=req.org_id,
                version=req.version)
        except LicenseError as exc:
            # sticky 熔断取证（site-delivery fixwave）：root 吊销行重注册被拒不复活
            # 落审计（按尝试计）；其余 license 拒绝维持原语义不刷审计。
            if exc.reason == "node revoked":
                _audit("node.register_denied_revoked", subject_type="node",
                       subject_id="", outcome="denied", account_id="",
                       detail={"fingerprint_prefix": fingerprint[:12]})
            raise HTTPException(exc.status_code, exc.reason) from exc
        _audit("node.registered", subject_type="node", subject_id=node_id,
               account_id="", detail={
                   "name": req.name, "platform": req.platform, "version": req.version,
                   "license_id": lic["license_id"],
                   "fingerprint_prefix": fingerprint[:12]})
        return {"node_id": node_id, "node_token": token,
                "heartbeat_interval_s": HEARTBEAT_INTERVAL_S}
    elif (req.license_key or "").strip():
        # 非加固模式也尊重显式 license（登记归属，不强制）。
        lic = store.find_license((req.license_key or "").strip())
        license_id = lic["license_id"] if lic else ""
    try:
        node_id, token = store.register(
            name=req.name, platform=req.platform, org_id=req.org_id, version=req.version,
            license_id=license_id, fingerprint=(req.fingerprint or "").strip(),
        )
    except LicenseError as exc:
        # 开放流复用候选是 root 吊销行（sticky）：同一拒绝语义，不分认证模式。
        # 熔断取证（site-delivery fixwave）：sticky 拒绝落审计，按尝试计。
        if exc.reason == "node revoked":
            _audit("node.register_denied_revoked", subject_type="node",
                   subject_id="", outcome="denied", account_id="",
                   detail={"fingerprint_prefix": (req.fingerprint or "")[:12]})
        raise HTTPException(exc.status_code, exc.reason) from exc
    _audit("node.registered", subject_type="node", subject_id=node_id,
           account_id="", detail={
               "name": req.name, "platform": req.platform, "version": req.version,
               "license_id": license_id,
               "fingerprint_prefix": (req.fingerprint or "")[:12]})
    return {"node_id": node_id, "node_token": token, "heartbeat_interval_s": HEARTBEAT_INTERVAL_S}


@app.post("/api/nodes/heartbeat")
def node_heartbeat(req: NodeHeartbeatRequest, authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    ok, reason = (False, "unknown_token")
    node = None
    if token:
        node = _node_store().resolve_node_token(token)
        ok, reason = _node_store().heartbeat(
            token, req.metrics, fingerprint=(req.fingerprint or "").strip(),
            require_license=node_license_required(),
            version=(req.version or "").strip())
    if not ok:
        # 克隆/吊销是安全事件（节点已被 store 自动吊销），一次性落审计；普通
        # 凭据错误只 401 不刷审计（防心跳重试刷屏）。
        if reason in ("fingerprint_mismatch", "license_revoked"):
            _audit(f"node.denied.{reason}", subject_type="node", subject_id="",
                   account_id="", detail={"reason": reason})
        detail = {"fingerprint_mismatch": "fingerprint mismatch (clone/relocated?)",
                  "license_revoked": "license revoked", "revoked": "node revoked",
                  "license_required": "node not licensed (hardened mode)"}.get(reason)
        if reason == "revoked" and token:
            # 停栈指令（site-delivery M1）：root 吊销（sticky）的心跳 401 detail
            # 必须携带机器可执行 action:"shutdown"——node_agent 据此不 self-heal、
            # 停栈退出。auto_clone 吊销保持纯文本（原机重注册复活路径保留，
            # 不该逼停整台机器）。resolve 失败（token 已被换发覆盖等）按纯文本兜底。
            node = _node_store().resolve_node_token(token)
            if node is not None and (node.get("revoked_source") or "") == "root":
                detail = {"reason": "node revoked", "action": "shutdown"}
        raise HTTPException(401, detail or "unknown node token")
    # 指令分发（P3）：先落回执（失败/异常类），再派发 pending→delivered。
    # 成功的 update 不等 ack——version 收敛时 store 自动关单。
    for ack in (req.acks or []):
        if not isinstance(ack, dict) or not str(ack.get("id", "")):
            continue
        _node_store().ack_command(str(node["node_id"]), str(ack["id"]),
                                  bool(ack.get("ok")), str(ack.get("result", ""))[:200])
    commands = _node_store().pop_commands(str(node["node_id"])) if node else []
    if commands:
        print(f"[node] dispatched {len(commands)} command(s) -> {node['node_id']}: "
              f"{[c['action'] for c in commands]}", flush=True)
    return {"ok": True, "commands": commands}


def _node_active_calls(node_id: str) -> list[dict]:
    """该节点上「在途」的通话（update/restart 前拒绝检查用）。

    终态=completed/failed/cancelled/handled/idle——其余（active/paused/
    escalated_to_human 等）都算在途：宁可多拒，root 有 force。逐账号扫太散，
    直接跨账号按 node 过滤（list_calls 账号传空=全账号）。"""
    terminal = {"", "idle", "completed", "failed", "cancelled", "canceled", "handled"}
    try:
        calls = _repo().list_calls("", node_id=node_id)
    except TypeError:
        # 旧 repo 实现不认 node_id 参数（第三方/测试桩）——守卫降级为不拦。
        return []
    return [c for c in calls if str(c.get("status", "")) not in terminal]


@app.post("/api/nodes/{node_id}/commands")
def create_node_command(node_id: str, req: NodeCommandRequest, request: Request) -> dict:
    """root 下发节点指令（P3 commands 通道入口）。

    白名单外 action → 400；update 必带 version（工件须在 downloads）；默认
    该节点有在途通话时拒绝（force=True 越过——强更掐通话是显式决定）。执行
    结果不在此同步返回：节点心跳领走后按 version 收敛（update）/ 节点 status
    变化（shutdown/restart）间接可见，台账 GET /api/nodes/{id}/commands。"""
    ident = require_role(request, "root")
    created_by = ident.user_id if ident is not None else "machine"
    store = _node_store()
    if store.node_by_id(node_id) is None:
        raise HTTPException(404, "node not found")
    args: dict = {}
    if req.action == "update":
        version = (req.version or "").strip()
        if not version:
            raise HTTPException(400, "update command requires version")
        args["version"] = version
        args["force"] = bool(req.force)
    elif req.action in ("restart", "shutdown"):
        args["force"] = bool(req.force)
    else:
        raise HTTPException(400, f"unsupported action: {req.action}")
    if req.action in ("update", "restart") and not req.force:
        inflight = _node_active_calls(node_id)
        if inflight:
            raise HTTPException(409, f"node has {len(inflight)} in-flight call(s); "
                                     "retry with force=true to override")
    try:
        cmd = store.enqueue_command(node_id, req.action, args=args,
                                    created_by=created_by)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _audit("node.command_queued", subject_type="node", subject_id=node_id,
           detail={"action": req.action, "args": args, "command_id": cmd["id"]})
    return cmd


@app.get("/api/nodes/{node_id}/commands")
def list_node_commands(node_id: str, request: Request) -> dict:
    """指令台账（root 面排障/舰队版本面板数据源）。"""
    require_role(request, "root")
    if _node_store().node_by_id(node_id) is None:
        raise HTTPException(404, "node not found")
    return {"node_id": node_id, "commands": _node_store().list_commands(node_id)}


@app.get("/api/nodes")
def list_nodes(request: Request) -> list[dict]:
    # 节点注册表=平台面（root）。
    require_role(request, "root")
    rows = _node_store().list_nodes()
    # P3 舰队面板：pending 指令数就地 enrich（默认 0，不查库时省一跳）。
    for r in rows:
        try:
            r["pending_commands"] = _node_store().pending_command_count(r["node_id"])
        except Exception:  # pragma: no cover - 面板增强不阻列表
            r["pending_commands"] = 0
    return rows


@app.get("/api/nodes/downloads/{kind}/{version}/{filename}")
def download_node_artifact(kind: str, version: str, filename: str,
                           authorization: str = Header(default="")) -> FileResponse:
    """节点工件下载（P3/去 GitHub 化交付链）：pkg=代码包，runtime=预构建运行时，
    bootstrap=装机引导脚本（install-node.sh/.ps1、bootstrap-node.sh——客户链路
    唯一入口，永久挂 pkg/latest 别名）。

    鉴权=端点内自证（同 register/heartbeat 模式，中间件前缀豁免）：Bearer
    node_token（拒 revoked）或 license key（须 active）——客户链路只见本 CP
    域名，仓库上游不可见。路径三段白名单校验（字母数字._- 且不点开头），
    文件必须真实落在工件目录内——穿越/编码绕行一律 404。"""
    token = authorization.removeprefix("Bearer ").strip()
    store = _node_store()
    if token:
        node = store.resolve_node_token(token)
        if node is not None and node.get("revoked"):
            raise HTTPException(403, "node revoked")
        if node is None:
            lic = store.find_license(token)
            if lic is None or lic.get("status") != "active":
                raise HTTPException(401, "node token or license key required")
    else:
        raise HTTPException(401, "node token or license key required")
    if kind not in ("pkg", "runtime", "bootstrap"):
        raise HTTPException(404, "not found")
    import re

    for seg in (version, filename):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", seg):
            raise HTTPException(404, "not found")
    artifacts = Path(os.environ.get("BOK_NODE_ARTIFACTS_DIR", "/app/downloads"))
    target = (artifacts / kind / version / filename).resolve()
    if not target.is_file() or artifacts.resolve() not in target.parents:
        raise HTTPException(404, "not found")
    _audit("node.artifact_downloaded", subject_type="artifact",
           subject_id=f"{kind}/{version}/{filename}")
    return FileResponse(target, filename=filename)


# ---- 节点日志上报/查看（W2 远程日志通道，2026-09-18）----
# 零入站模型：CP 不拉，节点领 upload_logs 指令后主动 POST gzip 束到本端点。
# 存储=工件卷 logs/ 命名空间（downloads 端点只放行 pkg/runtime/bootstrap 三段，
# 互不可见）；TTL 到期顺手清。root 查看/下载走 JWT 门禁（不入豁免表）。

_LOG_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
_LOG_TTL_DAYS_DEFAULT = 7


def _node_logs_dir(node_id: str) -> Path:
    base = (Path(os.environ.get("BOK_NODE_ARTIFACTS_DIR", "/app/downloads"))
            / "logs" / node_id)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 缺省 /app/downloads 是容器内路径；裸机/CI 上未配 env 时 mkdir 即失败
        # ——503 明文（节点 ack ok=false 带原因）而非裸 500（CI ⑨ 步首跑实证）。
        raise HTTPException(503, f"node log storage unavailable: {exc}") from exc
    return base


def _sweep_node_logs(base: Path, ttl_days: int) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - ttl_days * 86400
    for p in base.glob("*.tar.gz"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:  # pragma: no cover - 单文件清理失败不阻上传
            continue


@app.post("/api/nodes/logs")
async def upload_node_logs(request: Request,
                           authorization: str = Header(default="")) -> dict:
    """节点日志束上报（node_token 端点内自证，同 heartbeat 模式）。

    体=原始 gzip（魔数验证，Content-Type 可伪造）；封顶 8MB（413）；
    文件名带 UTC 时间戳+体长+短随机——同秒重复上传不互相覆盖。"""
    token = authorization.removeprefix("Bearer ").strip()
    node = _node_store().resolve_node_token(token) if token else None
    if node is None:
        raise HTTPException(401, "node token required")
    node_id = str(node.get("node_id", ""))
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty body")
    if len(body) > _LOG_UPLOAD_MAX_BYTES:
        raise HTTPException(413, "log bundle too large")
    if body[:2] != b"\x1f\x8b":
        raise HTTPException(415, "expected gzip payload")
    base = _node_logs_dir(node_id)
    _sweep_node_logs(base, int(os.environ.get(
        "BOK_NODE_LOG_TTL_DAYS", str(_LOG_TTL_DAYS_DEFAULT))))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = base / f"{stamp}-{len(body)}-{uuid.uuid4().hex[:6]}.tar.gz"
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(target)
    _audit("node.logs_uploaded", subject_type="node", subject_id=node_id,
           detail={"bytes": len(body), "file": target.name})
    return {"ok": True, "node_id": node_id, "file": target.name, "bytes": len(body)}


@app.get("/api/nodes/{node_id}/logs")
def list_node_logs(node_id: str, request: Request) -> list[dict]:
    """节点日志束清单（root 专属）：新→旧，含体长与上传时刻。"""
    require_role(request, "root")
    base = _node_logs_dir(node_id)
    out: list[dict] = []
    for p in sorted(base.glob("*.tar.gz"), reverse=True):
        try:
            st = p.stat()
        except OSError:  # pragma: no cover - 并发清扫竞态
            continue
        out.append({"file": p.name, "bytes": st.st_size,
                    "uploaded_at": datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc).isoformat()})
    return out


@app.get("/api/nodes/{node_id}/logs/{filename}")
def download_node_logs(node_id: str, filename: str, request: Request) -> FileResponse:
    """下载单个日志束（root 专属）：文件名白名单 + 目录收敛校验（同工件下载）。"""
    require_role(request, "root")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz", filename):
        raise HTTPException(404, "not found")
    base = _node_logs_dir(node_id)
    target = (base / filename).resolve()
    if not target.is_file() or base.resolve() not in target.parents:
        raise HTTPException(404, "not found")
    _audit("node.logs_downloaded", subject_type="node", subject_id=node_id,
           detail={"file": filename})
    return FileResponse(target, filename=filename, media_type="application/gzip")


def node_license_required() -> bool:
    """加固模式判定：任一强制凭据开启即要求节点 license（本地双关全空=开放）。"""
    return auth_required() or bool(os.environ.get("BOK_CP_TOKEN", "").strip())


@app.post("/api/nodes/licenses")
def create_node_license(req: NodeLicenseCreateRequest, request: Request) -> dict:
    """签发节点许可证（root 专属）：key 明文只在本次响应出现一次。"""
    require_role(request, "root")
    if req.max_nodes < 1:
        raise HTTPException(400, "max_nodes must be >= 1")
    out = _node_store().create_license(
        org_id=req.org_id.strip(), account_id=req.account_id.strip(),
        max_nodes=req.max_nodes, note=req.note.strip())
    _audit("node.license_created", subject_type="license",
           subject_id=out["license_id"], account_id=req.account_id.strip(),
           detail={"max_nodes": req.max_nodes, "org_id": req.org_id.strip(),
                   "note": req.note.strip()})
    return out


@app.get("/api/nodes/licenses")
def list_node_licenses(request: Request) -> list[dict]:
    """license 清单（root 专属；永不回显 key/hash）。"""
    require_role(request, "root")
    return _node_store().list_licenses()


@app.post("/api/nodes/licenses/{license_id}/revoke")
def revoke_node_license(license_id: str, request: Request) -> dict:
    """吊销 license：名下全部节点 token 即刻失效（心跳 401）。"""
    require_role(request, "root")
    out = _node_store().revoke_license(license_id)
    if out is None:
        raise HTTPException(404, "license not found")
    _audit("node.license_revoked", subject_type="license", subject_id=license_id,
           account_id=out.get("account_id", ""),
           detail={"nodes_revoked": out.get("nodes_revoked", 0)})
    return out


@app.post("/api/nodes/{node_id}/revoke")
def revoke_node(node_id: str, request: Request) -> dict:
    """吊销单个节点（root 专属）：token 即刻失效。开放期存量 token（无 license）
    此前无任何吊销手段（2026-09-16 深测 P2）。打 source='root'=sticky：注册端点
    不得复活，唯一恢复路径是 /unrevoke（auto_clone 吊销由 store 打标、可自愈）。"""
    require_role(request, "root")
    if not _node_store().revoke_node(node_id, source="root"):
        raise HTTPException(404, "node not found")
    _audit("node.revoked", subject_type="node", subject_id=node_id,
           detail={"source": "root"})
    return {"node_id": node_id, "revoked": True}


@app.post("/api/nodes/{node_id}/unrevoke")
def unrevoke_node(node_id: str, request: Request) -> dict:
    """解除节点吊销（root 专属；sticky 吊销的唯一恢复路径，site-delivery M1）。

    语义：解除后 status→offline（节点须重注册换发 token 或心跳成功才回 online）、
    revoked_source→''；revoked_at 保留作历史（最近一次吊销时刻，审计可循）。
    404=节点不存在；409=节点本就未吊销（live）——显式冲突而非幂等 200，让误触
    （双击/竞态）可被操作面分辨，与 revoke 二次调用 404 的既有口径对齐。"""
    require_role(request, "root")
    out = _node_store().unrevoke_node(node_id)
    if out is None:
        raise HTTPException(404, "node not found")
    if out == "live":
        raise HTTPException(409, "node not revoked")
    _audit("node.unrevoked", subject_type="node", subject_id=node_id)
    return {"node_id": node_id, "revoked": False}


@app.get("/api/calls/{call_id}/settlement")
def get_settlement(call_id: str, request: Request) -> dict:
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    settlement = _repo().get_settlement(call_id)
    if not settlement:
        raise HTTPException(404, "settlement not found")
    return settlement


@app.get("/api/calls/{call_id}/turns")
def get_turns(call_id: str, request: Request) -> list[dict]:
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    return [turn.__dict__ for turn in _repo().get_turns(call_id)]


@app.get("/api/calls/{call_id}/metrics")
def get_call_metrics(call_id: str, request: Request) -> dict:
    """每通通话延迟档案:p50/p95 latency_ms + 轮数/语言分布（审计闭环 T3）。"""
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    import statistics as _stats

    turns = _repo().get_turns(call_id)
    lat = sorted(t.latency_ms for t in turns if t.latency_ms)

    def _pct(q: float) -> int:
        # 最近秩百分位:lat 升序,ceil(q*n)-1
        if not lat:
            return 0
        import math as _math

        return lat[max(0, _math.ceil(q * len(lat)) - 1)]

    langs: dict[str, int] = {}
    for t in turns:
        if t.language:
            langs[t.language] = langs.get(t.language, 0) + 1
    return {
        "call_id": call_id,
        "turns": len(turns),
        "latency_ms": {"p50": _pct(0.5), "p95": _pct(0.95), "max": lat[-1] if lat else 0, "n": len(lat)},
        "languages": langs,
    }


def _safe_segment(value: str, fallback: str) -> str:
    """vault 相对路径段白名单（2026-09-16 深测 P2）：object_id/call_id 拼路径前收敛
    成 [A-Za-z0-9_-]。旧版 create_call 收任意 object_id，settle 落盘
    accounts/{account}/objects/{object}/… 可用 ../../ 把转写/蒸馏写进他账号
    knowledge/ 并被重启索引（跨租户知识投毒）。合法 id（obj-*/call-*/acc-*）逐字节不变。"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "").strip())
    return cleaned or fallback


def _write_settlement_docs(call: dict, turns: list[dict], result: dict) -> None:
    """把通话转写与结算文档真实落盘到 vault（与 settlement 声明的 doc path 一致）。
    路径：accounts/{account}/objects/{object}/calls/{call}/transcript.md(.settlement.md)。
    失败只告警，不阻塞结算主流程。"""
    account_id = _safe_segment(call.get("account_id") or "", "acc-001")
    object_id = _safe_segment(call.get("object_id") or "", "unknown")
    call_id = _safe_segment(call.get("id") or "", "unknown")
    base = f"accounts/{account_id}/objects/{object_id}/calls/{call_id}"
    lines: list[str] = [f"# 通话转写 {call_id}", ""]
    for t in turns:
        if not isinstance(t, dict):
            t = t.__dict__ if hasattr(t, "__dict__") else {}
        role = t.get("role", "user")
        text = (t.get("transcript") or t.get("text") or "").strip()
        lines += [f"## {role}", text, ""]
    transcript_md = "\n".join(lines).strip() + "\n"

    settle_lines: list[str] = ["# 通话结算", "", f"- call_id: {call_id}", f"- status: {result.get('status', '')}", ""]
    if result.get("summary"):
        settle_lines += ["## 摘要", str(result["summary"]), ""]
    if result.get("metrics"):
        settle_lines += ["## 指标", "```json", json.dumps(result.get("metrics"), ensure_ascii=False, indent=2), "```", ""]
    settlement_md = "\n".join(settle_lines).strip() + "\n"
    try:
        markdown = getattr(app.state, "knowledge", None)
        if markdown is None:
            return
        markdown.markdown.write(f"{base}/transcript.md", transcript_md)
        markdown.markdown.write(f"{base}/settlement.md", settlement_md)
    except Exception as exc:  # pragma: no cover - doc write must not break settle
        print(f"[settle] doc write failed: {exc!r}", flush=True)


async def _write_distill_knowledge(call: dict, result: dict) -> dict | None:
    """把结算蒸馏（摘要/新话题）写进【可检索】的知识库（accounts/*/knowledge/*.md）。

    结算文档（transcript.md/settlement.md）在 objects/.../calls/ 下，知识索引不加载；
    这里把同一份蒸馏落一份到 accounts/{acc}/knowledge/{object}/{call}.md 并立即 upsert，
    使后续通话能经知识检索引用到本次沉淀。失败只告警，不阻塞结算主流程。
    """
    try:
        summary = str(result.get("summary") or "").strip()
        new_topics = result.get("new_topics") or []
        if not summary and not new_topics:
            return None
        account_id = _safe_segment(call.get("account_id") or "", "acc-001")
        object_id = _safe_segment(call.get("object_id") or "", "unknown")
        call_id = _safe_segment(call.get("id") or "", "unknown")
        lines = ["# 通话蒸馏", "", f"- call_id: {call_id}"]
        if summary:
            lines += ["", "## 摘要", summary]
        if new_topics:
            lines += ["", "## 新话题"]
            for t in new_topics:
                topic = str((t or {}).get("topic") or "").strip()
                tsum = str((t or {}).get("summary") or "").strip()
                if topic:
                    lines.append(f"- {topic}" + (f"：{tsum}" if tsum else ""))
        md = "\n".join(lines).strip() + "\n"
        # import_document 内部是 write(vault 文件) + vector.upsert(立即可检索)，
        # 与 /api/knowledge/import 同一路径；文件名按 object/{call} 组织且唯一。
        knowledge = getattr(app.state, "knowledge", None)
        if knowledge is None:
            return None
        saved = await knowledge.import_document(account_id, f"{object_id}/{call_id}.md", md)
        _audit(
            "knowledge.distill",
            subject_type="call",
            subject_id=call_id,
            account_id=account_id,
            detail={"path": f"accounts/{account_id}/knowledge/{object_id}/{call_id}.md", "indexed": saved.get("indexed", 0)},
        )
        return saved
    except Exception as exc:  # pragma: no cover - 蒸馏入库失败不阻塞结算
        print(f"[settle] distill to knowledge failed: {exc!r}", flush=True)
        return None


def _backfill_turns_from_report(call_id: str, report_raw: str) -> int:
    """SessionReport.chat_history → turns 回填（幂等：仅当该通话零轮次时调用）。

    chat_history 结构 = {"items": [{type: "message", role, content: [str...]}]}。
    回填行 turn_id 用 backfill:{i} 防与正常 t{i} 序列冲突；失败只告警不阻结算。
    返回回填行数。"""
    import json as _json

    try:
        report = _json.loads(report_raw or "{}")
        items = ((report.get("chat_history") or {}).get("items")) or []
        rows = [
            it for it in items
            if it.get("type") == "message" and it.get("role") in ("user", "assistant")
        ]
        if not rows:
            return 0
        from bok_voice_core.types import TurnEvent

        n = 0
        for i, it in enumerate(rows):
            content = it.get("content")
            text = " ".join(content) if isinstance(content, list) else str(content or "")
            text = text.strip()
            if not text:
                continue
            _repo().create_turn(
                TurnEvent(
                    trace_id=call_id,
                    call_id=call_id,
                    turn_id=f"backfill:{i}",
                    role=it["role"],
                    transcript=text,
                    provider="session_report",
                )
            )
            n += 1
        if n:
            _audit("settle.turns_backfilled", subject_type="call", subject_id=call_id,
                   detail={"count": n})
        return n
    except Exception as exc:  # pragma: no cover
        print(f"[settle] backfill failed: {exc!r}", flush=True)
        return 0


@app.post("/api/calls/{call_id}/settle")
async def settle(call_id: str, request: Request) -> dict:
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    return await _settle_core(call_id)


async def _settle_core(call_id: str) -> dict:
    """结算内核（无鉴权层）：HTTP 端点与 reaper 后台循环共用。

    2026-09-18 修复：reaper 此前直调 settle(c["id"]) 缺 request 参数——TypeError
    被兜底 except 吞掉，僵尸通话/存量补结算从上线起一直空转（每轮打印
    reaper settle skipped 报 TypeError 即其痕迹）。鉴权留在端点壳，后台路径
    走本内核。
    """
    existing = _repo().get_settlement(call_id)
    if existing:
        return existing
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    turns = _repo().get_turns(call_id)
    if not turns:
        # 回填（2026-09-07 审计闭环）：打断/强挂通话的轮次可能整批未落库
        # （conversation_item_added 未及触发），但 SessionReport.chat_history
        # 有权威快照——settle 时回填,保证「每通通话必有档案」。
        backfilled = _backfill_turns_from_report(call_id, call.get("session_report") or "")
        if backfilled:
            turns = _repo().get_turns(call_id)
    from bok_voice_core.types import CallSession

    session = CallSession(
        id=call["id"],
        account_id=call["account_id"],
        object_id=call["object_id"],
        persona_id=call.get("persona_id", ""),
        mode=CallMode(call.get("mode", "simulation")),
    )
    result = app.state.settlement.build_result(session, turns)
    # usage_records 落一笔（2026-09-07 审计闭环:表此前无写入者）。数据取自
    # session_report 的真实 llm_usage(有)或轮数估算(无),重复 settle 幂等跳过
    # ——写失败只告警不阻结算。
    try:
        from bok_voice_business_db.models import UsageRecord

        # P1-A：跨「主列 + per-worker 历史列」逐份累加 llm_usage.total_tokens
        # （B 线双 worker 各一份；单份旧数据行为不变）。坏 report 只丢该份不炸。
        tokens = 0
        for _report in _iter_call_reports(call):
            try:
                tokens += int((_report.get("llm_usage") or {}).get("total_tokens") or 0)
            except Exception:
                continue
        if not tokens:
            tokens = len(turns) * 300
        if not _repo().get_usage_record(call_id):
            _repo().session.add(
                UsageRecord(
                    id=f"usage:{call_id}",
                    account_id=call["account_id"],
                    call_id=call_id,
                    provider="local",
                    kind="call",
                    units=len(turns),
                    tokens=tokens,
                    audio_seconds=0.0,
                    latency_ms=0,
                    cost_estimate=0.0,
                    status="ok",
                )
            )
            _repo().session.commit()
    except Exception as exc:  # pragma: no cover
        print(f"[settle] usage_record write skipped: {exc!r}", flush=True)
    # 总结/沉淀：用本机 LLM 生成总结正文 + 新话题 + 全局洞察（失败回退纯指标）。
    # 可观测（2026-09-07）：单次尝试,仍空→审计事件 settle.distill_empty,
    # 唔再静默吞掉（蒸馏覆盖率从此可查）。
    try:
        from .summarize import Summarizer

        settings = _repo().get_settings()
        # P1-4（2026-09-16 深测）：Summarizer.build 是同步 httpx 调用（原 timeout
        # 60s×2 次重试），直接跑在 async 路由=事件循环整体冻结——黑洞 LLM 实测
        # /health 59.4s 停摆（turns 上报/心跳/token 全部停摆）。挪工作线程+单次
        # 尝试；蒸馏失败由审计 settle.distill_empty 可观测。
        summ = await asyncio.to_thread(Summarizer().build, turns, call, settings)
        if not (summ.get("summary") or "").strip() and turns:
            _audit("settle.distill_empty", subject_type="call", subject_id=call_id,
                   detail={"turns": len(turns)})
        # 对象级滚动摘要（专项 B2 v1:结构化拼接,LLM 增量润色为后续增强）
        if (summ.get("summary") or "").strip() and call.get("object_id"):
            try:
                from datetime import datetime as _dt

                obj = _repo().get_object(call["object_id"])
                if obj is not None:
                    prev = str(obj.get("digest") or "").strip()
                    entry = f"- {_dt.now().strftime('%Y-%m-%d')}：{summ['summary'].strip()[:150]}"
                    merged = (prev + "\n" + entry).strip()
                    lines = merged.splitlines()
                    _repo().update_object_digest(call["object_id"], "\n".join(lines[-10:]))
            except Exception as exc:  # pragma: no cover
                print(f"[settle] digest merge skipped: {exc!r}", flush=True)
        if summ.get("summary"):
            result["summary"] = summ["summary"]
        else:
            result["summary"] = ""
        result["new_topics"] = summ.get("new_topics", [])
        insight = summ.get("insight")
        if insight:
            saved = _repo().append_global_insight({**insight, "kind": "insight"})
            result["global_insight_id"] = saved.get("id", "")
        if result.get("new_topics"):
            _repo().append_object_topics(call["object_id"], call["account_id"], result["new_topics"])
    except Exception as exc:  # pragma: no cover - summarizer must not break settle
        print(f"[settle] summarizer failed: {exc!r}", flush=True)
    _write_settlement_docs(call, turns, result)
    # 蒸馏入库（可检索 knowledge）：自动沉淀经验，供后续通话引用。
    await _write_distill_knowledge(call, result)
    _repo().append_settlement(call_id, result)
    _audit(
        "settle.create",
        subject_type="call",
        subject_id=call_id,
        detail={"status": result.get("status", ""), "turns": len(turns), "has_summary": bool(result.get("summary"))},
    )
    # 挂断自动短信（W5-T1 骨架，默认关）：enabled+hangup_enabled 且有号码才发；
    # 号码=call.contact_phone，空则 object.phone 兜底（同 digest 段读法）。模板
    # hangup_template 支持 {contact} 占位=收件号码。二次 settle 在函数头被
    # existing 短路，天然不重发。失败只打点绝不破 settle（照 summarizer 段
    # 「must not break settle」先例）。
    try:
        sms_cfg = _sms_settings()
        if _sms_configured(sms_cfg) and sms_cfg.get("hangup_enabled"):
            phone = str(call.get("contact_phone") or "").strip()
            if not phone and call.get("object_id"):
                obj = _repo().get_object(call["object_id"])
                phone = str((obj or {}).get("phone") or "").strip()
            template = str(sms_cfg.get("hangup_template") or "").strip()
            if phone and template:
                status_code = await _send_sms_webhook(sms_cfg, phone, template.replace("{contact}", phone))
                _audit("sms.send", subject_type="sms", subject_id=call_id,
                       call_id=call_id, detail={"chars": len(template), "status_code": status_code, "source": "hangup"})
    except Exception as exc:  # pragma: no cover - sms hook must not break settle
        print(f"[settle] sms hook failed: {exc!r}", flush=True)
    return _repo().get_settlement(call_id) or result


@app.get("/api/objects")
def list_objects(request: Request, account_id: str = "acc-001") -> list[dict]:
    # B4 页面权限：对象读面归 objects 键（写面下面三个端点仍是 admin/root）。
    _gate_page(request, "objects")
    account_id = scoped_account(request, account_id)
    return _repo().list_objects(account_id)


@app.get("/api/objects/{object_id}")
def get_object(object_id: str, request: Request) -> dict:
    _gate_page(request, "objects")
    deny_cross_account(request, _repo().get_object(object_id))
    obj = _repo().get_object(object_id)
    if not obj:
        raise HTTPException(404, "object not found")
    return obj


@app.post("/api/objects")
def create_object(request: Request, account_id: str, req: CreateObjectRequest) -> dict:
    # 对象=客户资料，话务员只读（页面矩阵）；建/改/删归 admin/root。
    require_role(request, "admin", "root")
    account_id = scoped_account(request, account_id)
    obj = _repo().create_object(account_id, req.model_dump())
    _audit("object.create", subject_type="object", subject_id=obj.get("id", ""), account_id=account_id, detail={"display_name": obj.get("display_name", "")})
    return obj


@app.patch("/api/objects/{object_id}")
def update_object(object_id: str, req: UpdateObjectRequest, request: Request) -> dict:
    require_role(request, "admin", "root")
    deny_cross_account(request, _repo().get_object(object_id))
    existing = _repo().get_object(object_id)
    obj = _repo().update_object(object_id, req.model_dump())
    if not obj:
        raise HTTPException(404, "object not found")
    _audit("object.update", subject_type="object", subject_id=object_id, account_id=(existing or {}).get("account_id", ""), detail={"display_name": obj.get("display_name", "")})
    return obj


@app.delete("/api/objects/{object_id}")
def delete_object(object_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    deny_cross_account(request, _repo().get_object(object_id))
    existing = _repo().get_object(object_id)
    if not _repo().delete_object(object_id):
        raise HTTPException(404, "object not found")
    _audit("object.delete", subject_type="object", subject_id=object_id, account_id=(existing or {}).get("account_id", ""))
    return {"object_id": object_id, "deleted": True}


@app.post("/api/objects/{object_id}/dial-now")
async def dial_now(object_id: str, req: DialNowRequest, request: Request) -> dict:
    """单发外呼「立即外呼」（spec 2026-09-13 P1.5 T4）：建一通 outbound 通话并直接派 agent。

    复用 campaign 的派发链路，只是名单退化成「就这一通」：
    `build_dial_block`（键序/trunk 解析/数字兜底与 campaign 同源，T2 站点优先）→
    `_create_call_in`（`direction=outbound` / `mode=live`，agent 侧零改，靠 metadata
    的 dial 块拨号）→ `_default_dispatcher`（= campaign 的 explicit dispatch，
    `campaign_item_id` 留空：不属任何战役名单）。

    失败面：对象不存在=404；对象无电话（strip 后空）=400；派发失败（LiveKit 凭据缺/
    不可达）=502——通话已建好留在库里便于排查（campaign 落 item failed 同语义），
    悬挂的 ringing 通话由僵尸回收器兜底翻 FAILED。
    """
    _gate_page(request, "calls")
    repo = _repo()
    obj = deny_cross_account(request, repo.get_object(object_id))
    if not obj:
        raise HTTPException(404, "object not found")
    phone = str(obj.get("phone") or "").strip()
    if not phone:
        raise HTTPException(400, "对象无电话号码")
    account_id = str(obj.get("account_id") or "acc-001")
    settings = repo.get_settings() or {}
    # 语言缺省链：请求 language > 对象 language > zh（粤语值只准 cantonese）。
    language = req.language or str(obj.get("language") or "zh")
    site = repo.get_site(str(req.site_id or "")) if req.site_id else None
    from .campaign import build_dial_block  # 延迟 import 防 main↔campaign 循环

    dial = build_dial_block(
        number=phone,
        language=language,
        sip=dict(settings.get("sip") or {}),
        site=site,
    )
    call = _create_call_in(repo, CreateCallRequest(
        account_id=account_id,
        object_id=object_id,
        persona_id=req.persona_id or "",
        language=dial["language"],
        mode=CallMode.LIVE,
        direction="outbound",
    ))
    call_id = str(call.get("id") or "")
    repo.update_call(call_id, contact_phone=phone)
    # 话术快照：显式 template_id 压过对象绑定模板（call_sessions.template_id 是
    # agent 装配的第一优先来源，与 campaign/工作台建单同优先级）。
    if req.template_id:
        repo.update_call(call_id, template_id=req.template_id)
    metadata = json.dumps({"call_id": call_id, "dial": dial}, ensure_ascii=False)
    from .campaign import _default_dispatcher

    try:
        await _default_dispatcher(call_id, metadata)
    except Exception as exc:  # noqa: BLE001 - 凭据缺/不可达统一成 502（不裸 500）
        _audit("call.dial_now", subject_type="call", subject_id=call_id,
               outcome="error", account_id=account_id,
               detail={"object": object_id, "error": str(exc)[:200]})
        raise HTTPException(502, f"外呼派发失败——LiveKit 不可达: {exc}") from exc
    _audit("call.dial_now", subject_type="call", subject_id=call_id,
           account_id=account_id, detail={"object": object_id})
    return {"call_id": call_id, "status": str(call.get("status") or "")}


@app.post("/api/objects/import")
def import_objects(request: Request, account_id: str, rows: list[CreateObjectRequest] = Body(...)) -> dict:
    require_role(request, "admin", "root")
    if len(rows) > 500:
        raise HTTPException(400, "单次导入上限 500 行")
    account_id = scoped_account(request, account_id)
    created = [_repo().create_object(account_id, row.model_dump()) for row in rows]
    _audit("object.import", subject_type="object", account_id=account_id,
           detail={"rows": len(created)})
    return {"imported": len(created), "items": created}


@app.get("/api/knowledge/search")
async def search_knowledge(request: Request, query: str, account_id: str = "acc-001", limit: int = 5) -> list[dict]:
    # 知识库=org 资产，话务员不可见（页面矩阵）。
    require_role(request, "admin", "root")
    account_id = scoped_account(request, account_id)
    return await app.state.knowledge.search(query, account_id, limit)


@app.get("/api/knowledge")
async def list_knowledge(request: Request, account_id: str = "acc-001") -> list[dict]:
    require_role(request, "admin", "root")
    account_id = scoped_account(request, account_id)
    return await app.state.knowledge.list(account_id)


@app.delete("/api/knowledge")
async def delete_knowledge(request: Request, knowledge_id: str, account_id: str = "acc-001") -> dict:
    require_role(request, "admin", "root")
    account_id = scoped_account(request, account_id)
    # id 形如 md:accounts/acc-001/knowledge/probe.md（含斜杠），放 path 参数会被
    # Starlette 路由层以 %2F 拒掉（404）——改走 query 参数最稳。
    removed = await app.state.knowledge.delete(account_id, [knowledge_id])
    _audit("knowledge.delete", subject_type="knowledge", subject_id=knowledge_id, account_id=account_id, detail={"removed": removed})
    return {"deleted": removed, "knowledge_id": knowledge_id}


@app.post("/api/knowledge/import")
async def import_knowledge(req: ImportRequest, request: Request) -> dict:
    require_role(request, "admin", "root")
    # 账号收窄（2026-09-16 深测 P2）：读侧 search/list/delete 都过 scoped_account，
    # 写侧曾原样收 body account_id——admin 可写穿他账号知识命名空间（读写口径分裂）。
    account_id = scoped_account(request, req.account_id)
    # 路径段校验：vault 内相对路径不允许 ..（跨目录挪位；与 LocalMarkdownSource
    # 的 root 逃逸守卫互补——那层只防逃出 vault，不防 vault 内跨账号目录）。
    parts = [p for p in req.path.replace("\\", "/").split("/") if p]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail="path must not contain '..'")
    result = await app.state.knowledge.import_document(account_id, req.path, req.content)
    _audit("knowledge.import", subject_type="knowledge", subject_id=req.path or "",
           account_id=account_id, detail={"content_len": len(req.content)})
    return result


# ---- 快答库(Q→A 检索快路,2026-09-09):条目 CRUD + 高频配对报告 ----

@app.get("/api/qa-entries")
def list_qa_entries(request: Request, account_id: str = "acc-001", enabled: int | None = None, owner_scope: str | None = None) -> list[dict]:
    # QA 库话务员可读可编辑（页面矩阵），按账号收窄；B3 owner 维度：user=自己的+共享
    # （owner_scope_filter 强制本人），admin/root/无身份不过滤；机器通道显式传
    # owner_scope=建单人（agent 装配线），''=仅共享（战役等无主通话）。
    # B4 页面权限归 qa 键（hit 端点不在此闸：agent 机器通道）。
    _gate_page(request, "qa")
    account_id = scoped_account(request, account_id)
    return _repo().list_qa_entries(
        account_id, enabled=None if enabled is None else bool(enabled),
        owner_scope=owner_scope_filter(request, owner_scope),
    )


def _clamp_priority(value: int | None) -> int:
    """QA 优先级钳制(Phase 3.1):[0,1000],缺省 10。0 是合法值不回退默认。"""
    if value is None:
        return 10
    return max(0, min(int(value), 1000))


@app.post("/api/qa-entries")
def create_qa_entry(req: QaEntryCreate, request: Request) -> dict:
    _gate_page(request, "qa")
    identity = current_identity(request)
    if identity is not None and identity.role != "root":
        # B3：user 建的自动归自己（body 的 owner 无效）；admin 建默认共享、可显式指派。
        updates: dict = {"account_id": identity.account_id, "priority": _clamp_priority(req.priority)}
        if identity.role == "user":
            updates["owner_user_id"] = identity.user_id
        req = req.model_copy(update=updates)
    else:
        req = req.model_copy(update={"priority": _clamp_priority(req.priority)})
    row = _repo().create_qa_entry(req.model_dump())
    _audit("qa_entry.create", subject_type="qa_entry", subject_id=row.get("id", ""), account_id=req.account_id,
           detail={"owner_user_id": row.get("owner_user_id", "")})
    return row


@app.patch("/api/qa-entries/{entry_id}")
def update_qa_entry(entry_id: str, req: QaEntryPatch, request: Request) -> dict:
    _gate_page(request, "qa")
    deny_foreign_owner(request, deny_cross_account(request, _repo().get_qa_entry(entry_id)), edit=True)
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    # 所有权转移只归 admin/root——user 的 patch 剥掉。
    ident = current_identity(request)
    if ident is not None and ident.role == "user":
        patch.pop("owner_user_id", None)
    if "priority" in patch:
        patch["priority"] = _clamp_priority(patch["priority"])
    row = _repo().update_qa_entry(entry_id, patch)
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="qa entry not found")
    _audit("qa_entry.update", subject_type="qa_entry", subject_id=entry_id)
    return row


@app.delete("/api/qa-entries/{entry_id}")
def delete_qa_entry(entry_id: str, request: Request) -> dict:
    _gate_page(request, "qa")
    deny_foreign_owner(request, deny_cross_account(request, _repo().get_qa_entry(entry_id)), edit=True)
    ok = _repo().delete_qa_entry(entry_id)
    _audit("qa_entry.delete", subject_type="qa_entry", subject_id=entry_id, outcome="ok" if ok else "not_found")
    return {"deleted": ok, "id": entry_id}


@app.post("/api/qa-entries/{entry_id}/hit")
def hit_qa_entry(entry_id: str, request: Request) -> dict:
    """agent 快路命中计数(fire-and-forget,幂等无副作用)。"""
    deny_cross_account(request, _repo().get_qa_entry(entry_id))
    _repo().incr_qa_hit(entry_id)
    return {"id": entry_id}


# ---- 意向规则(W4-T1,2026-09-19):条件规则 → 挂断意向码/处置,两级作用域 ----
# 作用域语义:account_id ''=全局行(admin/root 写)∪账号行(话务员写),读=两级行合并。
# 条件形状与评估纯函数=packages/core/bok_voice_core/intent_rules.py(CP 只消费)。


def _intent_rule_out(row: dict) -> dict:
    """出仓形状:conditions_json → conditions 对象数组(宽容 json.loads,坏=空数组)。"""
    out = dict(row)
    raw = out.pop("conditions_json", "[]")
    try:
        conds = json.loads(raw if isinstance(raw, str) else "[]")
    except Exception:
        conds = None
    out["conditions"] = conds if isinstance(conds, list) else []
    return out


def _forbid_user_on_global_rule(request: Request, row: dict) -> None:
    """全局行(account_id=='')只归 admin/root——user/普通身份 403(机器通道无身份直通)。"""
    if str(row.get("account_id") or "") != "":
        return
    ident = current_identity(request)
    if ident is not None and ident.role not in ("admin", "root"):
        raise HTTPException(status_code=403, detail="global intent rule requires admin/root")


def _gate_intent_rule_row(request: Request, row: dict | None) -> dict:
    """by-ID 规则闸链(W4-T1):404 不泄露存在性;全局行走角色闸、账号行走跨账号闸。

    全局行不进 deny_cross_account——admin 属某账号,按账号闸会把他该管的全局行
    误杀成 404;角色闸(user→403)已先行收窄到 admin/root/机器。
    """
    if row is None:
        raise HTTPException(status_code=404, detail="intent rule not found")
    if str(row.get("account_id") or "") == "":
        _forbid_user_on_global_rule(request, row)
    else:
        deny_cross_account(request, row)
    return row


@app.get("/api/intent-rules")
def list_intent_rules(request: Request, account_id: str = "acc-001") -> list[dict]:
    _gate_page(request, "calls")
    account_id = scoped_account(request, account_id)
    return [_intent_rule_out(r) for r in _repo().list_intent_rules(account_id)]


@app.post("/api/intent-rules")
def create_intent_rule(req: IntentRuleCreate, request: Request) -> dict:
    _gate_page(request, "calls")
    errs = validate_conditions(req.conditions)
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))
    identity = current_identity(request)
    if identity is not None and identity.role != "root":
        # 非 root 强制本账号行(body 的 account_id 无效;''=全局行仅 root 可建)。
        req = req.model_copy(update={"account_id": identity.account_id})
    payload = req.model_dump()
    payload["conditions_json"] = json.dumps(payload.pop("conditions"), ensure_ascii=False)
    row = _repo().create_intent_rule(payload)
    _audit("intent.rule.create", subject_type="intent_rule", subject_id=row.get("id", ""),
           account_id=row.get("account_id", ""),
           detail={"intent_code": row.get("intent_code", ""), "account_id": row.get("account_id", "")})
    return _intent_rule_out(row)


@app.patch("/api/intent-rules/{rule_id}")
def update_intent_rule(rule_id: str, req: IntentRulePatch, request: Request) -> dict:
    _gate_page(request, "calls")
    row = _gate_intent_rule_row(request, _repo().get_intent_rule(rule_id))
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if "conditions" in patch:
        errs = validate_conditions(patch["conditions"])
        if errs:
            raise HTTPException(status_code=400, detail="; ".join(errs))
        patch["conditions_json"] = json.dumps(patch.pop("conditions"), ensure_ascii=False)
    updated = _repo().update_intent_rule(rule_id, patch)
    if updated is None:
        raise HTTPException(status_code=404, detail="intent rule not found")
    _audit("intent.rule.update", subject_type="intent_rule", subject_id=rule_id,
           account_id=row.get("account_id", ""), detail={"keys": sorted(patch.keys())})
    return _intent_rule_out(updated)


@app.delete("/api/intent-rules/{rule_id}")
def delete_intent_rule(rule_id: str, request: Request) -> dict:
    _gate_page(request, "calls")
    row = _gate_intent_rule_row(request, _repo().get_intent_rule(rule_id))
    ok = _repo().delete_intent_rule(rule_id)
    _audit("intent.rule.delete", subject_type="intent_rule", subject_id=rule_id,
           account_id=row.get("account_id", ""), outcome="ok" if ok else "not_found")
    return {"deleted": ok, "id": rule_id}


# ---- 罐头状态面(2026-09-17 qa-canvas Phase 1 Task 3) ----


@app.get("/api/qa/canned-status")
def qa_canned_status_ep(request: Request, account_id: str = "acc-001") -> dict:
    """QA 条目罐头物化状态(透传 pregen_tts --qa-status,TTL 缓存;画布状态面用)。"""
    _gate_page(request, "qa")
    scoped_account(request, account_id)
    out = pregen_mod.qa_canned_status(str(request.base_url).rstrip("/"))
    return {
        "available": out["available"],
        "statuses": out["statuses"],
        "generated_at": out["generated_at"],
    }


@app.get("/api/qa/{entry_id}/canned-audio")
def qa_canned_audio(entry_id: str, request: Request) -> Response:
    """试听=罐头缓存回放,零云费,qa 页面权限即可;404=缺料(前端回退 preview,烧云归 admin)。"""
    _gate_page(request, "qa")
    deny_cross_account(request, _repo().get_qa_entry(entry_id))
    out = pregen_mod.qa_canned_status(str(request.base_url).rstrip("/"))
    info = (out.get("statuses") or {}).get(entry_id) or {}
    key = str(info.get("key") or "")
    if info.get("state") != "ok" or not re.fullmatch(r"[0-9a-f]{40}", key):
        raise HTTPException(status_code=404, detail="canned audio not materialized")
    try:
        pcm = (pregen_mod.cache_root() / f"{key}.pcm").read_bytes()
    except OSError:
        # 缓存被逐出/目录漂移=罐头缺料,按 404 交前端回退,不当 500。
        raise HTTPException(status_code=404, detail="canned audio not materialized")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return Response(content=buf.getvalue(), media_type="audio/wav")


@app.post("/api/qa/pregen")
def qa_pregen_ep(payload: dict, request: Request) -> dict:
    """手动触发 --qa 物化(可限 ids);烧云配额操作,与 /api/tts/preview 同闸同审计。"""
    require_role(request, "admin", "root")
    ids = [str(x) for x in (payload.get("ids") or [])]
    out = pregen_mod.qa_pregen_spawn(str(request.base_url).rstrip("/"), ids)
    _audit("qa.pregen", subject_type="qa_entry", subject_id=",".join(ids)[:128],
           detail={"count": len(ids), "status": out.get("status")})
    return out


@app.post("/api/qa/cluster")
def qa_cluster_ep(req: QaClusterRequest, request: Request, account_id: str = "acc-001") -> dict:
    """自学习聚类(W3-T1,2026-09-19):dry=挖掘→LLM 三列计划;apply=true 按 select 采纳入库。

    逻辑全在 qa_cluster runner(端点瘦):挖掘与 /api/reports/qa-pairs 同源
    (mine_qa_pairs);纯函数与 tts-mine --cluster 同源(bok_voice_core.qa_cluster);
    dry 计划 600s per-account 缓存,apply 优先吃新鲜缓存免二次 LLM。
    LLM 网络/解析失败 503(文案带原因);单飞冲突 409。junk 只展示不入库。
    """
    _gate_page(request, "qa")
    account_id = scoped_account(request, account_id)
    limit = max(1, min(int(req.limit), 100))
    min_calls = max(1, int(req.min_calls))
    select = (
        [{"kind": str(s.kind), "i": int(s.i)} for s in req.select]
        if req.select is not None
        else None
    )
    # 勾选采纳守卫(主会话审计修复):select 下标只在「与 dry 同参数的新鲜缓存」上有效,
    # 缓存过期/参数不符时静默重算=下标可能对到另一份计划采错条目——409 让前端重新
    # 生成;select=None 的「采纳全部」可安全重算(语义=采纳当前计划全量)。
    if req.apply and select is not None and not qa_cluster_mod.has_fresh_plan(account_id, min_calls, limit):
        raise HTTPException(status_code=409, detail="聚类计划已过期或参数不符，请重新生成计划后再采纳")
    try:
        plan = qa_cluster_mod.run_cluster(_repo(), account_id, min_calls=min_calls, limit=limit)
        if not req.apply:
            return plan
        return qa_cluster_mod.apply_cluster(
            _repo(), request, account_id, plan, select, audit=_audit
        )
    except qa_cluster_mod.AlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except qa_cluster_mod.ClusterError as exc:
        raise HTTPException(status_code=503, detail=f"聚类 LLM 不可用: {exc}") from exc


# ---- 垫话罐头库(2026-09-13 乙节):确定性语境命中,镜像 qa_entries ----


@app.get("/api/fillers")
def list_filler_entries(request: Request, account_id: str = "acc-001", enabled: int | None = None, lang: str = "") -> list[dict]:
    # 垫话罐头=运营配置面（agent 机器通道直通），话务员不可见。
    require_role(request, "admin", "root")
    account_id = scoped_account(request, account_id)
    return _repo().list_filler_entries(
        account_id, enabled=None if enabled is None else bool(enabled), lang=lang
    )


@app.post("/api/fillers/{entry_id}/hit")
def hit_filler_entry(entry_id: str, request: Request) -> dict:
    """agent 垫话罐头命中计数(fire-and-forget,幂等无副作用)。

    2026-09-16 深测 P3：旧版完全无闸（对照 qa hit 有 deny_cross_account）——
    经列表全量反查目标条目后走同一 404 口径；qa 键页面闸兜匿名刷计数。
    """
    _gate_page(request, "qa")
    entry = next((e for e in _repo().list_filler_entries("")
                  if str(e.get("id") or "") == entry_id), None)
    deny_cross_account(request, entry)
    if not entry:
        raise HTTPException(status_code=404, detail="not found")
    _repo().incr_filler_hit(entry_id)
    return {"id": entry_id}


@app.get("/api/reports/qa-pairs")
def report_qa_pairs(
    request: Request,
    min_calls: int = 5, account_id: str = "acc-001", limit: int = 100, exclude_test: bool = True
) -> list[dict]:
    """高频问答对挖掘报告:用户轮→紧随 assistant 轮,归一化聚类按出现通话数排序。

    与运行时匹配共用 bok_voice_core.qa_text.normalize_question,报告里的问句
    到运行时才对得上。--apply 入库走 POST /api/qa-entries(source=mined),
    音频物化统一走 agent 侧 bok.py tts-pregen。
    exclude_test 默认滤掉测试对象(E2E/压测前缀族)的通话——真实采集时代的
    报告要干净(2026-09-09 实测 top20 高频里 16 条是测试 fixture 音频);
    显式传 false 看全量。
    """
    # B4：报表面归 reports 键（主管默认关、可按人开），替换原 admin/root 角色闸。
    _gate_page(request, "reports")
    account_id = scoped_account(request, account_id)
    conversations = _repo().iter_call_conversations(account_id, exclude_test_objects=exclude_test)
    return mine_qa_pairs(conversations, min_calls=min_calls, limit=limit)


@app.get("/api/personas")
def list_personas(request: Request, account_id: str = "acc-001") -> list[dict]:
    # 人设=管理面（页面矩阵：话务员不可见）。
    require_role(request, "admin", "root")
    account_id = scoped_account(request, account_id)
    return _repo().list_personas(account_id)


@app.get("/api/personas/{persona_id}")
def get_persona(persona_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    persona = deny_cross_account(request, _repo().get_persona(persona_id))
    if not persona:
        raise HTTPException(404, "persona not found")
    return persona


@app.post("/api/personas")
def create_persona(req: PersonaRequest, request: Request) -> dict:
    require_role(request, "admin", "root")
    identity = current_identity(request)
    if identity is not None and identity.role != "root":
        # admin 建人设强制本账号（深测：曾可建进/挪进任意账号）。
        req = req.model_copy(update={"account_id": identity.account_id})
    persona = _repo().create_persona(req.model_dump())
    _audit("persona.create", subject_type="persona", subject_id=persona.get("id", ""), account_id=persona.get("account_id", ""), detail={"name": persona.get("name", "")})
    # 新人设上线:无罐头即提醒+自动全量物化(W3,响应 tts_pregen=提醒面)。
    # 装饰浅拷贝——内存 repo 返回活引用,直接写会把一次性状态键落进存储。
    out = dict(persona)
    out["tts_pregen"] = persona_pregen_status(
        out, base_url=str(request.base_url).rstrip("/")
    )
    return out


@app.put("/api/personas/{persona_id}")
def update_persona(persona_id: str, req: UpdatePersonaRequest, request: Request) -> dict:
    require_role(request, "admin", "root")
    # 冻结更新前快照(内存 repo 返回活引用,update 原地改会令 existing==persona,
    # 音色变化判定恒 False);audit 与物化触发都以此为准。
    existing = dict(_repo().get_persona(persona_id) or {})
    if existing:
        deny_cross_account(request, existing)
    identity = current_identity(request)
    if existing and identity is not None and identity.role != "root":
        # 冻结归属：非 root 不得经 UpdatePersonaRequest.account_id 挪账号。
        req = req.model_copy(update={"account_id": str(existing.get("account_id") or "")})
    persona = _repo().update_persona(persona_id, req.model_dump())
    if not persona:
        raise HTTPException(404, "persona not found")
    _audit("persona.update", subject_type="persona", subject_id=persona_id, account_id=(existing or {}).get("account_id", ""), detail={"name": persona.get("name", "")})
    out = dict(persona)
    out["tts_pregen"] = persona_pregen_status(
        out, base_url=str(request.base_url).rstrip("/"), existing=existing
    )
    return out


@app.put("/api/personas")
def upsert_persona(req: PersonaRequest, request: Request) -> dict:
    require_role(request, "admin", "root")
    identity = current_identity(request)
    if identity is not None and identity.role != "root":
        # admin 建人设强制本账号（深测：曾可建进/挪进任意账号）。
        req = req.model_copy(update={"account_id": identity.account_id})
    persona = _repo().create_persona(req.model_dump())
    _audit("persona.upsert", subject_type="persona", subject_id=persona.get("id", ""),
           account_id=req.account_id, detail={"name": req.name})
    out = dict(persona)
    out["tts_pregen"] = persona_pregen_status(
        out, base_url=str(request.base_url).rstrip("/")
    )
    return out


@app.delete("/api/personas/{persona_id}")
def delete_persona(persona_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    existing = deny_cross_account(request, _repo().get_persona(persona_id))
    if not _repo().delete_persona(persona_id):
        raise HTTPException(404, "persona not found")
    _audit("persona.delete", subject_type="persona", subject_id=persona_id, account_id=(existing or {}).get("account_id", ""))
    return {"persona_id": persona_id, "deleted": True}


def _validate_graph_field(raw: str) -> list[str]:
    """模板 graph_json 保存校验:空串=未启用放行;非法返回错误列表(→400)。"""
    if not str(raw or "").strip():
        return []
    return validate_flow_graph(str(raw))


# ---- 模板发布两态(W2-T1,2026-09-19):保存=草稿、发布=冻结即生效 ----
# 冻结 payload 九键(接口冻结):发布时按当时 live 值原样收进 published_json;
# 「已发布」≡published_json 非空,「有未发布改动」≡九键逐一比对 live≠冻结
# (服务端派生布尔,不落列——消灭 status 与快照的双列状态同步漂移)。
_TEMPLATE_PUBLISH_KEYS = (
    "steps_json",
    "graph_json",
    "hotwords",
    "tone_override",
    "opening",
    "core",
    "objection",
    "closing",
    "language",
)


def _template_published_flags(row: dict) -> dict:
    """派生发布态布尔(单点助手,列表与详情共用):published + has_changes。

    冻结 payload 解析失败/非 object 视为 has_changes=False(无法比对时保守报
    「无改动」)且照常返回 published——坏快照不得炸掉读端点。"""
    frozen_raw = str(row.get("published_json") or "")
    published = bool(frozen_raw.strip())
    has_changes = False
    if published:
        try:
            frozen = json.loads(frozen_raw)
        except Exception:
            frozen = None
        if isinstance(frozen, dict):
            has_changes = any(
                str(row.get(key) or "") != str(frozen.get(key) or "")
                for key in _TEMPLATE_PUBLISH_KEYS
            )
    return {"published": published, "has_changes": has_changes}


def _template_machine_overlay(row: dict) -> dict:
    """机器通道 GET 详情 overlay:已发布模板把冻结九键覆盖进行 dict 后返回
    (agent 建单装配恒吃发布冻结版,编辑中的 live 草稿不影响在途话术)。

    payload 解析失败回退 live(宽容 try,不炸读路径);未发布(空串)原样返回。
    人类通道不调用本函数——恒读 live。列表端点不 overlay。"""
    frozen_raw = str(row.get("published_json") or "")
    if not frozen_raw.strip():
        return row
    try:
        frozen = json.loads(frozen_raw)
    except Exception:
        return row
    if not isinstance(frozen, dict):
        return row
    merged = dict(row)
    for key in _TEMPLATE_PUBLISH_KEYS:
        if key in frozen:
            merged[key] = frozen[key]
    return merged


@app.get("/api/templates")
def list_templates(request: Request, account_id: str = "acc-001", owner_scope: str | None = None) -> list[dict]:
    # B3 owner 维度同 qa-entries：user=自己的+共享，admin/root/无身份不过滤。
    _gate_page(request, "templates")
    account_id = scoped_account(request, account_id)
    return [
        {**row, **_template_published_flags(row)}
        for row in _repo().list_templates(account_id, owner_scope=owner_scope_filter(request, owner_scope))
    ]


@app.get("/api/templates/{template_id}")
def get_template(template_id: str, request: Request) -> dict:
    _gate_page(request, "templates")
    tpl = deny_foreign_owner(request, deny_cross_account(request, _repo().get_template(template_id)))
    if not tpl:
        raise HTTPException(404, "template not found")
    # 机器通道(agent 装配)恒吃发布冻结版;人类通道恒 live(编辑器要见草稿)。
    if getattr(request.state, "machine", False):
        tpl = _template_machine_overlay(tpl)
    return {**tpl, **_template_published_flags(tpl)}


@app.post("/api/templates")
def create_template(req: TemplateRequest, request: Request) -> dict:
    _gate_page(request, "templates")
    identity = current_identity(request)
    if identity is not None and identity.role != "root":
        # 话务员建话术落本账号；B3：user 建的自动归自己，admin 建默认共享、可显式指派。
        updates: dict = {"account_id": identity.account_id}
        if identity.role == "user":
            updates["owner_user_id"] = identity.user_id
        req = req.model_copy(update=updates)
    _graph_errors = _validate_graph_field(req.graph_json)
    if _graph_errors:
        raise HTTPException(400, {"error": "invalid_graph_json", "detail": _graph_errors[:5]})
    tpl = _repo().create_template(req.model_dump())
    _audit("template.create", subject_type="template", subject_id=tpl.get("id", ""), account_id=tpl.get("account_id", ""),
           detail={"name": tpl.get("name", ""), "owner_user_id": tpl.get("owner_user_id", ""),
                   "graph_saved": bool(req.graph_json)})
    return tpl


@app.put("/api/templates/{template_id}")
def update_template(template_id: str, req: UpdateTemplateRequest, request: Request) -> dict:
    _gate_page(request, "templates")
    before = deny_foreign_owner(request, deny_cross_account(request, _repo().get_template(template_id)), edit=True)
    if not before:
        raise HTTPException(404, "template not found")
    # exclude_unset:部分更新只写请求里显式出现的键——schema 全字段带默认值
    # (language 默认 zh/name 默认空),整包 dump 会把未传字段抹掉(2026-09-09
    # QA「PUT 抹字段」实锤;前端 save 恒传全字段,行为不变,API 语义修正)。
    payload = req.model_dump(exclude_unset=True)
    # 话术图校验只对显式携带的键生效(exclude_unset:未传=不动存量图)。
    # 必须排在 revision 快照之前:append_template_revision 自带 commit,
    # 校验晚于它会令每个被拒的保存都白写一条与现状等同的版本行并吃掉版本号
    # (autosave 编辑器可刷满历史),「拒绝对数据无副作用」才成立(review R1)。
    if "graph_json" in payload:
        _graph_errors = _validate_graph_field(str(payload.get("graph_json") or ""))
        if _graph_errors:
            raise HTTPException(400, {"error": "invalid_graph_json", "detail": _graph_errors[:5]})
    # 话术版本化（2026-09-07 专项 B3）:update 即快照旧版——「哪版话术转化更好」
    # 从数据上可答;call_sessions.template_id 快照指向的版本内容不再随更新漂移。
    # default=str:SQL repo 的 before 含 datetime(created_at),不转直接 500——
    # 每次编辑保存必炸且静默丢改动(2026-09-09 QA B2 实锤;in-memory 测试替身
    # 无 created_at 字段所以单测全绿,prod-only 断裂)。
    revision = len(_repo().list_template_revisions(template_id)) + 1
    import json as _revjson

    _repo().append_template_revision(template_id, revision, _revjson.dumps(before, ensure_ascii=False, default=str))
    # 归属字段冻结（B3 堵洞）：非 root 不得经 body 改 account_id（update 白名单曾放行，
    # 可把模板挪去别账号）；所有权转移（owner_user_id）只归 admin/root。
    ident = current_identity(request)
    if ident is not None and ident.role != "root":
        payload.pop("account_id", None)
    if ident is not None and ident.role == "user":
        payload.pop("owner_user_id", None)
    tpl = _repo().update_template(template_id, payload)
    _audit(
        "template.update",
        subject_type="template",
        subject_id=template_id,
        account_id=tpl.get("account_id", ""),
        detail={"name": tpl.get("name", ""), "revision": revision, "graph_saved": bool(payload.get("graph_json")),
                "changed": sorted(k for k in req.model_dump() if req.model_dump().get(k) not in (None, "") and before.get(k) != req.model_dump().get(k))},
    )
    return tpl


@app.post("/api/templates/{template_id}/publish")
def publish_template(template_id: str, request: Request) -> dict:
    """发布=冻结当时 live 九键写 published_json(模板发布两态 W2-T1)。

    闸链逐字与 PUT 同族:gate_page + deny_cross_account + deny_foreign_owner(edit)
    ——共享基线只归 admin/root。PUT 永不触碰 published_json,发布是唯一写入口;
    建单装配(agent 机器通道 GET)恒吃冻结版,草稿(live)随后怎么改都不影响在途话术。
    """
    _gate_page(request, "templates")
    tpl = deny_foreign_owner(request, deny_cross_account(request, _repo().get_template(template_id)), edit=True)
    if not tpl:
        raise HTTPException(404, "template not found")
    frozen = {key: str(tpl.get(key) or "") for key in _TEMPLATE_PUBLISH_KEYS}
    updated = _repo().update_template(
        template_id, {"published_json": json.dumps(frozen, ensure_ascii=False)}
    )
    _audit(
        "template.publish",
        subject_type="template",
        subject_id=template_id,
        account_id=updated.get("account_id", ""),
        detail={"name": updated.get("name", ""), "language": frozen.get("language", "")},
    )
    return {**updated, **_template_published_flags(updated)}


@app.get("/api/templates/{template_id}/revisions")
def template_revisions(template_id: str, request: Request) -> list[dict]:
    _gate_page(request, "templates")
    deny_foreign_owner(request, deny_cross_account(request, _repo().get_template(template_id)))
    return _repo().list_template_revisions(template_id)


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str, request: Request) -> dict:
    _gate_page(request, "templates")
    tpl = deny_foreign_owner(request, deny_cross_account(request, _repo().get_template(template_id)), edit=True)
    if not _repo().delete_template(template_id):
        raise HTTPException(404, "template not found")
    _audit("template.delete", subject_type="template", subject_id=template_id, account_id=(tpl or {}).get("account_id", ""))
    return {"template_id": template_id, "deleted": True}


@app.get("/api/supervisor/active-calls")
def active_calls(request: Request) -> list[dict]:
    """主管台可见通话：进行中(active) + 已暂停(paused / 人工接管)。"""
    # 主管台=管理面（话务员不可见），且按账号收窄。
    require_role(request, "admin", "root")
    calls = _repo().list_calls(scoped_account(request, ""), "") if hasattr(_repo(), "list_calls") else []
    return [c for c in calls if c.get("status") in (CallStatus.ACTIVE.value, CallStatus.PAUSED.value)]


@app.get("/api/reports/summary")
def reports_summary(request: Request, account_id: str = "acc-001") -> dict:
    _gate_page(request, "reports")
    account_id = scoped_account(request, account_id)
    calls = _repo().list_calls(account_id, "")
    turns_total = 0
    settled = 0
    active = 0
    for call in calls:
        turns_total += len(_repo().get_turns(call["id"]))
        status = call.get("status", "")
        if status == "active":
            active += 1
        if _repo().get_settlement(call["id"]):
            settled += 1
    return {
        "total_calls": len(calls),
        "active_calls": active,
        "settled_calls": settled,
        "total_turns": turns_total,
    }


@app.get("/api/reports/calls")
def reports_calls(request: Request, account_id: str = "acc-001") -> list[dict]:
    _gate_page(request, "reports")
    return _repo().list_calls(scoped_account(request, account_id), "")


_DURATION_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0-15", 0, 15), ("15-30", 15, 30), ("30-60", 30, 60), ("60-90", 60, 90), ("90+", 90, None))
_ANSWERED_EXCLUDED = {"no_answer", "rejected", "failed"}


def _duration_bucket(duration_s: int) -> str | None:
    for name, low, high in _DURATION_BUCKETS:
        if duration_s >= low and (high is None or duration_s < high):
            return name
    return None


def _local_midnight_utc_boundary() -> datetime:
    """今日边界（T5-M3）：本地午夜对应的 UTC naive 时刻（单一公式，测试同源现算）。

    today 判定=created_at（经 _parse_updated_at 归一为 naive UTC）≥ 该边界；
    旧「存储串 startswith 本地日期前缀」把 UTC 串与本地日错配（正时区 UTC 深夜
    时段本地已是明天 → 漏计；负时区反向多计）。
    """
    midnight_local = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.astimezone(timezone.utc).replace(tzinfo=None)


_TODO_FAIL_STATUSES = ("no_answer", "rejected", "failed")


def _dashboard_todo(repo, now: datetime | None = None) -> dict:
    """待办事项（2026-09-18 用户补卡）：运行中战役的「接下来要处理什么」四桶账。

    - to_call: 首拨待外呼（pending 且 attempts<=1）——排期内该拨的名单量；
    - waiting_redispatch: 已拨过、在等重拨间隔走完（redispatch_due=True=未到期）；
    - due_redispatch: 间隔已到、下一轮 campaign_tick 即拨（redispatch_due=False；
      未配重拨策略的 attempts>=2 pending 也落这桶——机制上它下一轮就会被拨）；
    - exhausted: 重拨预算打满仍终态失败（no_answer/rejected/failed 且
      attempts>=max_attempts）——转人工跟进。

    非运行中战役不进待办（draft/paused/done 都不是「接下来」）。now 注入同
    campaign_tick 语义（UTC naive），测试钉钟用。
    """
    to_call = waiting = due = exhausted = 0
    for campaign in repo.list_campaigns("", status="running"):
        max_attempts = int(redispatch_policy(campaign)["max_attempts"])
        for item in repo.list_items(str(campaign["id"])):
            status = str(item.get("status") or "")
            attempts = int(item.get("attempts") or 1)
            if status == "pending":
                if attempts <= 1:
                    to_call += 1
                elif redispatch_due(item, campaign, now):
                    waiting += 1
                else:
                    due += 1
            elif (status in _TODO_FAIL_STATUSES and max_attempts > 0
                  and attempts >= max_attempts):
                exhausted += 1
    return {"to_call": to_call, "waiting_redispatch": waiting,
            "due_redispatch": due, "exhausted": exhausted}


@app.get("/api/stats/dashboard")
def stats_dashboard(request: Request, account_id: str = "acc-001") -> dict:
    """工作台仪表盘单端点（2026-09-17）。口径见 plan Task 5；P0 全量 Python 聚合。

    修复波 T5-M2/M3（2026-09-17）：answered 只认 status==ENDED 且 disposition 不在
    排除集——FAILED+abandoned（reaper 振铃超时，从未接通）不算接通，但仍计入
    answer_rate 分母（拨出有结果）；today/answered_today=created_at ≥ 本地午夜
    的 UTC 边界（`_local_midnight_utc_boundary`）。
    """
    from .campaign import _parse_updated_at
    _gate_page(request, "calls")
    account_id = scoped_account(request, account_id)
    calls = _repo().list_calls(account_id)
    today_boundary = _local_midnight_utc_boundary()

    def _is_today(call: dict) -> bool:
        created = _parse_updated_at(call.get("created_at"))
        return created is not None and created >= today_boundary

    ended = [c for c in calls if str(c.get("status") or "") in
             (CallStatus.ENDED.value, CallStatus.FAILED.value)]
    answered = [c for c in ended
                if str(c.get("status") or "") == CallStatus.ENDED.value
                and str(c.get("disposition") or "") not in _ANSWERED_EXCLUDED]
    answered_ids = {c.get("id") for c in answered}
    buckets = {name: 0 for name, _, _ in _DURATION_BUCKETS}
    for call in answered:
        duration = int(call.get("duration_s") or 0)
        if duration <= 0:
            continue  # 时长桶只统计 duration_s>0 的通话（未接通/边角零时长不进桶）。
        bucket = _duration_bucket(duration)
        if bucket:
            buckets[bucket] += 1
    by_agent: dict[str, dict] = {}
    for call in calls:
        uid = str(call.get("created_by") or "")
        if not uid:
            continue
        slot = by_agent.setdefault(uid, {"user_id": uid, "name": uid, "calls": 0, "answered": 0})
        slot["calls"] += 1
        if call.get("id") in answered_ids:
            slot["answered"] += 1
    for user in _repo().list_users():
        slot = by_agent.get(str(user.get("id") or ""))
        if slot:
            slot["name"] = str(user.get("display_name") or user.get("username") or slot["name"])
    disposition_counts: dict[str, int] = {}
    whatsapp_counts: dict[str, int] = {}
    for call in calls:
        if d := str(call.get("disposition") or ""):
            disposition_counts[d] = disposition_counts.get(d, 0) + 1
        if w := str(call.get("whatsapp_status") or ""):
            whatsapp_counts[w] = whatsapp_counts.get(w, 0) + 1
    return {
        "concurrency": {"current": sum(1 for c in calls if str(c.get("status") or "") == CallStatus.ACTIVE.value)},
        "calls": {"today": sum(1 for c in calls if _is_today(c)),
                  "total": len(calls), "answered": len(answered),
                  "answered_today": sum(1 for c in answered if _is_today(c)),
                  "answer_rate": round(len(answered) / len(ended), 4) if ended else 0.0},
        "duration_buckets": buckets,
        "agents": sorted(by_agent.values(), key=lambda a: (-a["calls"], a["user_id"]))[:8],
        "tags": {"disposition": disposition_counts, "whatsapp": whatsapp_counts},
        "todo": _dashboard_todo(_repo()),
    }


@app.get("/api/insights")
def list_insights(request: Request) -> list[dict]:
    """全局洞察（结算蒸馏产出）。跨账号内容（GlobalInsight 无 account 维度，
    深测 P2）→ 收管理面；账号维度化留待 schema 加列（见计划尾部延后项）。"""
    require_role(request, "admin", "root")
    return _repo().list_global_insights(kind="insight")


@app.get("/api/objects/{object_id}/topics")
def list_object_topics(object_id: str, request: Request) -> list[dict]:
    """对象历史主题（结算时 Summarizer 蒸馏产出并 append 到该对象）。"""
    _gate_page(request, "objects")
    deny_cross_account(request, _repo().get_object(object_id))
    return _repo().list_object_topics(object_id)


def _iter_call_reports(call: dict) -> list[dict]:
    """P1-A 报告合并视图：主列 session_report + per-worker 历史列 session_reports_json。

    顺序=主列在前（旧读点单报告语义不变），每元素是完整 report dict；坏 JSON/
    异形元素跳过不炸（读路径不得因脏数据 500）。usage 类读点（/api/reports/usage、
    settle 的 usage_record）对数值字段逐份累加。主列与某条历史**同文**（P1-A 镜像
    写入的场合）时去重——数值累加读点否则会把同一份报告算两遍。
    """
    entries: list[dict] = []
    arr_raw = str((call or {}).get("session_reports_json") or "").strip()
    if arr_raw:
        try:
            arr = json.loads(arr_raw)
            if isinstance(arr, list):
                for entry in arr:
                    if isinstance(entry, dict) and isinstance(entry.get("report"), dict):
                        entries.append(entry["report"])
        except Exception:
            pass
    out: list[dict] = []
    raw = str((call or {}).get("session_report") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and not any(r == parsed for r in entries):
                out.append(parsed)
        except Exception:
            pass
    return out + entries


@app.post("/api/calls/{call_id}/session-report")
async def ingest_session_report(call_id: str, request: Request) -> dict:
    """Agent shutdown 上报官方 SessionReport（真实逐模型 usage + 权威 chat_history 快照）。

    存 call_sessions.session_report(JSON)；结算/报表优先吃这里的真数据，
    没有上报的旧通话才回退估算口径。

    P1-B（2026-09-17 全量 debug，同批与 P1-A 一次改到位）：写入主体收紧——报告
    是 worker 产物不是操作台输入。机器通道（auth-on 下 state.machine 的 CP token、
    或加固模式 identity=None 的裸 token 通道）与 admin/root 放行；role=user 一律
    403（否则持 calls 页键的 user JWT 可改写活跃通话报告=报表可伪造）；真 auth-off
    （双关、无身份无 token）零变化。

    P1-A（同批）：body 增可选 `worker`（B 线 interp fwd/rev 双 worker 共享同一
    call_id，各自产出一份 report；interp-fwd.log 实证后收尾方向曾整份被 409 丢）。
    worker=""（旧 A 线 agent）语义零变化——首写进主列、ended+已有 report → 409
    幽灵守卫；worker 非空走 per-worker 历史（call_sessions.session_reports_json
    JSON 数组，元素 {"worker","report","ts"}）：同 worker 重发（重试）=替换其条目、
    异 worker=追加；ended 后不同 worker 的第二份照收（合并返回 200，不再 409）。
    该份同时仅在主列仍为空时镜像进主列（backfill/旧读点向后兼容）。
    """
    _ident = current_identity(request)
    if _ident is not None and _ident.role not in ("admin", "root"):
        # 身份三态穷尽：user → 403；admin/root → 放行；identity=None（机器通道
        # state.machine / 加固模式裸 CP token / 双关 auth-off）→ 下方直通。
        raise HTTPException(status_code=403, detail="session report is worker-authored")
    _gate_page(request, "calls")
    deny_cross_account(request, _repo().get_call(call_id))
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    worker = str((payload or {}).get("worker") or "").strip()
    # C2 闸2·幽灵覆盖防护(2026-09-13,call-6bd59b40):挂断后重连产生的幽灵 job
    # 会用自己的 report 覆盖真实通话的 session_report。ended 且已有 report →
    # 409 拒绝(首个 report 在 ended 后仍收——agent 收尾顺序是先 ended 后上报,
    # 只挡「第二次覆盖」)。P1-A 起该守卫只对 worker="" 旧语义生效；worker 非空
    # 是 per-worker 历史,异 worker 在 ended 后追加照收（不 409）。
    try:
        _cur = _repo().get_call(call_id) or {}
    except Exception:  # pragma: no cover - 读取失败按旧行为放行
        _cur = {}
    if not worker:
        if (
            str(_cur.get("status") or "") == "ended"
            and str(_cur.get("session_report") or "").strip()
        ):
            _audit(
                "call.session_report_rejected",
                subject_type="call",
                subject_id=call_id,
                account_id=_cur.get("account_id", ""),
                outcome="ghost_overwrite",
                call_id=call_id,
            )
            raise HTTPException(
                status_code=409,
                detail="call ended with existing session_report — ghost overwrite rejected",
            )
        row = _repo().update_call(call_id, session_report=json.dumps(payload, ensure_ascii=False, default=str))
        if not row:
            raise HTTPException(status_code=404, detail="call not found")
        _audit("call.session_report", subject_type="call", subject_id=call_id, account_id=row.get("account_id", ""), call_id=call_id)
        return {"call_id": call_id, "stored": True}
    # ---- worker 非空：per-worker 历史 upsert（P1-A） ----
    try:
        arr = json.loads(str(_cur.get("session_reports_json") or "") or "[]")
        if not isinstance(arr, list):
            arr = []
    except Exception:
        arr = []
    entry = {"worker": worker, "report": payload, "ts": _utcnow_iso()}
    replaced = False
    for i, old in enumerate(arr):
        if isinstance(old, dict) and str(old.get("worker") or "") == worker:
            arr[i] = entry  # 同 worker 重发（重试）=替换，不累积重复条目
            replaced = True
            break
    if not replaced:
        arr.append(entry)
    fields: dict = {"session_reports_json": json.dumps(arr, ensure_ascii=False, default=str)}
    if not str(_cur.get("session_report") or "").strip():
        # 镜像仅当主列仍为空：首个 worker 顺手喂饱 backfill/旧读点，后续方向不动主列。
        fields["session_report"] = json.dumps(payload, ensure_ascii=False, default=str)
    row = _repo().update_call(call_id, **fields)
    if not row:
        raise HTTPException(status_code=404, detail="call not found")
    _audit(
        "call.session_report",
        subject_type="call",
        subject_id=call_id,
        account_id=row.get("account_id", ""),
        call_id=call_id,
        detail={"worker": worker[:64], "replaced": replaced,
                "ended_merge": str(_cur.get("status") or "") == "ended"},
    )
    return {"call_id": call_id, "stored": True, "merged": True,
            "worker": worker, "replaced": replaced}


# 重派防复活门:终态通话(或记录已删)不得重派——为死通话新建的 dispatch 无
# 回收路径(reaper 只扫 ACTIVE/PAUSED),agent 会被带进空房念开场白。
# 重派重试排程(秒):等 livekit 把死 worker 的 job 判 FAILED / dispatch 服务恢复。测试可注入。
_REDISPATCH_RETRY_SCHEDULE = (0.0, 10.0, 25.0)
# per-room 重派锁:串行化「has_active_dispatch 检查 + create_dispatch」临界区,
# 收口 TOCTOU(后进锁者复查时见到先进锁者新建的 dispatch → 跳过,并发双 create
# 坍缩为一次)。锁图按房间单调增长:每通话房间一个小锁对象,CP 单进程 4-6 路并发
# 规模下可接受;不做淘汰——锁被取走瞬间另一任务可能正持有,边界不值得。
_redispatch_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# web_logs 上报滑动窗口限速（2026-09-16 深测 P3）：/api/web_logs 曾无限速，
# 匿名/任意身份可高频刷盘（logs/web-client.log 无界增长）。600 行/分钟封顶。
# P3-A（2026-09-17 全量 debug）改 per-identity：旧版单 deque 全局共享——一个
# 身份打满 600/min 即压掉所有调用方的诊断通道；改 dict[identity]→deque，
# 身份键=user:<id>/machine/anon，容量与窗口继承原值。
_weblog_times: dict[str, deque] = {}


# P3-C（2026-09-17 全量 debug）：加固模式漏配 LIVEKIT_API_SECRET 的首次告警旗标
# （进程内一次即可——每次请求都告警会刷屏）。
_webhook_secret_warned = False


def _verify_livekit_webhook(request: Request, body: bytes) -> bool:
    """LiveKit webhook 官方验签：Authorization Bearer JWT（HS256/LIVEKIT_API_SECRET）
    + video.webhook grant + sha256(body) 摘要（2026-09-16 深测 P2）。

    P3-C：auth-on（BOK_AUTH_REQUIRED=1）下 secret 为空=配置事故——伪造
    participant_left 可触发 LiveKit 云 API 放大调用，不再 fail-open，401 拒收
    （detail 指明漏配项）；双关 auth-off 与 CP-token-only 保留放行+打点（后者
    行为被 tests/test_debug_sweep.py::test_webhook_bypasses_cp_token_gate 钉死
    ——F2 修复语义「中间件/端点不得拦无 LiveKit 联调形态的 webhook」优先）。
    """
    import hashlib

    import jwt as _pyjwt

    global _webhook_secret_warned
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    if not secret:
        if auth_required():
            if not _webhook_secret_warned:
                _webhook_secret_warned = True
                control_log.warning(
                    "webhook_secret_missing_hardened",
                    extra={"event": "webhook.secret_missing",
                           "data": {"hint": "auth-on 要求 LIVEKIT_API_SECRET，未配置前 webhook 一律 401"}},
                )
            raise HTTPException(
                status_code=401,
                detail="webhook secret not configured — LIVEKIT_API_SECRET is required when auth is hardened",
            )
        control_log.warning("webhook_unsigned_accepted", extra={"event": "webhook.unsigned"})
        return True
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return False
    try:
        # leeway=10（终审修复）：秒级时钟偏移不拒真 webhook——exp/nbf 校验保持
        # 开启（LiveKit 签发带 exp/nbf），只容忍 ±10s 漂移。
        claims = _pyjwt.decode(
            token, secret, algorithms=["HS256"], options={"verify_aud": False}, leeway=10
        )
    except _pyjwt.PyJWTError:
        return False
    if not (claims.get("video") or {}).get("webhook"):
        return False
    return claims.get("sha256") == hashlib.sha256(body).hexdigest()


@app.post("/api/webhook/livekit")
async def livekit_webhook(request: Request) -> dict:
    """LiveKit webhook：agent 崩溃补位。

    自部署的 LiveKit OSS 在 worker/job 进程死亡时**不会自动重派** agent
    （restart_policy = Cloud-only，见 docs/DEV_TOOLS.md §5）。收到 participant_left
    且 identity 是我们 agent（= agent_name：bok-voice / bok-interp-fwd / bok-interp-rev）
    → 调 AgentDispatchService.CreateDispatch 把同名 agent 重派回房（token 内 dispatch
    只在建房时生效，这是官方指定通路）。launchd KeepAlive 只拉起 worker 本体，本端点
    补「房间内 agent 缺席」这一层。本端点需在 livekit.yaml 配 webhook 指向本 CP。
    """
    # 验签（2026-09-16 深测 P2）：本端点在中间件豁免表里=匿名可达。伪造
    # participant_left 会触发最多 3 轮 LiveKit 云 API 放大调用 + _redispatch_locks
    # 无界增长。官方姿势：LiveKit server 以 LIVEKIT_API_SECRET 签 JWT（HS256，
    # video.webhook grant）并在 claim 里带 sha256(body) 摘要——双验。
    # LIVEKIT_API_SECRET 未配置：双关 auth-off（本地无 LiveKit 联调）放行并打点；
    # 加固模式属配置事故 → P3-C 起 401 拒收（详见 _verify_livekit_webhook）。
    raw = await request.body()
    if not _verify_livekit_webhook(request, raw):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    if len(_redispatch_locks) > 512:
        # 攻击者可用任意 room 名撑大锁图（模块注释自认不做淘汰）——上限修剪。
        for _k in [k for k, v in _redispatch_locks.items() if not v.locked()][:256]:
            _redispatch_locks.pop(_k, None)
    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    event = str(payload.get("event") or "")
    room_name = str(payload.get("room", {}).get("name") or "")
    participant = payload.get("participant") or {}
    identity = str(participant.get("identity") or "")
    if event != "participant_left" or not room_name:
        return {"handled": False, "reason": "not participant_left"}
    # A 线 agent 身份识别:agents SDK 真实 job 入房 identity 是 "agent-<jobid>"
    # (livekit/agents job.py:1018,服务端签发的 job token 所带,非 agent_name——
    # 2026-09-10 实机演练实证 agent-AJ_*,"bok-voice" 永不出现;保留它作兼容)。
    # B 线 interpreter 以 listen 身份(me-<room>/other-<room>,与真人同款)在场——
    # participant_left 无法区分是 agent 崩溃还是真人离开,不做自动补位(房间短命,
    # 双端可重开);me-/other- 前缀与 agent-* 天然不重叠。
    if identity != "bok-voice" and not identity.startswith("agent-"):
        return {"handled": False, "reason": "not A-line agent"}
    # kind=interpret 房的 agent-<jobid> 离房 ≠ A 线崩溃——B 线 interp worker 的 job
    # identity 同为 agent-*，挂断踢出时一样触发本 webhook；误判会往同传房补派
    # bok-voice 形成 5min 周期殭尸循环（2026-09-10 实证 redispatch 至 1 小时）。
    # B 线同传房短命、双端可重开，崩溃不自动补位。
    try:
        _wcall = _repo().get_call(room_name) or {}
    except Exception:
        _wcall = {}
    if str(_wcall.get("kind") or "") == "interpret":
        return {"handled": False, "reason": "interpret room"}

    async def _redispatch() -> None:
        # 防复活(F1):挂断链是 update_call(ENDED) → delete_room 踢出 agent →
        # 本 webhook 重派,与 _disconnect_livekit_room 的 cleanup 赛跑——cleanup
        # 先赢时 has_active=False,死通话会被新建 dispatch「复活」。每次尝试前
        # 重查通话终态(记录已删同罪),死通话直接放弃,DB 状态是挂断链的权威先序信号。
        # 防重(僵尸通话 P1):participant_left 可能连发/与 worker 自愈竞态,
        # 先查同 agent 是否已有 PENDING/RUNNING job,有则跳过——叠加两套
        # agent 会互相抢麦、双重播报。per-room 锁包住检查+创建收口 TOCTOU。
        # 重试(2026-09-10 kill-recover 实证):worker 被强杀后 (a) 其 job 在
        # livekit 侧要等断连判定才转 FAILED,participant_left 一刻常仍显示
        # RUNNING → 立即补派会被防重拦下;(b) dispatch API 本身可能 503(唯一
        # worker 已死,agent 服务不可用)。两个窗口都随时间自愈,故按 0/10/25s
        # 三次尝试:任一次补派成功即收口;「已活跃」持续到末次 = 真叠加,放弃;
        # 通话进入终态 = 挂断抢先,放弃。
        async def _attempt(delay: float, attempt: int) -> str:
            if delay:
                await asyncio.sleep(delay)
            try:
                call = _repo().get_call(room_name) or {}
            except Exception:
                call = {}
            if not call or str(call.get("status") or "") in _TERMINAL_CALL_STATUSES:
                control_log.info(
                    "redispatch_skip_active_dispatch",
                    extra={
                        "event": "dispatch.redispatch.skip",
                        "data": {
                            "room": room_name,
                            "event": event,
                            "reason": "call_ended" if call else "call_not_found",
                            "attempt": attempt,
                        },
                    },
                )
                return "terminal"
            client = _lkapi_client()
            if client is None:
                return "terminal"
            if attempt > 0:
                # 复查轮(livekit OSS 缺口,2026-09-10 演练实证):worker 死后其 job
                # 永停 JS_RUNNING(旧 dispatch 常驻"活跃"),且无 worker 时创建的
                # PENDING dispatch 不会被新注册 worker 拾取——双重死锁。先看 agent
                # 是否已回房(回房=上一轮补派成功,收口);确实不在房则清扫全部本
                # agent 的 dispatch,让本轮 create 走全新生命周期。
                from livekit.api import ListParticipantsRequest
                ps = await client.room.list_participants(ListParticipantsRequest(room=room_name))
                if any(
                    p.identity == "bok-voice" or p.identity.startswith("agent-")
                    for p in ps.participants
                ):
                    print(f"[webhook] agent back in room, recovery done (call {room_name})", flush=True)
                    return "recovered"
                for d in await client.agent_dispatch.list_dispatch(room_name=room_name):
                    if d.agent_name == "bok-voice":
                        await client.agent_dispatch.delete_dispatch(
                            dispatch_id=d.id, room_name=room_name
                        )
                        print(f"[webhook] stale dispatch {d.id} deleted (agent absent, call {room_name})", flush=True)
            try:
                async with _redispatch_locks[room_name]:
                    if await has_active_dispatch(client, room_name):
                        control_log.info(
                            "redispatch_skip_active_dispatch",
                            extra={
                                "event": "dispatch.redispatch.skip",
                                "data": {"room": room_name, "event": event, "attempt": attempt},
                            },
                        )
                        return "dup"
                    # 官方签名收请求对象(非 kwargs;kwarg 形态 TypeError,
                    # 2026-09-10 实机演练暴露——mock 测试看不见签名错配)。
                    from livekit.api import CreateAgentDispatchRequest
                    await client.agent_dispatch.create_dispatch(
                        CreateAgentDispatchRequest(agent_name="bok-voice", room=room_name)
                    )
                _audit("agent.redispatch", subject_type="call", subject_id=room_name, detail={"agent_name": "bok-voice", "event": event, "attempt": attempt})
                print(f"[webhook] redispatch created (call {room_name}, attempt {attempt})", flush=True)
                return "created"
            except Exception as exc:  # pragma: no cover - dispatch API 瞬断(唯一 worker 已死时 503)
                print(f"[webhook] redispatch attempt {attempt} failed: {exc!r} (call {room_name})", flush=True)
                return "error"
            finally:
                await client.aclose()

        outcome = "created"
        for attempt, delay in enumerate(_REDISPATCH_RETRY_SCHEDULE):
            outcome = await _attempt(delay, attempt)
            if outcome in ("terminal", "recovered", "created_dup_free"):
                break
            # created(上一轮补派)/dup/error → 进入下一轮:验证 agent 是否回房,
            # 没回房则清扫幽灵 dispatch 再补派(#57 之前 created 即收口,漏掉
            # 「无 worker 时创建的 PENDING dispatch 不被新 worker 拾取」的死局)。

    asyncio.create_task(_redispatch())
    return {"handled": True, "redispatch": identity}


@app.get("/api/reports/script-insights")
def script_insights(request: Request, account_id: str = "acc-001", limit: int = 10) -> dict:
    _gate_page(request, "reports")
    account_id = scoped_account(request, account_id)
    """话术优化分析视图（知识库=全局分析数据层）:高频问题 TOP N + 通话/轮次统计。"""
    from collections import Counter

    freq: Counter = Counter()
    for obj in _repo().list_objects(account_id):
        for t in _repo().list_object_topics(str(obj.get("id") or "")):
            topic = str((t or {}).get("topic") or "").strip()
            if topic:
                freq[topic] += 1
    calls = _repo().list_calls(account_id, "")
    stats = _repo().turn_stats()
    by_status: Counter = Counter(str(c.get("status") or "") for c in calls)
    total_turns = sum(st.get("turns", 0) for st in stats.values())
    return {
        "account_id": account_id,
        "calls": len(calls),
        "calls_by_status": dict(by_status),
        "turns_total": total_turns,
        "top_issues": [
            {"topic": topic, "mentions": n}
            for topic, n in freq.most_common(limit)
        ],
        "distill_docs": len(_repo().list_calls(account_id, "")),
    }


@app.get("/api/reports/distill-health")
def distill_health(request: Request, account_id: str = "acc-001", limit: int = 10) -> dict:
    _gate_page(request, "reports")
    account_id = scoped_account(request, account_id)
    """蒸馏健康度（审计事件 settle.distill_empty 由 #23 铺设,本端点出报表口径）。"""
    calls = _repo().list_calls(account_id, "")
    settled = [c for c in calls if c.get("status") == "ended"]
    empty_events = _repo().list_audit_events(
        account_id=account_id, action="settle.distill_empty", limit=200
    )
    return {
        "account_id": account_id,
        "calls_total": len(calls),
        "settled": len(settled),
        "distill_empty_events": len(empty_events),
        "recent_empty": [
            {"call_id": e.get("call_id", ""), "ts": e.get("ts", "")}
            for e in empty_events[:limit]
        ],
    }


@app.get("/api/objects/{object_id}/digest")
def get_object_digest(object_id: str, request: Request) -> dict:
    _gate_page(request, "objects")
    obj = deny_cross_account(request, _repo().get_object(object_id))
    if not obj:
        raise HTTPException(404, "object not found")
    return {"object_id": object_id, "digest": obj.get("digest", "")}


@app.get("/api/reports/usage")
def reports_usage(request: Request, account_id: str = "acc-001") -> dict:
    _gate_page(request, "reports")
    account_id = scoped_account(request, account_id)
    calls = _repo().list_calls(account_id, "")
    # 真实用量优先：官方 SessionReport 的逐模型 input/output tokens；P1-A 起
    # 一通通话可能有多份报告（B 线 fwd/rev 双 worker 各一份，_iter_call_reports
    # 合并视图）——数值逐份累加；没有上报的旧通话才回退「轮次数」估算（口径见
    # 字段名后缀）。
    llm_tokens = 0
    estimated_calls = 0
    for c in calls:
        tokens = 0
        for report in _iter_call_reports(c):
            for u in report.get("usage") or []:
                if u.get("type") == "llm_usage":
                    tokens += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
        if tokens:
            llm_tokens += tokens
        else:
            estimated_calls += 1
            llm_tokens += len(_repo().get_turns(c["id"]))
    return {
        "asr_calls": len(calls),
        "llm_tokens": llm_tokens,
        "llm_tokens_estimated_calls": estimated_calls,
        "tts_calls": len(calls),
        "vad_calls": len(calls),
    }


@app.get("/api/audit")
def list_audit(request: Request, account_id: str = "", action: str = "", call_id: str = "", limit: int = 200) -> list[dict]:
    # 审计=管理面（主管操作留痕的查看口），话务员不可见。
    require_role(request, "admin", "root")
    limit = max(1, min(int(limit or 200), 1000))  # 巨值 limit 曾直透 SQL（全表进内存）
    account_id = scoped_account(request, account_id)
    repo = _repo()
    if hasattr(repo, "list_audit_events"):
        return repo.list_audit_events(account_id=account_id, action=action, call_id=call_id, limit=limit)
    return []


@app.get("/api/setup")
def setup_status(request: Request) -> dict:
    """Report first-run model readiness for the desktop setup wizard."""
    require_role(request, "admin", "root")
    try:
        import subprocess

        root = Path(os.environ.get("BOK_ROOT", "."))
        out = subprocess.run(
            [sys.executable, str(root / "tools" / "bok.py"), "setup", "status"],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        return _parse_setup(out.stdout)
    except Exception as exc:
        return {"ready": False, "models": [], "error": str(exc)}


@app.post("/api/setup/download")
def setup_download(request: Request) -> dict:
    """Trigger model download (best-effort; UI polls /api/setup for progress)."""
    require_role(request, "admin", "root")
    try:
        import subprocess

        root = Path(os.environ.get("BOK_ROOT", "."))
        subprocess.Popen(
            [sys.executable, str(root / "tools" / "bok.py"), "setup", "download"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(root),
        )
        _audit("setup.download", subject_type="global_settings", subject_id="models")
        return {"started": True}
    except Exception as exc:
        return {"started": False, "error": str(exc)}


def _parse_setup(stdout: str) -> dict:
    try:
        return json.loads(stdout)
    except Exception:
        return {"ready": False, "models": [], "error": "unable to parse setup status"}


@app.post("/api/supervisor/{call_id}/join")
def supervisor_join(call_id: str, request: Request) -> dict:
    """主管进房:校验通话存在并直接签发 supervisor token(官方 TokenSource 契约)。

    以前只回 role 不回 token(与 CONTRACTS.md「主管进房 token」自相矛盾);现在
    与 /api/token 同一条签发链路(identity=supervisor-<room>,挂 bok.role 属性,
    A 线 dispatch 由 token 端点统一处理)。
    """
    require_role(request, "admin", "root")
    deny_cross_account(request, _repo().get_call(call_id))
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    issued = token(TokenRequest(call_id=call_id, role="supervisor"), request)
    _audit("supervisor.join", subject_type="call", subject_id=call_id,
           account_id=str(call.get("account_id") or ""), call_id=call_id)
    return {
        "call_id": call_id,
        "status": call.get("status", "active"),
        "role": Role.SUPERVISOR.value,
        "serverUrl": issued.serverUrl,
        "participantToken": issued.participantToken,
    }


@app.post("/api/supervisor/{call_id}/listen")
def supervisor_listen(call_id: str, request: Request) -> dict:
    """主管静默旁听：签发只订阅 token（can_publish 全关）+ 留审计。

    与 `/join` 的区别：join 是通用主管身份（可发布），listen 是旁听专线——
    从 token 层保证「只听不说」，且不翻通话状态；被听方无任何提示（产品拍板），
    但每次旁听都在审计留痕（此处记 start，前端结束回执 stop 补时长）。
    """
    require_role(request, "admin", "root")
    deny_cross_account(request, _repo().get_call(call_id))
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    if str(call.get("status") or "") in _TERMINAL_CALL_STATUSES:
        raise HTTPException(409, "call has ended")
    account_id = str(call.get("account_id") or "acc-001")
    issued = token(TokenRequest(call_id=call_id, role="supervisor",
                                purpose="listen", account_id=account_id), request)
    _audit("supervisor.listen.start", subject_type="call", subject_id=call_id,
           account_id=account_id, call_id=call_id, detail={"purpose": "listen"})
    return {
        "call_id": call_id,
        "status": call.get("status", "active"),
        "serverUrl": issued.serverUrl,
        "participantToken": issued.participantToken,
    }


@app.post("/api/supervisor/{call_id}/listen/stop")
def supervisor_listen_stop(call_id: str, req: ListenStopRequest, request: Request) -> dict:
    """旁听结束回执：补一条带时长的审计（纯留痕，不改通话状态）。"""
    require_role(request, "admin", "root")
    # 与 join/listen/pause/resume 同款越权口径:404 不泄露他账号通话存在性
    # (2026-09-17 全量 debug F8——此前可对他账号 call_id 注入审计行)。
    deny_cross_account(request, _repo().get_call(call_id))
    call = _repo().get_call(call_id) or {}
    _audit("supervisor.listen.stop", subject_type="call", subject_id=call_id,
           account_id=str(call.get("account_id") or ""), call_id=call_id,
           detail={"seconds": max(0, int(req.seconds or 0))})
    return {"call_id": call_id, "recorded": True}


@app.post("/api/web_logs")
async def web_logs(payload: dict, request: Request) -> dict:
    """web 客户端(浏览器侧)关键事件落盘——同传控制台的设备枚举/自动分配/sink 路由/
    麦克风开关等决策只发生在浏览器里,服务端日志全然看不见(2026-09-12 同传输出
    路由排障多轮全靠排除法实证)。JSON 行追加 logs/web-client.log,与 agent.log 同
    目录;行限长防刷爆。上报失败静默(诊断通道永不影响功能)。"""
    from datetime import datetime, timezone

    import time as _t

    # P3-A：限速窗 per-identity（user:<id>/machine/anon）——web 端诊断保留 user
    # 可写，但一个身份打满窗口不再挤占其他调用方额度。
    _wl_ident = current_identity(request)
    _wl_key = (
        f"user:{_wl_ident.user_id}" if _wl_ident is not None
        else "machine" if getattr(request.state, "machine", False)
        else "anon"
    )
    now = _t.time()
    dq = _weblog_times.setdefault(_wl_key, deque(maxlen=600))
    if len(dq) >= 600 and now - dq[0] < 60:
        return {"ok": False, "reason": "rate_limited"}  # 600 行/分钟上限,防刷盘
    dq.append(now)

    event = str(payload.get("event") or "")[:80]
    if not event:
        return {"ok": False}
    call_id = str(payload.get("call_id") or "")[:64]
    try:
        data = json.dumps(payload.get("data"), ensure_ascii=False)[:2000]
    except Exception:
        data = "??"
    line = json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(), "call_id": call_id, "event": event, "data": data},
        ensure_ascii=False,
    )
    try:
        log_dir = Path(os.environ.get("BOK_APP_DATA", str(Path.home() / "Library" / "Application Support" / "BokVoice"))) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "web-client.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # pragma: no cover - 诊断通道失败不阻功能
        print(f"[web_logs] write failed: {exc!r}", flush=True)
        return {"ok": False}
    return {"ok": True}


@app.post("/api/supervisor/{call_id}/pause-agent")
def pause_agent(call_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    deny_cross_account(request, _repo().get_call(call_id))
    call = _repo().update_call(call_id, status=CallStatus.PAUSED.value)
    if not call:
        raise HTTPException(404, "call not found")
    _audit("supervisor.pause", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id)
    return {"call_id": call_id, "action": "pause-agent", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/resume-agent")
def resume_agent(call_id: str, request: Request) -> dict:
    """恢复 AI 自动应答：解除人工接管并把通话置回 active（agent 轮询到后恢复）。"""
    require_role(request, "admin", "root")
    deny_cross_account(request, _repo().get_call(call_id))
    existing = _repo().get_call(call_id)
    if not existing:
        raise HTTPException(404, "call not found")
    # 恢复 ACTIVE 不覆盖 started_at（首转 ACTIVE 时刻口径）；从未接过的边角
    # （ringing 直接被 pause/resume）就地补起点，避免 duration 永远 0。
    call = _repo().update_call(call_id, escalated_to_human=False,
                               started_at=existing.get("started_at") or _utcnow_naive(),
                               status=CallStatus.ACTIVE.value)
    if not call:
        raise HTTPException(404, "call not found")
    _audit("supervisor.resume", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id)
    return {"call_id": call_id, "action": "resume-agent", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/takeover")
def takeover(call_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    deny_cross_account(request, _repo().get_call(call_id))
    # 人工接手=协助面终态:assist_status 顺手置 done(W4-T1)。
    call = _repo().update_call(call_id, escalated_to_human=True, status=CallStatus.PAUSED.value,
                               assist_status="done")
    if not call:
        raise HTTPException(404, "call not found")
    _audit("supervisor.takeover", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id)
    return {"call_id": call_id, "action": "takeover", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/transfer")
async def transfer(call_id: str, request: Request) -> dict:
    require_role(request, "admin", "root")
    deny_cross_account(request, _repo().get_call(call_id))
    existing = _repo().get_call(call_id)
    if not existing:
        raise HTTPException(404, "call not found")
    call = _repo().update_call(call_id, escalated_to_human=True, disposition="transferred",
                               status=CallStatus.ENDED.value, **_call_end_fields(existing))
    if not call:
        raise HTTPException(404, "call not found")
    _disconnect_room_background(call_id)
    _audit("supervisor.transfer", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id)
    return {"call_id": call_id, "action": "transfer", "status": call["status"], "disconnected": True}


@app.post("/api/supervisor/{call_id}/transfer-sip")
async def transfer_sip(call_id: str, req: TransferSipRequest, request: Request) -> dict:
    """SIP REFER 试点骨架（W5-T1）：把通话电话腿直接转给坐席手机号/SIP URI。

    trunk 的 REFER 支持未验证——settings.sip.mode=="mock"（默认）一律短路返回
    {"mocked": True}（默认安全，不触 LiveKit）；真路径只在显式 real 档执行，
    姿势照 trunk register 先例（一次性客户端 finally aclose、失败 502）。
    participant_identity 按 LiveKit SIP 参与者命名约定取 `sip-{contact_phone}`
    （call 行无号码 400——mock 档同样先验，语义不随档位漂移）。
    """
    require_role(request, "admin", "root")
    deny_cross_account(request, _repo().get_call(call_id))
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    transfer_to = (req.transfer_to or "").strip()
    if not transfer_to:
        raise HTTPException(400, "transfer_to 必填")
    contact_phone = str(call.get("contact_phone") or "").strip()
    if not contact_phone:
        raise HTTPException(400, "该通话无 contact_phone，无法定位 SIP 参与者")
    sip_cfg = (_repo().get_settings() or {}).get("sip") or {}
    mocked = str(sip_cfg.get("mode") or "mock") != "real"
    if not mocked:
        client = _lkapi_client()
        if client is None:
            raise HTTPException(502, "LiveKit 凭据未配置——livekit-sip 未部署或不可达")
        from livekit.api import TransferSIPParticipantRequest

        try:
            await client.sip.transfer_sip_participant(
                TransferSIPParticipantRequest(
                    room_name=call_id,
                    participant_identity=f"sip-{contact_phone}",
                    transfer_to=transfer_to,
                )
            )
        except Exception as exc:
            raise HTTPException(502, f"SIP transfer 失败——livekit-sip 未部署或不可达: {exc}") from exc
        finally:
            await client.aclose()  # 一次性客户端（自带 aiohttp session）必须关
    _audit("supervisor.transfer_sip", subject_type="call", subject_id=call_id,
           account_id=str(call.get("account_id") or ""), call_id=call_id,
           detail={"transfer_to": transfer_to, "mocked": mocked})
    return {"call_id": call_id, "mocked": mocked, "transfer_to": transfer_to}


@app.post("/api/supervisor/{call_id}/end")
async def supervisor_end(call_id: str, request: Request, disposition: str = "declined",
                         intent_code: str = "") -> dict:
    """AI 收尾后主动结束通话:置 ENDED 并断房。

    disposition=declined(客户明确拒绝/告别,默认)| no_response(沉默心跳两次无回应)。
    intent_code=W4-T2 意向规则引擎命中码(agent 挂断评估回写,可选;空=未命中不落列)。
    agent 讲完一句礼貌再见后调用;结算由 agent 侧 _on_close 幂等触发,这里只负责
    归档 disposition + 踢出房间。房间不存在/服务不可用不阻塞(DB 已置 ENDED)。
    """
    # agent 收线走机器通道直通；人工触发时按管理面闸。
    ident = current_identity(request)
    if ident is not None and ident.role not in ("admin", "root"):
        raise HTTPException(status_code=403, detail="forbidden")
    deny_cross_account(request, _repo().get_call(call_id))
    disposition = (disposition or "declined").strip()[:64] or "declined"
    intent_code = (intent_code or "").strip()[:32]
    existing = _repo().get_call(call_id)
    if not existing:
        raise HTTPException(404, "call not found")
    end_fields: dict = dict(_call_end_fields(existing))
    if intent_code:
        end_fields["intent_code"] = intent_code  # 非空才落列(''=零写入,方言/历史行零扰动)
    call = _repo().update_call(call_id, escalated_to_human=False, disposition=disposition,
                               status=CallStatus.ENDED.value, **end_fields)
    if not call:
        raise HTTPException(404, "call not found")
    _disconnect_room_background(call_id)
    end_detail: dict = {"disposition": disposition}
    if intent_code:
        end_detail["intent_code"] = intent_code
    _audit("supervisor.end", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id, detail=end_detail)
    return {"call_id": call_id, "action": "end", "status": call["status"], "disconnected": True}


# 管理台静态托管（云端形态）：目录在才挂载，本地开发形态零变化。
# 必须**放在全部 API 路由之后**——FastAPI 按注册顺序匹配，路由先于挂载命中，
# `/api/*` 与 `/health` 不受影响；目录缺失(本地仓库/测试)整段跳过，连 import 都不发生。
_web_static_dir = Path(os.environ.get("BOK_WEB_STATIC_DIR", "/app/web-out"))


def _write_web_runtime_config(public_url: str, web_root: Path) -> Path | None:
    """云托管管理台的运行时 CP 地址注入（2026-09-19 Docker 模拟拓扑实测缺口）。

    静态台 apiBase() 第一优先 window.__BOK_CONFIG__.cpUrl（节点托管形态由
    node_agent 写同一文件），未注入则回落构建期烤死的 127.0.0.1:8000——云端口
    形态（如 prod 18010）下该回落指向访问者本机，登录必 401 弹回、控制台不可用。
    BOK_CP_PUBLIC_URL 设置时幂等写 runtime-config.js；未设置不写（节点托管自己
    写，双写会互相踩）。URL 只收 http(s) 且禁引号/尖括号/反斜杠（进 JS 字符串
    与 HTML 上下文）；写失败（只读挂载等）只警告绝不阻启动。
    """
    url = (public_url or "").strip().rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")) or any(
        ch in url for ch in ("'", '"', "<", ">", "\\")
    ):
        print(f"[web-config] BOK_CP_PUBLIC_URL 非法（须 http(s) 且无特殊字符），跳过注入: {url[:60]!r}")
        return None
    target = web_root / "runtime-config.js"
    payload = "window.__BOK_CONFIG__ = " + json.dumps({"cpUrl": url}) + ";\n"
    try:
        target.write_text(payload, encoding="utf-8")
    except OSError as exc:
        print(f"[web-config] runtime-config.js 写入失败（控制台将回落构建期地址）: {exc}")
        return None
    return target


if _web_static_dir.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_web_static_dir), html=True), name="web")
