from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import uuid
import wave
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

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

from .deps import build_engine, build_repository, build_session_factory
from .dispatch_utils import cleanup_dispatch, has_active_dispatch
from .nodes_store import HEARTBEAT_INTERVAL_S, NodeStore
from .pregen import persona_pregen_status
from .schemas import (
    CreateCallRequest,
    CreateObjectRequest,
    ImportRequest,
    DialResultRequest,
    PersonaRequest,
    QaEntryCreate,
    QaEntryPatch,
    RosterClaimRequest,
    RosterHandledRequest,
    TemplateRequest,
    UpdateTemplateRequest,
    UpdateObjectRequest,
    UpdatePersonaRequest,
    SettingsRequest,
    TokenRequest,
    TokenResponse,
    WhatsAppCaptureRequest,
    WhatsAppHandledRequest,
)

# 通话终态集合：模块级常量（dial-result 端点与下方重派段共用）。
# 只含真终态——CallStatus.FAILED 是通话级失败终态(与拨号失败 disposition="failed" 同名不同义)。
_TERMINAL_CALL_STATUSES = (CallStatus.ENDED.value, CallStatus.FAILED.value)


app = FastAPI(title="Bok Voice Control Plane", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CorrelationMiddleware)


@app.middleware("http")
async def optional_bearer_auth(request: Request, call_next):
    """可选 Bearer 鉴权（R2）：BOK_CP_TOKEN 未设=全放行（本机单用户形态零变化）。

    设置后除 /health 与 /api/nodes/heartbeat 外全部端点要求
    `Authorization: Bearer <BOK_CP_TOKEN>`——暴露到局域网/云之前必须设置；
    agent(worker env)与 web 需同步带同值。心跳豁免：该端点用注册时签发的
    node_token 自鉴权（sha256 比对，与 CP token 不同源），CP 门禁会把它拦死
    令节点注册表失联；register/list 属管理操作，仍在门禁内。
    """
    expected = os.environ.get("BOK_CP_TOKEN", "").strip()
    if expected and request.url.path not in ("/health", "/api/nodes/heartbeat"):
        if request.headers.get("authorization", "") != f"Bearer {expected}":
            return Response(status_code=401, content=b'{"detail":"unauthorized"}',
                             media_type="application/json")
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


@app.on_event("startup")
def _startup() -> None:
    configure_logging(level=os.environ.get("BOK_LOG_LEVEL", "INFO"))
    engine = build_engine()
    app.state.repo = build_repository(engine)
    app.state.node_store = NodeStore(engine)  # 与 repo 同一 engine；None → 内存双模
    app.state.session_factory = build_session_factory(engine)
    app.state.lk_key = os.environ.get("LIVEKIT_API_KEY", "")
    app.state.lk_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    app.state.lk_url = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
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


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "bok-voice-control-plane"}


@app.get("/api/settings")
def get_settings(internal: bool = False) -> dict:
    raw = _repo().get_settings()
    if internal:
        return raw
    masked = {k: _mask_secrets(v) for k, v in raw.items() if k != "policy"}
    masked["policy"] = raw.get("policy", "offline_first")
    return masked


@app.put("/api/settings")
def put_settings(req: SettingsRequest) -> dict:
    existing = _repo().get_settings()
    new_values = {
        "asr": req.asr.model_dump(),
        "llm": req.llm.model_dump(),
        "tts": req.tts.model_dump(),
        "vad": req.vad.model_dump(),
        "sip": req.sip.model_dump(),
        "policy": req.policy,
    }
    secret_keys = {"api_key", "access_token", "token", "auth_password"}
    for kind in ("asr", "llm", "tts", "vad", "sip"):
        old = existing.get(kind, {})
        new = new_values[kind]
        for key in secret_keys:
            if not new.get(key) and old.get(key):
                new[key] = old[key]
        new_values[kind] = new
    raw = new_values
    saved = _repo().save_settings(raw)
    _audit("settings.save", subject_type="global_settings", subject_id="global", detail={"llm_provider": raw.get("llm", {}).get("provider", "")})
    masked = {k: _mask_secrets(v) for k, v in saved.items() if k != "policy"}
    masked["policy"] = saved.get("policy", "offline_first")
    return masked


def _mask_secrets(config: dict) -> dict:
    out = dict(config)
    secret_keys = {"api_key", "access_token", "token", "auth_password"}
    for key in secret_keys:
        if out.get(key):
            out[key] = ""
            out[f"has_{key}"] = True
    return out


