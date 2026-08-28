from __future__ import annotations

import hashlib
import json
import os
import uuid

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from bok_voice_core.providers import BusinessRepository
from bok_voice_core.policies import select_session_manifest
from bok_voice_core.types import CallMode, CallStatus, Role, SessionManifest, TurnEvent

from bok_voice_core.settlement import SettlementTrigger
from bok_voice_core.embeddings import CharHashEmbedding
from bok_voice_knowledge.knowledge import DefaultKnowledgeService
from bok_voice_knowledge.markdown_source import LocalMarkdownSource
from bok_voice_knowledge.vector_store import InMemoryVectorStore
from bok_voice_business_db.vector_store import SqlVectorStore

from .deps import build_engine, build_repository
from .schemas import (
    CreateCallRequest,
    CreateObjectRequest,
    ImportRequest,
    PersonaRequest,
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
    return app.state.repo


@app.on_event("startup")
def _startup() -> None:
    engine = build_engine()
    app.state.repo = build_repository(engine)
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


@app.post("/api/objects/import")
def import_objects(account_id: str, rows: list[CreateObjectRequest]) -> dict:
    created = [_repo().create_object(account_id, row.model_dump()) for row in rows]
    return {"imported": len(created), "items": created}


@app.get("/api/knowledge/search")
async def search_knowledge(query: str, account_id: str = "acc-001", limit: int = 5) -> list[dict]:
    return await app.state.knowledge.search(query, account_id, limit)


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


@app.put("/api/personas")
def upsert_persona(req: PersonaRequest) -> dict:
    return _repo().create_persona(req.model_dump())


@app.get("/api/supervisor/active-calls")
def active_calls() -> list[dict]:
    return [c for c in _repo().list_calls("", "active")] if hasattr(_repo(), "list_calls") else []


@app.post("/api/supervisor/{call_id}/join")
def supervisor_join(call_id: str) -> dict:
    return {"call_id": call_id, "token": "dev-supervisor-token", "role": Role.SUPERVISOR.value}


@app.post("/api/supervisor/{call_id}/pause-agent")
def pause_agent(call_id: str) -> dict:
    return {"call_id": call_id, "action": "pause-agent", "status": "ok"}


@app.post("/api/supervisor/{call_id}/takeover")
def takeover(call_id: str) -> dict:
    _repo().update_call(call_id, escalated_to_human=True)
    return {"call_id": call_id, "action": "takeover", "status": "ok"}


@app.post("/api/supervisor/{call_id}/transfer")
def transfer(call_id: str) -> dict:
    _repo().update_call(call_id, escalated_to_human=True, disposition="transferred")
    return {"call_id": call_id, "action": "transfer", "status": "ok"}
