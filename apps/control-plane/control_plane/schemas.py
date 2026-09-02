from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from bok_voice_core.types import CallMode, CallStatus, Role, SettlementStatus


class TokenRequest(BaseModel):
    account_id: str = "acc-001"
    object_id: str = ""
    call_id: str = ""


class TokenResponse(BaseModel):
    url: str = "ws://127.0.0.1:7880"
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
    template_id: str = ""


class ImportRequest(BaseModel):
    account_id: str
    path: str
    content: str


class UpdateObjectRequest(BaseModel):
    display_name: str = ""
    role_template: str = "customer"
    language: str = "zh"
    background: str = ""
    phone: str = ""
    template_id: str = ""
    status: str = "active"


class TemplateRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    opening: str = ""
    core: str = ""
    objection: str = ""
    closing: str = ""
    tone_override: str = ""
    language: str = "zh"


class UpdateTemplateRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    opening: str = ""
    core: str = ""
    objection: str = ""
    closing: str = ""
    tone_override: str = ""
    language: str = "zh"


class PersonaRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    company: str = ""
    tone: str = ""
    language: str = "zh"
    reference_audio: str = ""


class UpdatePersonaRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    company: str = ""
    tone: str = ""
    language: str = "zh"
    reference_audio: str = ""


class ProviderSettings(BaseModel):
    provider: str = ""
    model: str = ""
    base_url: str = ""
    backend: str = ""
    api_key: str = ""
    endpoint: str = ""
    language: str = ""
    speaker: str = ""
    speaker_zh: str = ""
    speaker_yue: str = ""
    speaker_en: str = ""
    instruct: str = ""
    resource_id: str = ""
    app_id: str = ""
    access_token: str = ""
    # VAD / 打断（agent 运行时会读取这些值；sensitivity 仅为兼容保留，UI 不再暴露）
    max_buffered_speech: float = 15.0
    min_speech_duration: float = 0.15
    min_silence_duration: float = 0.35
    interruption: bool = True
    sensitivity: float = 0.5
    sample_rate: int = 24000


class SettingsRequest(BaseModel):
    asr: ProviderSettings = ProviderSettings()
    llm: ProviderSettings = ProviderSettings()
    tts: ProviderSettings = ProviderSettings()
    vad: ProviderSettings = ProviderSettings()
    policy: str = "offline_first"


class SupervisorCommand(BaseModel):
    call_id: str