@app.get("/api/asr/health")
async def asr_health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{_qwen3_asr_url()}/health")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/tts/health")
async def tts_health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{_qwen3_tts_url()}/health")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/tts/speakers")
async def tts_speakers() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_qwen3_tts_url()}/v1/speakers")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/tts/voices")
async def tts_voices() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_qwen3_tts_url()}/v1/voices")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/tts/voices/{voice_id}")
async def tts_delete_voice(voice_id: str) -> dict:
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
    file: UploadFile = File(...),
    voice_id: str = Form(...),
    ref_text: str = Form(...),
    language: str = Form("zh"),
) -> dict:
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


@app.get("/api/tts/filler-preview")
def tts_filler_preview(lang: str = "zh", i: int = 0) -> Response:
    """垫话资产试听（2026-09-11 症状④）：直接吐源码 wav（随包分发,零云调用）。

    i=池内索引(取模轮换),web 端随机传即「换一句试听」。浏览器按 wav 头原生
    播放=正确速率;房间内 48k 混音器错配是 agent 播放路径问题,与此端点无关。"""
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
async def tts_preview(payload: dict) -> Response:
    """试听一段 TTS。provider=qwen3_tts 走本地 sidecar；provider=minimax 走云端 MiniMax
    （voice 是 MiniMax 音色 ID，如 Cantonese_Male_news_anchor_vv2）。返回 WAV。"""
    provider = str(payload.get("provider") or "qwen3_tts").lower()
    sample_rate = int(payload.get("sample_rate") or 24000)
    text = str(payload.get("text") or "")
    voice = str(payload.get("voice") or "")
    language = str(payload.get("language") or "zh")
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


@app.post("/api/token", response_model=TokenResponse, status_code=201)
def token(req: TokenRequest) -> TokenResponse:
    """签发参与者 token——LiveKit 官方 TokenSource endpoint 契约。

    请求体兼容两种形态:官方 TokenSourceRequest(snake_case: room_name /
    participant_identity,由 TokenSource.endpoint / 任意官方 SDK 直发)与本项目
    业务字段(call_id/role)。响应即官方 TokenSourceResponse({serverUrl,
    participantToken}),任何按标准实现的客户端(playground/Swift/Flutter…)可直接消费。
    """
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
    at = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(req.participant_name or name)
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True, can_publish_data=True))
        .with_ttl(datetime.timedelta(seconds=3600))
    )
    if req.participant_metadata:
        at = at.with_metadata(req.participant_metadata)
    # 业务维度放 participant attributes(官方机制):agent/前端按属性判定角色,
    # 替代对 identity 前缀的字符串嗅探;SIP 接入时同通道补 bok.* 属性。
    at = at.with_attributes({"bok.role": role, "bok.account_id": req.account_id})

    _call: dict = {}
    try:
        _call = _repo().get_call(room) or {}
    except Exception:
        _call = {}
    kind = str(_call.get("kind") or "")

    # 同传房间:我方端是创建者,token 里挂 RoomConfiguration 显式分发两个方向的
    # interpreter agent(RoomAgentDispatch 只在首个参与者建房时生效,所以只挂 me 端)。
    # metadata 带精确 identity(CP 已知房间名,不再让 agent 拼前缀)与语言对。
    if kind == "interpret" and role == "me":
        src = (_call.get("language") or "zh").strip() or "zh"
        tgt = (_call.get("target_lang") or "en").strip() or "en"
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
                        }),
                    ),
                    RoomAgentDispatch(
                        agent_name="bok-interp-rev",
                        metadata=json.dumps({
                            "listen_identity": f"other-{room}",
                            "deliver_identity": f"me-{room}",
                            "source_lang": tgt,
                            "target_lang": src,
                        }),
                    ),
                ]
            )
        )
    elif kind != "interpret" and role in ("operator", "supervisor"):
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
    if req.call_id:
        try:
            # 终态守卫（2026-09-11 同传审计 P0）：断线重连的客户端在 hangup 后再取
            # token（call-a9511563 实证 hangup 200 后 +18ms 一发），无条件写 ACTIVE
            # 会把 ENDED/FAILED 复活——最后一写者胜令「挂断不结算」成立。
            cur = _repo().get_call(req.call_id) or {}
            if str(cur.get("status") or "") not in _TERMINAL_CALL_STATUSES:
                _repo().update_call(req.call_id, status=CallStatus.ACTIVE.value)
        except Exception:
            pass
    _audit("token.issue", subject_type="call", subject_id=req.call_id or "",
           account_id=req.account_id, call_id=req.call_id or "", detail={"role": req.role})
    return TokenResponse(serverUrl=url, participantToken=participant_token)


@app.post("/api/calls")
def create_call(req: CreateCallRequest) -> dict:
    return _create_call_in(_repo(), req)


