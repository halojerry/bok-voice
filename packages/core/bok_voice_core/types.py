from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallMode(str, Enum):
    SIMULATION = "simulation"
    LIVE = "live"


class CallStatus(str, Enum):
    IDLE = "idle"
    RINGING = "ringing"
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"
    FAILED = "failed"


class SettlementStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class Role(str, Enum):
    OPERATOR = "operator"
    ADMIN = "admin"
    SUPERVISOR = "supervisor"


class ProviderKind(str, Enum):
    VAD = "vad"
    ASR = "asr"
    LLM = "llm"
    TTS = "tts"
    EMBEDDING = "embedding"


@dataclass
class SessionManifest:
    session_id: str
    account_id: str
    object_id: str
    persona_id: str
    mode: CallMode
    direction: str
    language: str
    providers: dict[str, str]
    policy: str = "offline_first"
    tts_reference_voice: str = ""
    template_id: str = ""


@dataclass
class ContextBundle:
    system_prompt: str = ""
    object_card: dict[str, Any] = field(default_factory=dict)
    product_snippets: list[dict[str, Any]] = field(default_factory=list)
    history_snippets: list[dict[str, Any]] = field(default_factory=list)
    current_turns: list[dict[str, Any]] = field(default_factory=list)
    global_hints: list[dict[str, Any]] = field(default_factory=list)
    token_estimate: int = 0
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TurnEvent:
    trace_id: str
    call_id: str
    turn_id: str
    role: str
    transcript: str
    emotion: str = ""
    provider: str = ""
    latency_ms: int = 0


@dataclass
class ObjectProfile:
    id: str
    account_id: str
    display_name: str
    role_template: str = "customer"
    language: str = "zh"
    background: str = ""
    phone: str = ""
    tracking_no: str = ""
    courier: str = ""
    template_id: str = ""
    status: str = "active"


@dataclass
class ConversationTemplate:
    id: str
    account_id: str
    name: str = ""
    opening: str = ""
    core: str = ""
    objection: str = ""
    closing: str = ""
    tone_override: str = ""
    language: str = "zh"
    steps_json: str = ""


@dataclass
class ObjectTopic:
    id: str
    object_id: str
    account_id: str
    topic: str
    summary: str = ""
    created_at: str = field(default_factory=utcnow)


@dataclass
class PersonaProfile:
    id: str
    account_id: str = ""
    name: str = ""
    company: str = ""
    tone: str = ""
    language: str = "zh"
    reference_audio: str = ""
    tts_provider: str = ""


@dataclass
class CallSession:
    id: str
    account_id: str
    object_id: str
    persona_id: str
    mode: CallMode
    direction: str = "webrtc"
    language: str = "zh"
    contact_phone: str = ""
    consent_status: str = "unknown"
    recording_enabled: bool = False
    disposition: str = ""
    escalated_to_human: bool = False
    status: CallStatus = CallStatus.IDLE
    created_at: str = field(default_factory=utcnow)


@dataclass
class GlobalInsight:
    id: str
    kind: str = "objection"
    statement: str = ""
    confidence: float = 0.0
    language: str = "zh"
    status: str = "active"


@dataclass
class UsageRecord:
    account_id: str
    call_id: str
    provider: str
    kind: str
    units: int = 0
    tokens: int = 0
    audio_seconds: float = 0.0
    latency_ms: int = 0
    cost_estimate: float = 0.0
    status: str = "ok"
    created_at: str = field(default_factory=utcnow)
