from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PersonaProfile(Base):
    __tablename__ = "persona_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    company: Mapped[str] = mapped_column(String(255), default="")
    tone: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    reference_audio: Mapped[str] = mapped_column(String(512), default="")
    # 人设级 TTS 引擎："" = 跟随全局设置；qwen3_tts / minimax / volcano_streaming / fake。
    # 决定该人设通话用哪套音色（本地克隆 vs 云端 MiniMax/火山），避免克隆 ID 串引擎。
    tts_provider: Mapped[str] = mapped_column(String(32), default="")


class ObjectProfile(Base):
    __tablename__ = "object_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role_template: Mapped[str] = mapped_column(String(64), default="customer")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    background: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    # 快递场景变量:姓名=display_name;单号/物流公司/收货地址供话术变量与 LLM 引用。
    tracking_no: Mapped[str] = mapped_column(String(64), default="")
    courier: Mapped[str] = mapped_column(String(64), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    template_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")


class ConversationTemplate(Base):
    __tablename__ = "conversation_templates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    opening: Mapped[str] = mapped_column(Text, default="")
    core: Mapped[str] = mapped_column(Text, default="")
    objection: Mapped[str] = mapped_column(Text, default="")
    closing: Mapped[str] = mapped_column(Text, default="")
    tone_override: Mapped[str] = mapped_column(String(255), default="")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    # 分步话术:JSON 数组 [{"goal": "这一步要达成的目标", "ref": "参考说法(可含 {变量})"}, ...]。
    # 由 agent 每轮按步骤推进,LLM 结合客户回复只回应当前步。opening/core 四段保留兼容。
    steps_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ObjectTopic(Base):
    __tablename__ = "object_topics"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    topic: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CallSession(Base):
    __tablename__ = "call_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    object_id: Mapped[str] = mapped_column(String(64), index=True)
    persona_id: Mapped[str] = mapped_column(String(64), default="")
    template_id: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(32), default="simulation")
    direction: Mapped[str] = mapped_column(String(32), default="webrtc")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    contact_phone: Mapped[str] = mapped_column(String(64), default="")
    consent_status: Mapped[str] = mapped_column(String(32), default="unknown")
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    disposition: Mapped[str] = mapped_column(String(64), default="")
    escalated_to_human: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    # WhatsApp 對接狀態:""(未提供) / offered(客戶應承加專員,未俾號碼) /
    # captured(客戶讀出咗自己號碼) / handled(專員已標記對接)。customer_whatsapp 存號碼。
    whatsapp_status: Mapped[str] = mapped_column(String(16), default="")
    customer_whatsapp: Mapped[str] = mapped_column(String(64), default="")
    # 会话种类:""=客服通话(A 线) / interpret=双端同传(B 线 v2)。
    # interpret 会话:language=我方语言,target_lang=对方语言,object_id 通常为空。
    kind: Mapped[str] = mapped_column(String(32), default="")
    target_lang: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Turn(Base):
    __tablename__ = "turns"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    turn_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    transcript: Mapped[str] = mapped_column(Text)
    emotion: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Settlement(Base):
    __tablename__ = "settlements"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    summary: Mapped[str] = mapped_column(Text, default="")
    transcript_doc_path: Mapped[str] = mapped_column(String(512), default="")
    settlement_doc_path: Mapped[str] = mapped_column(String(512), default="")
    new_topics_json: Mapped[str] = mapped_column(Text, default="[]")
    global_insight_id: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class GlobalInsight(Base):
    __tablename__ = "global_insights"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), default="objection")
    statement: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    language: Mapped[str] = mapped_column(String(16), default="zh")
    status: Mapped[str] = mapped_column(String(32), default="active")


class GlobalSetting(Base):
    __tablename__ = "global_settings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default="global")
    asr_json: Mapped[str] = mapped_column(Text, default="{}")
    llm_json: Mapped[str] = mapped_column(Text, default="{}")
    tts_json: Mapped[str] = mapped_column(Text, default="{}")
    vad_json: Mapped[str] = mapped_column(Text, default="{}")
    policy: Mapped[str] = mapped_column(String(64), default="offline_first")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AuditEventRecord(Base):
    """Append-only business audit trail, mirrored from the JSONL sink.

    Keeps a queryable copy of every audited action (voice clone, settle,
    template/object/persona edits, settings save, knowledge import…) so the
    UI / reports can render a traceable history without scraping log files.
    """

    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), default="")
    subject_id: Mapped[str] = mapped_column(String(128), default="")
    actor: Mapped[str] = mapped_column(String(128), default="")
    outcome: Mapped[str] = mapped_column(String(32), default="ok")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    request_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    call_id: Mapped[str] = mapped_column(String(64), default="")
    account_id: Mapped[str] = mapped_column(String(64), default="")
    object_id: Mapped[str] = mapped_column(String(64), default="")
    persona_id: Mapped[str] = mapped_column(String(64), default="")


class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(64))
    units: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    audio_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


def create_all(engine) -> None:
    Base.metadata.create_all(engine)