def _create_call_in(repo, req: CreateCallRequest) -> dict:
    """建通话（会话清单装配 + 审计）；repo 由调用方给出（端点= `_repo()`）。

    抽成函数便于 campaign 循环在**注入的 repo** 上建通话（campaign_tick 的 repo
    参数与 app.state 可不同源，单测注入内存仓时不能走 `_repo()`）。
    """
    # 会话清单：读取全局策略(offline_first/cloud_first)与已配置 provider，
    # 并把对象绑定的模板快照到 call（审计「这场用了哪版话术」）。
    settings = repo.get_settings()
    policy = (settings or {}).get("policy") or "offline_first"
    providers = _effective_providers(settings or {})
    template_id = ""
    if req.object_id:
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
    )
    call = repo.create_call(manifest)
    _audit("call.create", subject_type="call", subject_id=call.get("id", ""),
           account_id=req.account_id, call_id=call.get("id", ""),
           detail={"mode": req.mode, "kind": req.kind, "language": req.language, "template_id": template_id})
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
def list_calls(account_id: str = "acc-001", status: str = "") -> list[dict]:
    calls = _repo().list_calls(account_id, status)
    stats = _repo().turn_stats()
    for c in calls:
        st = stats.get(c.get("id") or "", {})
        c["turn_count"] = st.get("turns", 0)
        c["avg_latency_ms"] = st.get("avg_latency_ms", 0)
    return calls


@app.get("/api/calls/{call_id}")
def get_call(call_id: str) -> dict:
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    return call


@app.delete("/api/calls/{call_id}")
def delete_call(call_id: str) -> dict:
    if not _repo().delete_call(call_id):
        raise HTTPException(404, "call not found")
    _audit("call.delete", subject_type="call", subject_id=call_id, detail={})
    return {"deleted": True, "call_id": call_id}


@app.delete("/api/calls")
def clear_ended_calls(account_id: str = "acc-001") -> dict:
    """清空该账号下已结束(ended)的通话历史。活跃/进行中的通话不删。"""
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
            _repo().update_call(c["id"], status=CallStatus.FAILED.value, disposition="abandoned")
            out["failed"] += 1
    for st in (CallStatus.ACTIVE.value, CallStatus.PAUSED.value):
        for c in _repo().list_calls("", status=st):
            if await _room_has_participants(c["id"]):
                continue
            _repo().update_call(c["id"], status=CallStatus.ENDED.value, disposition="abandoned")
            out["ended"] += 1
            try:
                await settle(c["id"])  # 幂等:已结算直接 existing 短路
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
            await settle(c["id"])
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
async def hangup(call_id: str) -> dict:
    call = _repo().update_call(call_id, status=CallStatus.ENDED.value)
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
def report_whatsapp(call_id: str, req: WhatsAppCaptureRequest) -> dict:
    """Agent 偵測到客戶俾 WhatsApp。number 有值 → captured(客戶讀出自己號碼);
    空 → offered(客戶應承加專員,未俾號碼)。升級規則:offered→captured 容許、
    captured 唔覆寫、handled 後唔再降級(避免專員已對接又彈返出嚟)。
    """
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


@app.post("/api/calls/{call_id}/dial-result")
def report_dial_result(call_id: str, req: DialResultRequest) -> dict:
    """Agent 外呼拨号结果上报:answered→ACTIVE;三失败态→ENDED+disposition。

    幂等：已终态(ended/failed)的通话直接原样返回，不复活也不改写 disposition
    （重派/重复上报时 4B 侧时序抖动不会把已收线的通话抬回 ACTIVE）。
    status 空/未知同样 no-op（仍记审计，便于排查上游漏配）。
    """
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    if str(call.get("status") or "") in _TERMINAL_CALL_STATUSES:
        return call
    status = str(req.status or "").strip()
    if status == "answered":
        updated = _repo().update_call(call_id, status=CallStatus.ACTIVE.value) or call
    elif status in ("no_answer", "rejected", "failed"):
        updated = _repo().update_call(call_id, status=CallStatus.ENDED.value,
                                      disposition=status) or call
    else:
        updated = call
    _audit("call.dial_result", subject_type="call", subject_id=call_id,
           account_id=str(call.get("account_id", "acc-001")),
           detail={"status": status, "detail": (req.detail or "")[:120]})
    return updated


@app.post("/api/calls/{call_id}/whatsapp/handled")
def mark_whatsapp_handled(call_id: str, req: WhatsAppHandledRequest) -> dict:
    """專員喺操作台標記已對接 → status=handled,爆閃停止(AI 通話不受影響)。"""
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
def list_roster(account_id: str = "acc-001", status: str = "", channel: str = "") -> list[dict]:
    """名册认领池列表；status/channel 空=不过滤。"""
    return _repo().list_roster(account_id, status=status, channel=channel)


