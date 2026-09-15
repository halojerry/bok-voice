"""KB 增量索引迁移测试(2026-09-10):content_hash 列补齐+回填+幂等。"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, inspect, text

from control_plane.deps import _migrate_knowledge_content_hash

_OLD_SCHEMA_SQL = """
CREATE TABLE knowledge_chunks (
    id VARCHAR(64) PRIMARY KEY,
    account_id VARCHAR(64),
    text TEXT,
    path VARCHAR(512),
    source VARCHAR(64),
    embedding VECTOR(384),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


def _seed_old_schema() -> "object":
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(_OLD_SCHEMA_SQL))
        conn.execute(
            text(
                "INSERT INTO knowledge_chunks (id, account_id, text, path, source, embedding) "
                "VALUES ('c1', 'acc-1', '段落甲', 'accounts/acc-1/knowledge/a.md', 'import', '')"
            )
        )
    return engine


def _hashes(engine) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, content_hash FROM knowledge_chunks")).all()
    return {rid: h for rid, h in rows}


def test_migration_adds_column_and_backfills() -> None:
    engine = _seed_old_schema()
    assert "content_hash" not in {c["name"] for c in inspect(engine).get_columns("knowledge_chunks")}
    _migrate_knowledge_content_hash(engine)
    hashes = _hashes(engine)
    assert hashes["c1"] == hashlib.sha256("段落甲".encode()).hexdigest()[:32]


def test_migration_is_idempotent() -> None:
    engine = _seed_old_schema()
    _migrate_knowledge_content_hash(engine)
    first = _hashes(engine)
    _migrate_knowledge_content_hash(engine)
    assert _hashes(engine) == first


def test_migration_noop_when_table_absent() -> None:
    engine = create_engine("sqlite:///:memory:")
    _migrate_knowledge_content_hash(engine)  # 不应抛错


def test_migration_keeps_existing_nonempty_hash() -> None:
    engine = _seed_old_schema()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE knowledge_chunks ADD COLUMN content_hash VARCHAR(64) DEFAULT ''"))
        conn.execute(text("UPDATE knowledge_chunks SET content_hash='preset' WHERE id='c1'"))
    _migrate_knowledge_content_hash(engine)
    assert _hashes(engine)["c1"] == "preset"
