from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import uuid
import wave
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from bok_voice_core.providers import BusinessRepository
from bok_voice_core.policies import select_session_manifest
from bok_voice_core.types import CallMode, CallStatus, Role, SessionManifest, TurnEvent

from bok_voice_core.settlement import SettlementTrigger
from bok_voice_core.embeddings import CharHashEmbedding
from bok_voice_knowledge.knowledge import DefaultKnowledgeService
from bok_voice_knowledge.markdown_source import LocalMarkdownSource
from bok_voice_knowledge.vector_store import InMemoryVectorStore
from bok_voice_business_db.vector_store import SqlVectorStore
from bok_voice_obs.audit import AuditEvent, AuditStore, audit_store
from bok_voice_obs.context import get_correlation
from bok_voice_obs.logging import configure_logging, get_logger
from bok_voice_obs.middleware import CorrelationMiddleware

from .deps import build_engine, build_repository, build_session_factory
from .schemas import (
    CreateCallRequest,
    CreateObjectRequest,
    ImportRequest,
    PersonaRequest,
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


app = FastAPI(title="Bok Voice Control Plane", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CorrelationMiddleware)

control_log = get_logger("control-plane", component="control-plane", service="control-plane")


def _repo() -> BusinessRepository:
    # SQL 路径：每个 _repo() 调用都拿独立 Session（已提交数据对所有新 Session 可见），
    # 根除“共享 Session 并发操作 InvalidRequestError / PendingRollbackError”。
    factory = getattr(app.state, "session_factory", None)
    if factory is not None:
        from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

        return SqlAlchemyBusinessRepository(factory())
    return app.state.repo


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
        vector = InMemoryVectorStore()
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
        await vector.upsert(
            # path 存完整 vault 相对路径（accounts/acc-001/knowledge/...），
            # 与 import_document 的 path、delete 里 markdown.forget(path) 一致——
            # 否则 delete 找不到 vault 文件，重启后知识从 vault 复活。
            [{"id": f"md:{rel}", "text": content, "path": rel, "source": "vault"}],
            account_id,
        )


def _audit_dir():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("HOME", ".")) / "Library" / "Application Support"
    return base / "BokVoice" / "audit"


def _audit(action: str, *, subject_type: str = "", subject_id: str = "", outcome: str = "ok", account_id: str = "", detail: dict | None = None) -> dict:
    """Emit an audit event (JSONL + optional DB copy) from a request context."""
    detail = detail or {}
    event = audit_store().emit(
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        outcome=outcome,
        account_id=account_id,
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
        "policy": req.policy,
    }
    secret_keys = {"api_key", "access_token", "token"}
    for kind in ("asr", "llm", "tts", "vad"):
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
    secret_keys = {"api_key", "access_token", "token"}
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


@app.post("/api/tts/preview")
async def tts_preview(payload: dict) -> Response:
    """试听一段 TTS。provider=qwen3_tts 走本地 sidecar；provider=minimax 走云端 MiniMax
    （voice 是 MiniMax 音色 ID，如 Cantonese_Male_news_anchor_vv2）。返回 WAV。"""
    provider = str(payload.get("provider") or "qwen3_tts").lower()
    sample_rate = int(payload.get("sample_rate") or 24000)
    text = str(payload.get("text") or "")
    voice = str(payload.get("voice") or "")
    language = str(payload.get("language") or "zh")
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
                        "voice_setting": {"voice_id": voice, "speed": 1, "vol": 1, "pitch": 0},
                        "audio_setting": {"sample_rate": sample_rate, "format": "pcm", "channel": 1},
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                audio_hex = (body.get("data") or {}).get("audio") or ""
                if not audio_hex:
                    raise HTTPException(status_code=502, detail=f"minimax empty: {body.get('base_resp')}")
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