@app.post("/api/roster/{entry_id}/claim")
def roster_claim(entry_id: str, req: RosterClaimRequest) -> dict:
    """认领名册条目：status=claimed + claimed_by/claimed_at（naive UTC，与读侧 ISO 对齐）。"""
    entry = _repo().update_roster_entry(
        entry_id, status="claimed", claimed_by=req.claimed_by,
        claimed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    if not entry:
        raise HTTPException(404, "roster entry not found")
    _audit("roster.claim", subject_type="roster", subject_id=entry_id,
           account_id=req.claimed_by, detail={"claimed_by": req.claimed_by})
    return entry


@app.post("/api/roster/{entry_id}/unclaim")
def roster_unclaim(entry_id: str) -> dict:
    """释放认领：status=unclaimed、claimed_by 清空。

    claimed_at 必须传空串（repo 契约 None=不修改、""=清空）——空串在双后端
    都映射回「未认领」的读侧契约（SQL 存 NULL / InMemory 存 ""，读侧都渲染 ""）。
    """
    entry = _repo().update_roster_entry(entry_id, status="unclaimed", claimed_by="", claimed_at="")
    if not entry:
        raise HTTPException(404, "roster entry not found")
    _audit("roster.unclaim", subject_type="roster", subject_id=entry_id,
           account_id=str(entry.get("account_id") or ""))
    return entry


@app.post("/api/roster/{entry_id}/handled")
def roster_handled(entry_id: str, req: RosterHandledRequest) -> dict:
    """操作台标「已对接」/撤销，联动来源通话 whatsapp_status（与通话内横幅同源语义）。

    true → 名册 handled + 通话 whatsapp_status=handled（爆闪停止）；
    false → 名册回 unclaimed，通话按有无号码回 captured/offered。
    来源通话缺失/已删除不阻塞名册状态变更。
    """
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


class MockCalleeRequest(BaseModel):
    room: str
    number: str
    identity: str = ""
    scenario: str = "answer"  # answer | no_answer | reject | hangup_mid
    language: str = "cantonese"
    script: list[str] = []
    ring_delay_s: float = 3.0
    ringing_window_s: float = 35.0


@app.post("/api/sip/mock/callee")
def spawn_mock_callee(req: MockCalleeRequest) -> dict:
    """模拟联调档:派生 mock 客户子进程(同 pregen detached 姿势,失败不阻拨号主链)。

    真语音被叫——子进程进房后按剧本(四型)TTS 轮播客户话音,agent 侧
    dialer._dial_mock 靠 participant identity 认它。无 LiveKit 凭据=404
    (与 /api/token 同语义:缺凭据不静默回退)。
    """
    import subprocess

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
    ]
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
    _audit("sip.mock_callee_spawn", subject_type="room", subject_id=req.room,
           account_id="acc-001",
           detail={"scenario": req.scenario, "pid": proc.pid, "identity": identity})
    return {"ok": True, "pid": proc.pid, "identity": identity}


class NodeRegisterRequest(BaseModel):
    name: str = ""
    platform: str = ""
    org_id: str = ""
    version: str = ""


class NodeHeartbeatRequest(BaseModel):
    metrics: dict = {}


@app.post("/api/nodes/register")
def register_node(req: NodeRegisterRequest) -> dict:
    """节点注册（spec §4.2）：签发 node_token，明文只在本次响应出现一次。"""
    node_id, token = _node_store().register(
        name=req.name, platform=req.platform, org_id=req.org_id, version=req.version
    )
    return {"node_id": node_id, "node_token": token, "heartbeat_interval_s": HEARTBEAT_INTERVAL_S}


