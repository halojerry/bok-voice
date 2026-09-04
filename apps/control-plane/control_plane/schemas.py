from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from bok_voice_core.types import CallMode, CallStatus, Role, SettlementStatus


class TokenRequest(BaseModel):
    account_id: str = "acc-001"
    object_id: str = ""
    call_id: str = ""
    # 参与者角色:operator(默认,客服操作端) / me(同传我方端,创建房间并挂 agent 分发) /
    # other(同传对方端)。identity 分别为 operator-*/me-*/other-*。
    role: str = "operator"


class TokenResponse(BaseModel):
    url: str = "ws://127.0.0.1:7880"
    token: str = ""
    roomName: str = ""


class CreateCallRequest(BaseModel):
    account_id: str
    # 同传会话(kind=interpret)没有客服对象,允许空。
    object_id: str = ""
    persona_id: str = ""
    mode: CallMode = CallMode.SIMULATION
    direction: str = "webrtc"
    language: str = "zh"
    tts_reference_voice: str = ""
    # 会话种类:""=客服通话 / interpret=双端同传(B 线 v2)。target_lang=对方语言。
    kind: str = ""
    target_lang: str = ""


class CreateObjectRequest(BaseModel):
    display_name: str
    role_template: str = "customer"
    language: str = "zh"
    background: str = ""
    phone: str = ""
    tracking_no: str = ""
    courier: str = ""
    address: str = ""
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
    tracking_no: str = ""
    courier: str = ""
    address: str = ""
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
    steps_json: str = ""


class UpdateTemplateRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    opening: str = ""
    core: str = ""
    objection: str = ""
    closing: str = ""
    tone_override: str = ""
    language: str = "zh"
    steps_json: str = ""


class PersonaRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    company: str = ""
    tone: str = ""
    language: str = "zh"
    reference_audio: str = ""
    tts_provider: str = ""


class UpdatePersonaRequest(BaseModel):
    account_id: str = ""
    name: str = ""
    company: str = ""
    tone: str = ""
    language: str = "zh"
    reference_audio: str = ""
    tts_provider: str = ""


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
    speaker_cantonese: str = ""
    speaker_en: str = ""
    instruct: str = ""
    resource_id: str = ""
    app_id: str = ""
    access_token: str = ""
    # 本地 LLM 模型选择(ML Studio repo,如 avan-ag/Qwen3.5-4B-Uncensored-MLX-4bit):
    # 由 bok serve 读取,决定 :1235 起哪个模型;空则用 bok 默认。model 字段是请求体里的
    # 模型名,cloud 用;本地模型切换存这里,避免与 mlx 需要的全路径模型名冲突。
    local_model: str = ""
    # VAD / 打断（agent 运行时会读取这些值；sensitivity = Silero 激活阈值 0~1，
    # 越高越抗噪，越低越灵敏，agent 侧传给 inference.VAD activation_threshold）
    max_buffered_speech: float = 15.0
    min_speech_duration: float = 0.15
    min_silence_duration: float = 0.45
    interruption: bool = True
    sensitivity: float = 0.6
    sample_rate: int = 24000


class SettingsRequest(BaseModel):
    asr: ProviderSettings = ProviderSettings()
    llm: ProviderSettings = ProviderSettings()
    tts: ProviderSettings = ProviderSettings()
    vad: ProviderSettings = ProviderSettings()
    policy: str = "offline_first"


class SupervisorCommand(BaseModel):
    call_id: str


class WhatsAppCaptureRequest(BaseModel):
    """Agent 偵測到客戶俾 WhatsApp:number 有值=captured(客戶讀出號碼),空=offered(應承加專員)。"""

    number: str = ""


class WhatsAppHandledRequest(BaseModel):
    """專員喺操作台標記已對接。"""

    handled: bool = True