@app.post("/api/token", response_model=TokenResponse)
def token(req: TokenRequest) -> TokenResponse:
    room = req.call_id or f"call-{uuid.uuid4().hex[:8]}"
    key = getattr(app.state, "lk_key", "") or os.environ.get("LIVEKIT_API_KEY", "")
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    url = getattr(app.state, "lk_url", "") or os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if not key or not secret:
        # 必须走真实 JWT：旧 sha256 兜底会让前端 decodeTokenPayload 抛错、
        # LiveKit 服务器 401，A 线永远接不通。缺凭据时显式失败，不静默回退。
        raise HTTPException(status_code=503, detail="LiveKit credentials not configured")
    import datetime
    from livekit import api

    at = (
        api.AccessToken(key, secret)
        .with_identity(f"operator-{req.account_id}-{room}")
        .with_name("Bok Voice Operator")
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True, can_publish_data=True))
        .with_ttl(datetime.timedelta(seconds=3600))
    )
    token = at.to_jwt()
    # When the operator connects an existing call, flip it to ACTIVE so the supervisor
    # "active calls" view reflects the real live room.
    if req.call_id:
        try:
            _repo().update_call(req.call_id, status=CallStatus.ACTIVE.value)
        except Exception:
            pass
    return TokenResponse(token=token, roomName=room, url=url)


@app.post("/api/calls")
def create_call(req: CreateCallRequest) -> dict:
    # 会话清单：读取全局策略(offline_first/cloud_first)与已配置 provider，
    # 并把对象绑定的模板快照到 call（审计「这场用了哪版话术」）。
    settings = _repo().get_settings()
    policy = (settings or {}).get("policy") or "offline_first"
    providers = _effective_providers(settings or {})
    template_id = ""
    if req.object_id:
        obj = _repo().get_object(req.object_id)
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
    )
    return _repo().create_call(manifest)


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
    return _repo().list_calls(account_id, status)


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


@app.post("/api/calls/{call_id}/hangup")
async def hangup(call_id: str) -> dict:
    call = _repo().update_call(call_id, status=CallStatus.ENDED.value)
    if not call:
        raise HTTPException(404, "call not found")
    # 真正断开 LiveKit 房间：主管台/任意端挂断后 agent 与监听端都会被服务端踢出，
    # agent 侧 on_close 触发结算。房间不存在/服务不可用时不阻塞（DB 已置 ENDED）。
    await _disconnect_livekit_room(call_id)
    return {"call_id": call_id, "status": call["status"], "disconnected": True}


async def _disconnect_livekit_room(room_name: str) -> None:
    """调用 LiveKit RoomService 删除房间（房间名 = call_id），容错。"""
    key = getattr(app.state, "lk_key", "") or os.environ.get("LIVEKIT_API_KEY", "")
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    url = getattr(app.state, "lk_url", "") or os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if not key or not secret or not room_name:
        return
    http_url = url.replace("ws://", "http://").replace("wss://", "https://").rstrip("/")
    try:
        import aiohttp

        from livekit.api.room_service import DeleteRoomRequest, RoomService

        async with aiohttp.ClientSession() as session:
            svc = RoomService(session, http_url, key, secret)
            await svc.delete_room(DeleteRoomRequest(room=room_name))
    except Exception as exc:  # pragma: no cover - 房间不存在/livekit 未起都不阻塞挂断
        print(f"[cp] livekit room delete skipped ({room_name}): {exc!r}", flush=True)


@app.post("/api/calls/{call_id}/turns")
def add_turn(call_id: str, role: str, transcript: str, emotion: str = "") -> dict:
    turn = TurnEvent(trace_id=call_id, call_id=call_id, turn_id=f"t{len(_repo().get_turns(call_id))}", role=role, transcript=transcript, emotion=emotion)
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
    _audit("call.whatsapp_captured", subject_type="call", subject_id=call_id,
           account_id=call.get("account_id", "acc-001"),
           detail={"status": fields["whatsapp_status"], "number": (number or "")[:3] + "***"})
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


@app.get("/api/calls/{call_id}/settlement")
def get_settlement(call_id: str) -> dict:
    settlement = _repo().get_settlement(call_id)
    if not settlement:
        raise HTTPException(404, "settlement not found")
    return settlement


