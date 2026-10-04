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
    # 电话边缘站点（spec 2026-09-13 P1.5）：同上，缺表即门禁失败。
    "sip_sites",
    # 跟进工单（漏斗 v2 P1，spec §3.3）：同上，缺表即门禁失败。
    "call_followups",
}


def test_all_tables_declared():
    assert set(Base.metadata.tables.keys()) >= EXPECTED_TABLES


def test_ddl_compiles_on_sqlite_and_postgres():
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        for table in Base.metadata.tables.values():
            CreateTable(table).compile(dialect=dialect)  # 不抛即过


def test_like_wildcard_stays_in_param_value_psycopg_paramstyle():
    """yue→cantonese 迁移的 LIKE 通配必须活在绑定参数值里（2026-10-04 CI 真 PG 回归钉）。

    psycopg3 对带参数的查询在客户端解析 % 占位符——SQL 文本里的字面 '%yue%'
    会被当 '%y' 占位符直接 ProgrammingError（SQLite 不解析所以本地永远绿，
    只有真 PG 的 CI 能抓到）。编译成 pyformat 形态后，文本里只允许出现
    具名占位符 %(pat)s，绝不允许裸 % 通配。"""
    from sqlalchemy import text

    compiled = text(
        "SELECT id, reference_audio FROM persona_profiles WHERE reference_audio LIKE :pat"
    ).compile(dialect=postgresql.dialect())
    s = str(compiled)
    assert "%(pat)s" in s
    assert "%%" not in s and "%y" not in s and "%u" not in s
