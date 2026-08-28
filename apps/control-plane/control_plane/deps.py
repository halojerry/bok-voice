from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from bok_voice_business_db.repository import InMemoryBusinessRepository, SqlAlchemyBusinessRepository


def build_engine() -> Engine | None:
    url = os.environ.get("DATABASE_URL", "")
    if url:
        engine = create_engine(url, future=True)
        from bok_voice_business_db import models

        models.create_all(engine)
        # pgvector: create the extension + knowledge_chunks table (best-effort SQLite-safe).
        try:
            from sqlalchemy import text

            from bok_voice_business_db.vector_models import VectorBase

            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            VectorBase.metadata.create_all(engine)
        except Exception as exc:  # pragma: no cover - sqlite / missing extension
            print(f"[deps] vector schema skipped: {exc}")
        return engine
    return None


def build_repository(engine: Engine | None = None):
    if engine is not None:
        from sqlalchemy.orm import sessionmaker

        session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
        return SqlAlchemyBusinessRepository(session)
    # Default: in-memory repo so the server runs without Postgres for dev/tests.
    return InMemoryBusinessRepository()
