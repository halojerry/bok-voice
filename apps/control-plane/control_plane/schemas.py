from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from bok_voice_core.types import CallMode, CallStatus, Role, SettlementStatus


class TokenRequest(BaseModel):
    account_id: str = "acc-001"
    object_id: str = ""
    call_id: str = ""
    # 参与者角色:operator(默认,客服操作端) / me(同传我方端,创建房间并挂 agent 分发) /
    # other(同传对方端) / supervisor(主管旁听)。官方契约路径(participant_identity
    # 前缀)优先于本字段。
    role: str = "operator"
    # ---- LiveKit 官方 TokenSource endpoint 契约(livekit_token_source.proto,
    # snake_case 请求体)。room_name/participant_identity 提供时优先于旧字段。
    room_name: str = ""
    participant_identity: str = ""
    participant_name: str = ""
    participant_metadata: str = ""


class TokenResponse(BaseModel):
    # LiveKit 官方 TokenSourceResponse 契约字段(proto JSON camelCase——官方
    # development token server 同款;客户端 fromJson 双向兼容 snake/camel)。
    # TokenSource.endpoint/custom 自此直连本端点,不再需要键名映射层。
    serverUrl: str = "ws://127.0.0.1:7880"
    participantToken: str = ""


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
    # 语言/音色模式（agent 运行时读取；设置以 JSON blob 存储，旧档缺键=默认值，
    # 无需 DB 迁移）：tts.voice_mode: single=整场同声(collapse 成主音色,默认) |
    # per_language=按语言分音色(speaker_zh/speaker_cantonese/speaker_en 逐轮切换)；
    # asr.language_mode: auto=锚定+滞回跟随(默认) | fixed=钉死 language 指定语言。
    voice_mode: str = "single"
    language_mode: str = "auto"
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