@app.post("/api/nodes/heartbeat")
def node_heartbeat(req: NodeHeartbeatRequest, authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not _node_store().heartbeat(token, req.metrics):
        raise HTTPException(401, "unknown node token")
    return {"ok": True, "commands": []}


@app.get("/api/nodes")
def list_nodes() -> list[dict]:
    return _node_store().list_nodes()


@app.get("/api/calls/{call_id}/settlement")
def get_settlement(call_id: str) -> dict:
    settlement = _repo().get_settlement(call_id)
    if not settlement:
        raise HTTPException(404, "settlement not found")
    return settlement


@app.get("/api/calls/{call_id}/turns")
def get_turns(call_id: str) -> list[dict]:
    return [turn.__dict__ for turn in _repo().get_turns(call_id)]


@app.get("/api/calls/{call_id}/metrics")
def get_call_metrics(call_id: str) -> dict:
    """每通通话延迟档案:p50/p95 latency_ms + 轮数/语言分布（审计闭环 T3）。"""
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


def _write_settlement_docs(call: dict, turns: list[dict], result: dict) -> None:
    """把通话转写与结算文档真实落盘到 vault（与 settlement 声明的 doc path 一致）。
    路径：accounts/{account}/objects/{object}/calls/{call}/transcript.md(.settlement.md)。
    失败只告警，不阻塞结算主流程。"""
    account_id = call.get("account_id") or "acc-001"
    object_id = call.get("object_id") or "unknown"
    call_id = call.get("id") or ""
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
        account_id = call.get("account_id") or "acc-001"
        object_id = call.get("object_id") or "unknown"
        call_id = call.get("id") or ""
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
async def settle(call_id: str) -> dict:
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
        import json as _json
        from bok_voice_business_db.models import UsageRecord

        sr_raw = call.get("session_report") or ""
        tokens = 0
        try:
            tokens = int((_json.loads(sr_raw) or {}).get("llm_usage", {}).get("total_tokens") or 0)
        except Exception:
            tokens = 0
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
    # 可观测（2026-09-07）：失败重试 1 次;仍空→审计事件 settle.distill_empty,
    # 唔再静默吞掉（蒸馏覆盖率从此可查）。
    try:
        from .summarize import Summarizer

        settings = _repo().get_settings()
        summ = Summarizer().build(turns, call, settings)
        if not (summ.get("summary") or "").strip() and turns:
            summ = Summarizer().build(turns, call, settings)
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
    return _repo().get_settlement(call_id) or result


@app.get("/api/objects")
def list_objects(account_id: str = "acc-001") -> list[dict]:
    return _repo().list_objects(account_id)


@app.get("/api/objects/{object_id}")
def get_object(object_id: str) -> dict:
    obj = _repo().get_object(object_id)
    if not obj:
        raise HTTPException(404, "object not found")
    return obj


@app.post("/api/objects")
def create_object(account_id: str, req: CreateObjectRequest) -> dict:
    obj = _repo().create_object(account_id, req.model_dump())
    _audit("object.create", subject_type="object", subject_id=obj.get("id", ""), account_id=account_id, detail={"display_name": obj.get("display_name", "")})
    return obj


@app.patch("/api/objects/{object_id}")
def update_object(object_id: str, req: UpdateObjectRequest) -> dict:
    existing = _repo().get_object(object_id)
    obj = _repo().update_object(object_id, req.model_dump())
    if not obj:
        raise HTTPException(404, "object not found")
    _audit("object.update", subject_type="object", subject_id=object_id, account_id=(existing or {}).get("account_id", ""), detail={"display_name": obj.get("display_name", "")})
    return obj


@app.delete("/api/objects/{object_id}")
def delete_object(object_id: str) -> dict:
    existing = _repo().get_object(object_id)
    if not _repo().delete_object(object_id):
        raise HTTPException(404, "object not found")
    _audit("object.delete", subject_type="object", subject_id=object_id, account_id=(existing or {}).get("account_id", ""))
    return {"object_id": object_id, "deleted": True}


@app.post("/api/objects/import")
def import_objects(account_id: str, rows: list[CreateObjectRequest] = Body(...)) -> dict:
    created = [_repo().create_object(account_id, row.model_dump()) for row in rows]
    return {"imported": len(created), "items": created}


@app.get("/api/knowledge/search")
async def search_knowledge(query: str, account_id: str = "acc-001", limit: int = 5) -> list[dict]:
    return await app.state.knowledge.search(query, account_id, limit)


@app.get("/api/knowledge")
async def list_knowledge(account_id: str = "acc-001") -> list[dict]:
    return await app.state.knowledge.list(account_id)


@app.delete("/api/knowledge")
async def delete_knowledge(knowledge_id: str, account_id: str = "acc-001") -> dict:
    # id 形如 md:accounts/acc-001/knowledge/probe.md（含斜杠），放 path 参数会被
    # Starlette 路由层以 %2F 拒掉（404）——改走 query 参数最稳。
    removed = await app.state.knowledge.delete(account_id, [knowledge_id])
    _audit("knowledge.delete", subject_type="knowledge", subject_id=knowledge_id, account_id=account_id, detail={"removed": removed})
    return {"deleted": removed, "knowledge_id": knowledge_id}


@app.post("/api/knowledge/import")
async def import_knowledge(req: ImportRequest) -> dict:
    result = await app.state.knowledge.import_document(req.account_id, req.path, req.content)
    _audit("knowledge.import", subject_type="knowledge", subject_id=req.path or "", detail={"account_id": req.account_id, "content_len": len(req.content)})
    return result


# ---- 快答库(Q→A 检索快路,2026-09-09):条目 CRUD + 高频配对报告 ----

@app.get("/api/qa-entries")
def list_qa_entries(account_id: str = "acc-001", enabled: int | None = None) -> list[dict]:
    return _repo().list_qa_entries(account_id, enabled=None if enabled is None else bool(enabled))


@app.post("/api/qa-entries")
def create_qa_entry(req: QaEntryCreate) -> dict:
    row = _repo().create_qa_entry(req.model_dump())
    _audit("qa_entry.create", subject_type="qa_entry", subject_id=row.get("id", ""), account_id=req.account_id)
    return row


@app.patch("/api/qa-entries/{entry_id}")
def update_qa_entry(entry_id: str, req: QaEntryPatch) -> dict:
    row = _repo().update_qa_entry(entry_id, {k: v for k, v in req.model_dump().items() if v is not None})
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="qa entry not found")
    _audit("qa_entry.update", subject_type="qa_entry", subject_id=entry_id)
    return row


