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
        # create_all 不会给已存在的表加列 —— 幂等补上对象卡的模板绑定列。
        try:
            from sqlalchemy import text

            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE object_profiles ADD COLUMN IF NOT EXISTS template_id VARCHAR(64) DEFAULT ''"))
        except Exception as exc:  # pragma: no cover - sqlite / duplicate column
            print(f"[deps] object_profiles.template_id migration skipped: {exc}")
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


def build_session_factory(engine: Engine | None = None):
    """SQL 路径返回 sessionmaker 工厂（每请求独立 Session，避免共享 Session 并发踩踏）。"""
    if engine is None:
        return None
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
