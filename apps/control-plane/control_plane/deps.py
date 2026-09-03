from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from bok_voice_business_db.repository import InMemoryBusinessRepository, SqlAlchemyBusinessRepository


def build_engine() -> Engine | None:
    url = os.environ.get("DATABASE_URL", "")
    if url:
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            # 本地单机 SQLite：不要连接池（QueuePool 会在并发下耗尽并 30s 超时）。
            # 每请求独立连接，短事务 + busy timeout，天然规避“pool overflow”类故障。
            from sqlalchemy.pool import NullPool

            kwargs["poolclass"] = NullPool
            kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
        engine = create_engine(url, **kwargs)
        from bok_voice_business_db import models

        models.create_all(engine)
        # create_all 不会给已存在的表加列 —— 幂等补上新增列。注意 SQLite 不支持
        # `ADD COLUMN IF NOT EXISTS`（MySQL 语法，会抛错被吞），必须先查列是否存在。
        try:
            from sqlalchemy import text

            def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
                dialect = engine.dialect.name
                if dialect == "sqlite":
                    exists = any(
                        row[1] == column
                        for row in conn.execute(text(f"PRAGMA table_info({table})"))
                    )
                else:
                    exists = conn.execute(
                        text(
                            "SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = :t AND column_name = :c"
                        ),
                        {"t": table, "c": column},
                    ).first() is not None
                if not exists:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

            with engine.begin() as conn:
                _ensure_column(
                    conn,
                    "object_profiles",
                    "template_id",
                    "template_id VARCHAR(64) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "call_sessions",
                    "template_id",
                    "template_id VARCHAR(64) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "settlements",
                    "summary",
                    "summary TEXT",
                )
                _ensure_column(
                    conn,
                    "persona_profiles",
                    "tts_provider",
                    "tts_provider VARCHAR(32) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "object_profiles",
                    "tracking_no",
                    "tracking_no VARCHAR(64) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "object_profiles",
                    "courier",
                    "courier VARCHAR(64) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "object_profiles",
                    "address",
                    "address VARCHAR(255) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "conversation_templates",
                    "steps_json",
                    "steps_json TEXT",
                )
        except Exception as exc:  # pragma: no cover - sqlite / duplicate column
            print(f"[deps] idempotent column migration skipped: {exc}")
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
