from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase
from pgvector.sqlalchemy import Vector


class VectorBase(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeChunk(VectorBase):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(String(512), default="")
    source: Mapped[str] = mapped_column(String(64), default="import")
    embedding: Mapped[list[float]] = mapped_column(Vector(384))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
