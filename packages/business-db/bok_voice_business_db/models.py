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


class ObjectProfile(Base):
    __tablename__ = "object_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role_template: Mapped[str] = mapped_column(String(64), default="customer")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    background: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")


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
    mode: Mapped[str] = mapped_column(String(32), default="simulation")
    direction: Mapped[str] = mapped_column(String(32), default="webrtc")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    contact_phone: Mapped[str] = mapped_column(String(64), default="")
    consent_status: Mapped[str] = mapped_column(String(32), default="unknown")
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    disposition: Mapped[str] = mapped_column(String(64), default="")
    escalated_to_human: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="idle")
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