@app.get("/api/calls/{call_id}/turns")
def get_turns(call_id: str) -> list[dict]:
    return [turn.__dict__ for turn in _repo().get_turns(call_id)]


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


@app.post("/api/calls/{call_id}/settle")
async def settle(call_id: str) -> dict:
    existing = _repo().get_settlement(call_id)
    if existing:
        return existing
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
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
    # 总结/沉淀：用本机 LLM 生成总结正文 + 新话题 + 全局洞察（失败回退纯指标）。
    try:
        from .summarize import Summarizer

        settings = _repo().get_settings()
        summ = Summarizer().build(turns, call, settings)
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
def import_objects(account_id: str, rows: list[CreateObjectRequest]) -> dict:
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
    return {"deleted": removed, "knowledge_id": knowledge_id}


@app.post("/api/knowledge/import")
async def import_knowledge(req: ImportRequest) -> dict:
    result = await app.state.knowledge.import_document(req.account_id, req.path, req.content)
    _audit("knowledge.import", subject_type="knowledge", subject_id=req.path or "", detail={"account_id": req.account_id, "content_len": len(req.content)})
    return result


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
def create_persona(req: PersonaRequest) -> dict:
    persona = _repo().create_persona(req.model_dump())
    _audit("persona.create", subject_type="persona", subject_id=persona.get("id", ""), account_id=persona.get("account_id", ""), detail={"name": persona.get("name", "")})
    return persona


@app.put("/api/personas/{persona_id}")
def update_persona(persona_id: str, req: UpdatePersonaRequest) -> dict:
    existing = _repo().get_persona(persona_id)
    persona = _repo().update_persona(persona_id, req.model_dump())
    if not persona:
        raise HTTPException(404, "persona not found")
    _audit("persona.update", subject_type="persona", subject_id=persona_id, account_id=(existing or {}).get("account_id", ""), detail={"name": persona.get("name", "")})
    return persona


@app.put("/api/personas")
def upsert_persona(req: PersonaRequest) -> dict:
    return _repo().create_persona(req.model_dump())


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
    tpl = _repo().update_template(template_id, req.model_dump())
    if not tpl:
        raise HTTPException(404, "template not found")
    _audit("template.update", subject_type="template", subject_id=template_id, account_id=tpl.get("account_id", ""), detail={"name": tpl.get("name", "")})
    return tpl


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


@app.get("/api/reports/usage")
def reports_usage(account_id: str = "acc-001") -> dict:
    calls = _repo().list_calls(account_id, "")
    return {
        "asr_calls": len(calls),
        "llm_tokens": sum(len(_repo().get_turns(c["id"])) for c in calls),
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
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    return {"call_id": call_id, "status": call.get("status", "active"), "role": Role.SUPERVISOR.value}


@app.post("/api/supervisor/{call_id}/pause-agent")
def pause_agent(call_id: str) -> dict:
    call = _repo().update_call(call_id, status=CallStatus.PAUSED.value)
    if not call:
        raise HTTPException(404, "call not found")
    return {"call_id": call_id, "action": "pause-agent", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/resume-agent")
def resume_agent(call_id: str) -> dict:
    """恢复 AI 自动应答：解除人工接管并把通话置回 active（agent 轮询到后恢复）。"""
    call = _repo().update_call(call_id, escalated_to_human=False, status=CallStatus.ACTIVE.value)
    if not call:
        raise HTTPException(404, "call not found")
    return {"call_id": call_id, "action": "resume-agent", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/takeover")
def takeover(call_id: str) -> dict:
    call = _repo().update_call(call_id, escalated_to_human=True, status=CallStatus.PAUSED.value)
    if not call:
        raise HTTPException(404, "call not found")
    return {"call_id": call_id, "action": "takeover", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/transfer")
async def transfer(call_id: str) -> dict:
    call = _repo().update_call(call_id, escalated_to_human=True, disposition="transferred", status=CallStatus.ENDED.value)
    if not call:
        raise HTTPException(404, "call not found")
    await _disconnect_livekit_room(call_id)
    return {"call_id": call_id, "action": "transfer", "status": call["status"], "disconnected": True}
