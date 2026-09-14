"""方言兼容门禁（spec 2026-09-10-thin-node-saas §5）：全部 ORM 表必须能对
sqlite 与 postgresql 两种方言编译 DDL。P0 起业务库要能搬 Supabase Postgres，
任何 sqlite 专有类型/语法都会在这道门禁上红。无服务器、纯编译，可进 CI。
"""

from __future__ import annotations

from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql, sqlite

from bok_voice_business_db.models import Base

EXPECTED_TABLES = {
    "accounts", "persona_profiles", "object_profiles", "conversation_templates",
    "object_topics", "call_sessions", "turns", "settlements", "global_insights",
    "global_settings", "audit_events", "conversation_template_revisions",
    "usage_records", "qa_entries", "orgs", "nodes",
    # 外呼战役 + 名册（spec 2026-09-12）：补进下限集，缺表即门禁失败。
    "roster_entries", "campaigns", "campaign_items",
}


def test_all_tables_declared():
    assert set(Base.metadata.tables.keys()) >= EXPECTED_TABLES


def test_ddl_compiles_on_sqlite_and_postgres():
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        for table in Base.metadata.tables.values():
            CreateTable(table).compile(dialect=dialect)  # 不抛即过
