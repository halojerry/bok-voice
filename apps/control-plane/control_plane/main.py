from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
import wave

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

from .deps import build_engine, build_repository, build_session_factory
from .schemas import (
    CreateCallRequest,
    CreateObjectRequest,
    ImportRequest,
    PersonaRequest,
    UpdateObjectRequest,
    UpdatePersonaRequest,
    SettingsRequest,
    TokenRequest,
    TokenResponse,
)


app = FastAPI(title="Bok Voice Control Plane", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    engine = build_engine()
    app.state.repo = build_repository(engine)
    app.state.session_factory = build_session_factory(engine)
    app.state.lk_key = os.environ.get("LIVEKIT_API_KEY", "")
    app.state.lk_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    app.state.lk_url = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
    vault = os.environ.get("VAULT_ROOT", "./data/vault")
    embedder = CharHashEmbedding(384)
    if engine is not None:
        from sqlalchemy.orm import sessionmaker

        session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        vector = SqlVectorStore(session_factory(), embedder)
    else:
        vector = InMemoryVectorStore()
    app.state.knowledge = DefaultKnowledgeService(
        markdown=LocalMarkdownSource(vault),
        vector=vector,
    )
    app.state.settlement = SettlementTrigger()


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
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/tts/preview")
async def tts_preview(payload: dict) -> Response:
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{_qwen3_tts_url()}/v1/audio/speech",
                json={
                    "input": payload.get("text", ""),
                    "voice": payload.get("voice", ""),
                    "language": payload.get("language", "Auto"),
                    "instruct": payload.get("instruct", ""),
                    "sample_rate": int(payload.get("sample_rate") or 24000),
                    "response_format": "pcm",
                },
            )
            resp.raise_for_status()
            pcm = resp.content
        sample_rate = int(payload.get("sample_rate") or 24000)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        return Response(content=buffer.getvalue(), media_type="audio/wav")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/token", response_model=TokenResponse)
def token(req: TokenRequest) -> TokenResponse:
    room = req.call_id or f"call-{uuid.uuid4().hex[:8]}"
    key = getattr(app.state, "lk_key", "") or os.environ.get("LIVEKIT_API_KEY", "")
    secret = getattr(app.state, "lk_secret", "") or os.environ.get("LIVEKIT_API_SECRET", "")
    url = getattr(app.state, "lk_url", "") or os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
    if key and secret:
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
    else:
        # Dev fallback (no LiveKit keys configured): keep tests reproducible.
        token = hashlib.sha256(f"{req.account_id}:{room}".encode()).hexdigest()
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
    manifest = select_session_manifest(
        session_id=f"call-{uuid.uuid4().hex[:8]}",
        account_id=req.account_id,
        object_id=req.object_id,
        persona_id=req.persona_id,
        mode=req.mode,
        direction=req.direction,
        language=req.language,
        tts_reference_voice=req.tts_reference_voice,
    )
    return _repo().create_call(manifest)


@app.get("/api/calls")
def list_calls(account_id: str = "acc-001", status: str = "") -> list[dict]:
    return _repo().list_calls(account_id, status)


@app.get("/api/calls/{call_id}")
def get_call(call_id: str) -> dict:
    call = _repo().get_call(call_id)
    if not call:
        raise HTTPException(404, "call not found")
    return call


@app.post("/api/calls/{call_id}/hangup")
def hangup(call_id: str) -> dict:
    call = _repo().update_call(call_id, status=CallStatus.ENDED.value)
    if not call:
        raise HTTPException(404, "call not found")
    return {"call_id": call_id, "status": call["status"]}


@app.post("/api/calls/{call_id}/turns")
def add_turn(call_id: str, role: str, transcript: str, emotion: str = "") -> dict:
    turn = TurnEvent(trace_id=call_id, call_id=call_id, turn_id=f"t{len(_repo().get_turns(call_id))}", role=role, transcript=transcript, emotion=emotion)
    return _repo().create_turn(turn)


@app.get("/api/calls/{call_id}/settlement")
def get_settlement(call_id: str) -> dict:
    settlement = _repo().get_settlement(call_id)
    if not settlement:
        raise HTTPException(404, "settlement not found")
    return settlement


@app.get("/api/calls/{call_id}/turns")
def get_turns(call_id: str) -> list[dict]:
    return [turn.__dict__ for turn in _repo().get_turns(call_id)]


@app.post("/api/calls/{call_id}/settle")
def settle(call_id: str) -> dict:
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
    return _repo().append_settlement(call_id, result)


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
    return _repo().create_object(account_id, req.model_dump())


@app.patch("/api/objects/{object_id}")
def update_object(object_id: str, req: UpdateObjectRequest) -> dict:
    obj = _repo().update_object(object_id, req.model_dump())
    if not obj:
        raise HTTPException(404, "object not found")
    return obj


@app.delete("/api/objects/{object_id}")
def delete_object(object_id: str) -> dict:
    if not _repo().delete_object(object_id):
        raise HTTPException(404, "object not found")
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


@app.delete("/api/knowledge/{knowledge_id}")
async def delete_knowledge(knowledge_id: str, account_id: str = "acc-001") -> dict:
    removed = await app.state.knowledge.delete(account_id, [knowledge_id])
    return {"deleted": removed, "knowledge_id": knowledge_id}


@app.post("/api/knowledge/import")
async def import_knowledge(req: ImportRequest) -> dict:
    return await app.state.knowledge.import_document(req.account_id, req.path, req.content)


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
    return _repo().create_persona(req.model_dump())


@app.put("/api/personas/{persona_id}")
def update_persona(persona_id: str, req: UpdatePersonaRequest) -> dict:
    persona = _repo().update_persona(persona_id, req.model_dump())
    if not persona:
        raise HTTPException(404, "persona not found")
    return persona


@app.put("/api/personas")
def upsert_persona(req: PersonaRequest) -> dict:
    return _repo().create_persona(req.model_dump())


@app.delete("/api/personas/{persona_id}")
def delete_persona(persona_id: str) -> dict:
    if not _repo().delete_persona(persona_id):
        raise HTTPException(404, "persona not found")
    return {"persona_id": persona_id, "deleted": True}


@app.get("/api/supervisor/active-calls")
def active_calls() -> list[dict]:
    return [c for c in _repo().list_calls("", "active")] if hasattr(_repo(), "list_calls") else []


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


@app.get("/api/reports/usage")
def reports_usage(account_id: str = "acc-001") -> dict:
    calls = _repo().list_calls(account_id, "")
    return {
        "asr_calls": len(calls),
        "llm_tokens": sum(len(_repo().get_turns(c["id"])) for c in calls),
        "tts_calls": len(calls),
        "vad_calls": len(calls),
    }


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


@app.post("/api/supervisor/{call_id}/takeover")
def takeover(call_id: str) -> dict:
    call = _repo().update_call(call_id, escalated_to_human=True, status=CallStatus.PAUSED.value)
    if not call:
        raise HTTPException(404, "call not found")
    return {"call_id": call_id, "action": "takeover", "status": call["status"]}


@app.post("/api/supervisor/{call_id}/transfer")
def transfer(call_id: str) -> dict:
    call = _repo().update_call(call_id, escalated_to_human=True, disposition="transferred", status=CallStatus.ENDED.value)
    if not call:
        raise HTTPException(404, "call not found")
    return {"call_id": call_id, "action": "transfer", "status": call["status"]}