@app.delete("/api/qa-entries/{entry_id}")
def delete_qa_entry(entry_id: str) -> dict:
    ok = _repo().delete_qa_entry(entry_id)
    _audit("qa_entry.delete", subject_type="qa_entry", subject_id=entry_id, outcome="ok" if ok else "not_found")
    return {"deleted": ok, "id": entry_id}


@app.post("/api/qa-entries/{entry_id}/hit")
def hit_qa_entry(entry_id: str) -> dict:
    """agent 快路命中计数(fire-and-forget,幂等无副作用)。"""
    _repo().incr_qa_hit(entry_id)
    return {"id": entry_id}


@app.get("/api/reports/qa-pairs")
def report_qa_pairs(
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
    conversations = _repo().iter_call_conversations(account_id, exclude_test_objects=exclude_test)
    return mine_qa_pairs(conversations, min_calls=min_calls, limit=limit)


@app.get("/api/personas")
def list_personas(account_id: str = "acc-001") -> list[dict]:
    return _repo().list_personas(account_id)


@app.get("/api/personas/{persona_id}")
def get_persona(persona_id: str) -> dict:
    persona = _repo().get_persona(persona_id)
    if not persona:
        raise HTTPException(404, "persona not found")
    return persona


@app.post("/api/personas")
def create_persona(req: PersonaRequest, request: Request) -> dict:
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
    # 冻结更新前快照(内存 repo 返回活引用,update 原地改会令 existing==persona,
    # 音色变化判定恒 False);audit 与物化触发都以此为准。
    existing = dict(_repo().get_persona(persona_id) or {})
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
    persona = _repo().create_persona(req.model_dump())
    out = dict(persona)
    out["tts_pregen"] = persona_pregen_status(
        out, base_url=str(request.base_url).rstrip("/")
    )
    return out


@app.delete("/api/personas/{persona_id}")
def delete_persona(persona_id: str) -> dict:
    existing = _repo().get_persona(persona_id)
    if not _repo().delete_persona(persona_id):
        raise HTTPException(404, "persona not found")
    _audit("persona.delete", subject_type="persona", subject_id=persona_id, account_id=(existing or {}).get("account_id", ""))
    return {"persona_id": persona_id, "deleted": True}


@app.get("/api/templates")
def list_templates(account_id: str = "acc-001") -> list[dict]:
    return _repo().list_templates(account_id)


@app.get("/api/templates/{template_id}")
def get_template(template_id: str) -> dict:
    tpl = _repo().get_template(template_id)
    if not tpl:
        raise HTTPException(404, "template not found")
    return tpl


@app.post("/api/templates")
def create_template(req: TemplateRequest) -> dict:
    tpl = _repo().create_template(req.model_dump())
    _audit("template.create", subject_type="template", subject_id=tpl.get("id", ""), account_id=tpl.get("account_id", ""), detail={"name": tpl.get("name", "")})
    return tpl


@app.put("/api/templates/{template_id}")
def update_template(template_id: str, req: UpdateTemplateRequest) -> dict:
    before = _repo().get_template(template_id)
    if not before:
        raise HTTPException(404, "template not found")
    # 话术版本化（2026-09-07 专项 B3）:update 即快照旧版——「哪版话术转化更好」
    # 从数据上可答;call_sessions.template_id 快照指向的版本内容不再随更新漂移。
    # default=str:SQL repo 的 before 含 datetime(created_at),不转直接 500——
    # 每次编辑保存必炸且静默丢改动(2026-09-09 QA B2 实锤;in-memory 测试替身
    # 无 created_at 字段所以单测全绿,prod-only 断裂)。
    revision = len(_repo().list_template_revisions(template_id)) + 1
    import json as _revjson

    _repo().append_template_revision(template_id, revision, _revjson.dumps(before, ensure_ascii=False, default=str))
    # exclude_unset:部分更新只写请求里显式出现的键——schema 全字段带默认值
    # (language 默认 zh/name 默认空),整包 dump 会把未传字段抹掉(2026-09-09
    # QA「PUT 抹字段」实锤;前端 save 恒传全字段,行为不变,API 语义修正)。
    payload = req.model_dump(exclude_unset=True)
    tpl = _repo().update_template(template_id, payload)
    _audit(
        "template.update",
        subject_type="template",
        subject_id=template_id,
        account_id=tpl.get("account_id", ""),
        detail={"name": tpl.get("name", ""), "revision": revision,
                "changed": sorted(k for k in req.model_dump() if req.model_dump().get(k) not in (None, "") and before.get(k) != req.model_dump().get(k))},
    )
    return tpl


@app.get("/api/templates/{template_id}/revisions")
def template_revisions(template_id: str) -> list[dict]:
    return _repo().list_template_revisions(template_id)


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str) -> dict:
    tpl = _repo().get_template(template_id)
    if not _repo().delete_template(template_id):
        raise HTTPException(404, "template not found")
    _audit("template.delete", subject_type="template", subject_id=template_id, account_id=(tpl or {}).get("account_id", ""))
    return {"template_id": template_id, "deleted": True}


