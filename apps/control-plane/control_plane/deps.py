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
                _ensure_column(
                    conn,
                    "call_sessions",
                    "whatsapp_status",
                    "whatsapp_status VARCHAR(16) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "call_sessions",
                    "customer_whatsapp",
                    "customer_whatsapp VARCHAR(64) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "call_sessions",
                    "kind",
                    "kind VARCHAR(32) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "call_sessions",
                    "target_lang",
                    "target_lang VARCHAR(16) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "call_sessions",
                    "session_report",
                    "session_report TEXT",
                )
        except Exception as exc:  # pragma: no cover - sqlite / duplicate column
            print(f"[deps] idempotent column migration skipped: {exc}")

        # ---- 数据迁移：语言值 yue → cantonese 全栈统一（幂等，SQLite/Postgres 通用）。
        # 这是全仓唯一的旧值兼容点：旧库在 CP 启动时一次性落成规范值 cantonese，
        # agent 侧不再保留任何 yue 别名分支（tests/test_cantonese_terminology.py 门禁防复发）。
        try:
            import json as _json

            with engine.begin() as conn:
                for _tbl in (
                    "persona_profiles",
                    "object_profiles",
                    "call_sessions",
                    "conversation_templates",
                ):
                    conn.execute(
                        text(f"UPDATE {_tbl} SET language='cantonese' WHERE language='yue'")
                    )
                # persona reference_audio JSON 的键 yue → cantonese（值=音色 ID 不动）。
                _rows = conn.execute(
                    text("SELECT id, reference_audio FROM persona_profiles WHERE reference_audio LIKE '%yue%'")
                ).fetchall()
                for _rid, _raw in _rows:
                    if not _raw:
                        continue
                    try:
                        _m = _json.loads(_raw) if isinstance(_raw, str) else _raw
                    except Exception:
                        continue
                    if not isinstance(_m, dict) or "yue" not in _m:
                        continue
                    if "cantonese" not in _m:
                        _m["cantonese"] = _m.pop("yue")
                    else:
                        _m.pop("yue", None)
                    conn.execute(
                        text("UPDATE persona_profiles SET reference_audio=:v WHERE id=:id"),
                        {"v": _json.dumps(_m, ensure_ascii=False), "id": _rid},
                    )
                # global_settings 四个 json 列:配置键 speaker_yue → speaker_cantonese。
                # （不做 vad 数值改写——VAD 默认值由代码/EMPTY_FORM 统一，避免启动迁移
                #   把用户有意调过的 vad 值覆盖掉。）
                _scols = (
                    "asr_json",
                    "llm_json",
                    "tts_json",
                    "vad_json",
                )
                for _bk in _scols:
                    _srows = conn.execute(text(f"SELECT id, {_bk} FROM global_settings")).fetchall()
                    for _gid, _raw in _srows:
                        if not _raw:
                            continue
                        try:
                            _b = _json.loads(_raw) if isinstance(_raw, str) else _raw
                        except Exception:
                            continue
                        if not isinstance(_b, dict):
                            continue
                        _changed = False
                        if "speaker_yue" in _b:
                            _b.setdefault("speaker_cantonese", _b.pop("speaker_yue"))
                            _changed = True
                        if _changed:
                            conn.execute(
                                text(f"UPDATE global_settings SET {_bk}=:v WHERE id=:id"),
                                {"v": _json.dumps(_b, ensure_ascii=False), "id": _gid},
                            )
        except Exception as exc:  # pragma: no cover - 数据迁移失败不阻断启动
            print(f"[deps] data migration (yue→cantonese) skipped: {exc}")
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
