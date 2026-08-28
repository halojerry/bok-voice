from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from bok_voice_core.types import CallMode, CallStatus, Role, SettlementStatus


class TokenRequest(BaseModel):
    account_id: str = "acc-001"
    object_id: str = ""
    call_id: str = ""


class TokenResponse(BaseModel):
    url: str = "ws://localhost:7880"
    token: str = ""
    roomName: str = ""


class CreateCallRequest(BaseModel):
    account_id: str
    object_id: str
    persona_id: str = ""
    mode: CallMode = CallMode.SIMULATION
    direction: str = "webrtc"
    language: str = "zh"
    tts_reference_voice: str = ""


class CreateObjectRequest(BaseModel):
    display_name: str
    role_template: str = "customer"
    language: str = "zh"
    background: str = ""
    phone: str = ""


class ImportRequest(BaseModel):
    account_id: str
    path: str
    content: str


class PersonaRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    company: str = ""
    tone: str = ""
    language: str = "zh"
    reference_audio: str = ""


class SupervisorCommand(BaseModel):
    call_id: str