@app.get("/api/supervisor/active-calls")
def active_calls() -> list[dict]:
    """主管台可见通话：进行中(active) + 已暂停(paused / 人工接管)。"""
    calls = _repo().list_calls("", "") if hasattr(_repo(), "list_calls") else []
    return [c for c in calls if c.get("status") in (CallStatus.ACTIVE.value, CallStatus.PAUSED.value)]


@app.get("/api/reports/summary")
def reports_summary(account_id: str = "acc-001") -> dict:
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
def reports_calls(account_id: str = "acc-001") -> list[dict]:
    return _repo().list_calls(account_id, "")


@app.get("/api/insights")
def list_insights() -> list[dict]:
    """全局洞察（结算时 Summarizer 蒸馏产出，跨对象共性的观察）。"""
    return _repo().list_global_insights(kind="insight")


@app.get("/api/objects/{object_id}/topics")
def list_object_topics(object_id: str) -> list[dict]:
    """对象历史主题（结算时 Summarizer 蒸馏产出并 append 到该对象）。"""
    return _repo().list_object_topics(object_id)


@app.post("/api/calls/{call_id}/session-report")
async def ingest_session_report(call_id: str, request: Request) -> dict:
    """Agent shutdown 上报官方 SessionReport（真实逐模型 usage + 权威 chat_history 快照）。

    存 call_sessions.session_report(JSON)；结算/报表优先吃这里的真数据，
    没有上报的旧通话才回退估算口径。
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    row = _repo().update_call(call_id, session_report=json.dumps(payload, ensure_ascii=False, default=str))
    if not row:
        raise HTTPException(status_code=404, detail="call not found")
    _audit("call.session_report", subject_type="call", subject_id=call_id, account_id=row.get("account_id", ""))
    return {"call_id": call_id, "stored": True}


# 重派防复活门:终态通话(或记录已删)不得重派——为死通话新建的 dispatch 无
# 回收路径(reaper 只扫 ACTIVE/PAUSED),agent 会被带进空房念开场白。
# 重派重试排程(秒):等 livekit 把死 worker 的 job 判 FAILED / dispatch 服务恢复。测试可注入。
_REDISPATCH_RETRY_SCHEDULE = (0.0, 10.0, 25.0)
# per-room 重派锁:串行化「has_active_dispatch 检查 + create_dispatch」临界区,
# 收口 TOCTOU(后进锁者复查时见到先进锁者新建的 dispatch → 跳过,并发双 create
# 坍缩为一次)。锁图按房间单调增长:每通话房间一个小锁对象,CP 单进程 4-6 路并发
# 规模下可接受;不做淘汰——锁被取走瞬间另一任务可能正持有,边界不值得。
_redispatch_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


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
    try:
        payload = await request.json()
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
def script_insights(account_id: str = "acc-001", limit: int = 10) -> dict:
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
def distill_health(account_id: str = "acc-001", limit: int = 10) -> dict:
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
def get_object_digest(object_id: str) -> dict:
    obj = _repo().get_object(object_id)
    if not obj:
        raise HTTPException(404, "object not found")
    return {"object_id": object_id, "digest": obj.get("digest", "")}


@app.get("/api/reports/usage")
def reports_usage(account_id: str = "acc-001") -> dict:
    calls = _repo().list_calls(account_id, "")
    # 真实用量优先：官方 SessionReport 的逐模型 input/output tokens；
    # 没有上报的旧通话才回退「轮次数」估算（口径见字段名后缀）。
    llm_tokens = 0
    estimated_calls = 0
    for c in calls:
        raw = c.get("session_report") or ""
        tokens = 0
        if raw:
            try:
                for u in json.loads(raw).get("usage") or []:
                    if u.get("type") == "llm_usage":
                        tokens += int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
            except Exception:
                tokens = 0
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
def list_audit(account_id: str = "", action: str = "", call_id: str = "", limit: int = 200) -> list[dict]:
    repo = _repo()
    if hasattr(repo, "list_audit_events"):
        return repo.list_audit_events(account_id=account_id, action=action, call_id=call_id, limit=limit)
    return []


@app.get("/api/setup")
def setup_status() -> dict:
    """Report first-run model readiness for the desktop setup wizard."""
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
def setup_download() -> dict:
    """Trigger model download (best-effort; UI polls /api/setup for progress)."""
    try:
        import subprocess

        root = Path(os.environ.get("BOK_ROOT", "."))
        subprocess.Popen(
            [sys.executable, str(root / "tools" / "bok.py"), "setup", "download"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(root),
        )
        return {"started": True}
    except Exception as exc:
        return {"started": False, "error": str(exc)}


def _parse_setup(stdout: str) -> dict:
    try:
        return json.loads(stdout)
    except Exception:
        return {"ready": False, "models": [], "error": "unable to parse setup status"}


@app.post("/api/supervisor/{call_id}/join")
def supervisor_join(call_id: str) -> dict:
    """主管进房:校验通话存在并直接签发 supervisor token(官方 TokenSource 契约)。

    以前只回 role 不回 token(与 CONTRACTS.md「主管进房 token」自相矛盾);现在
    与 /api/token 同一条签发链路(identity=supervisor-<room>,挂 bok.role 属性,
    A 线 dispatch 由 token 端点统一处理)。
    """
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    issued = token(TokenRequest(call_id=call_id, role="supervisor"))
    return {
        "call_id": call_id,
        "status": call.get("status", "active"),
        "role": Role.SUPERVISOR.value,
        "serverUrl": issued.serverUrl,
        "participantToken": issued.participantToken,
    }


@app.post("/api/web_logs")
async def web_logs(payload: dict) -> dict:
    """web 客户端(浏览器侧)关键事件落盘——同传控制台的设备枚举/自动分配/sink 路由/
    麦克风开关等决策只发生在浏览器里,服务端日志全然看不见(2026-09-12 同传输出
    路由排障多轮全靠排除法实证)。JSON 行追加 logs/web-client.log,与 agent.log 同
    目录;行限长防刷爆。上报失败静默(诊断通道永不影响功能)。"""
    from datetime import datetime, timezone

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
def pause_agent(call_id: str) -> dict:
    call = _repo().update_call(call_id, status=CallStatus.PAUSED.value)
    if not call:
        raise HTTPException(404, "call not found")
    _audit("supervisor.pause", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id)
    return {"call_id": call_id, "action": "pause-agent", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/resume-agent")
def resume_agent(call_id: str) -> dict:
    """恢复 AI 自动应答：解除人工接管并把通话置回 active（agent 轮询到后恢复）。"""
    call = _repo().update_call(call_id, escalated_to_human=False, status=CallStatus.ACTIVE.value)
    if not call:
        raise HTTPException(404, "call not found")
    _audit("supervisor.resume", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id)
    return {"call_id": call_id, "action": "resume-agent", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/takeover")
def takeover(call_id: str) -> dict:
    call = _repo().update_call(call_id, escalated_to_human=True, status=CallStatus.PAUSED.value)
    if not call:
        raise HTTPException(404, "call not found")
    _audit("supervisor.takeover", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id)
    return {"call_id": call_id, "action": "takeover", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/transfer")
async def transfer(call_id: str) -> dict:
    call = _repo().update_call(call_id, escalated_to_human=True, disposition="transferred", status=CallStatus.ENDED.value)
    if not call:
        raise HTTPException(404, "call not found")
    _disconnect_room_background(call_id)
    _audit("supervisor.transfer", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id)
    return {"call_id": call_id, "action": "transfer", "status": call["status"], "disconnected": True}


@app.post("/api/supervisor/{call_id}/end")
async def supervisor_end(call_id: str, disposition: str = "declined") -> dict:
    """AI 收尾后主动结束通话:置 ENDED 并断房。

    disposition=declined(客户明确拒绝/告别,默认)| no_response(沉默心跳两次无回应)。
    agent 讲完一句礼貌再见后调用;结算由 agent 侧 _on_close 幂等触发,这里只负责
    归档 disposition + 踢出房间。房间不存在/服务不可用不阻塞(DB 已置 ENDED)。
    """
    disposition = (disposition or "declined").strip()[:64] or "declined"
    call = _repo().update_call(call_id, escalated_to_human=False, disposition=disposition, status=CallStatus.ENDED.value)
    if not call:
        raise HTTPException(404, "call not found")
    _disconnect_room_background(call_id)
    _audit("supervisor.end", subject_type="call", subject_id=call_id, account_id=call.get("account_id", ""), call_id=call_id, detail={"disposition": disposition})
    return {"call_id": call_id, "action": "end", "status": call["status"], "disconnected": True}
