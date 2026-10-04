"""schema 反向 parity（C5，2026-10-04）——CP 启动期补列 _ensure_column ⊆ business-db ORM。

方向说明（两路径的不对称是刻意的）:
- models 声明面 = ``packages/business-db/bok_voice_business_db/models.py``（create_all
  对全新库全量建列）;
- 存量库升级面 = ``apps/control-plane/control_plane/deps.py`` build_engine() 里的
  ``_ensure_column(conn, table, column, ddl)`` 幂等补列（CP 启动期加列的唯一点,
  列序敏感——language 列冻结等历史教训）。
- 正向（models→DDL 产物 vs build_engine 真库）已由 scripts/check_schema_drift.py
  门禁管（真 Postgres 容器级,进不了单测）; **反向缺失面**是本文件:**CP 加了列而
  models 漏声明 = ORM 查询面静默缺列**（历史事故:pronunciation 列 NOT NULL vs
  DEFAULT '' 的 DDL 漂移打红过门禁——同族两路径分叉）。
- 本测试把 deps.py 源码全部 ``_ensure_column(conn, "table", "column", "ddl")`` 的
  字符串字面量实参提取为 (table, column) 对,断言都能在
  ``models.Base.metadata.tables`` 里找到对应列。
- **反方向（models 有而 _ensure_column 无）不要求**:create_all 新库全建列,
  ``_ensure_column`` 只服务旧库迁移,models 允许是超集——该不对称刻意,勿加反向断言。
- 动态实参（变量表名/列名）当前不存在;提取锚点=字符串字面量,另有
  「调用点计数 == 字面量提取对数」与代表对下限的守卫,将来出现非字面量调用或
  锚点形状漂移即红,提示人工扩展而非静默空转。
- 范围外（相邻迁移路径,不在本测试断言面）:deps.py ``_migrate_knowledge_content_hash``
  的 ``ALTER TABLE knowledge_chunks ADD COLUMN content_hash`` 是独立函数（声明在
  ``vector_models`` 的独立 metadata,不在 models.Base 内）——若未来也要反向 parity,
  需单开 vector models 面。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPS = ROOT / "apps" / "control-plane" / "control_plane" / "deps.py"

# 调用点锚点（含 def 行;拿总数减去 def 数 = 调用数）。
_CALL_RE = re.compile(r"(?<![\w.])_ensure_column\(")
_DEF_RE = re.compile(r"def _ensure_column\(")
# 字面量实参锚点:`conn, "table", "column", "ddl"`（多行/单行皆可;ddl 允许跨行）。
_LITERAL_CALL_RE = re.compile(
    r'_ensure_column\(\s*conn\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]*)"',
    re.S,
)

# 当前提取对数下限（2026-10-04 实测 58 对）:防锚点失效后空转绿。
_MIN_EXPECTED_PAIRS = 50
# 代表对（每张被补列表至少一条）:锚点/正则被重构时最先说话的哨兵。
_PINNED_PAIRS = (
    ("object_profiles", "pronunciation"),
    ("conversation_templates", "published_json"),
    ("call_sessions", "assist_status"),
    ("qa_entries", "hit_threshold"),
    ("global_settings", "model_routing_json"),
    ("turns", "perceived_ms"),
    ("campaigns", "redispatch_json"),
    ("nodes", "revoked_at"),
    ("users", "permissions_json"),
    ("accounts", "org_id"),
    ("persona_profiles", "tts_provider"),
    ("settlements", "summary"),
)


def _ensure_column_calls() -> list[tuple[str, str, str]]:
    """deps.py 源码 → [(table, column, ddl), ...]（仅字符串字面量实参）。"""
    src = DEPS.read_text(encoding="utf-8")
    calls = _CALL_RE.findall(src)
    defs = _DEF_RE.findall(src)
    assert len(defs) == 1, f"deps.py 的 _ensure_column 函数定义应恰 1 处,实际 {len(defs)}"
    literal = _LITERAL_CALL_RE.findall(src)
    assert len(calls) - len(defs) == len(literal), (
        "存在非字面量 _ensure_column 实参（变量表名/列名或形状漂移）:"
        f" 调用点={len(calls) - len(defs)} 字面量提取={len(literal)}"
        "——请扩展本测试的提取锚点（动态表名需单独处理）"
    )
    return literal


# ---- 1. 提取锚点自检:数量/代表对/重复 ----


def test_extraction_anchor_sees_all_literal_calls():
    triples = _ensure_column_calls()
    assert len(triples) >= _MIN_EXPECTED_PAIRS, (
        f"仅提取到 {len(triples)} 对 _ensure_column 实参（下限 {_MIN_EXPECTED_PAIRS}）"
        "——锚点可能失效,本 pin 不能空转绿"
    )
    pairs = [(t, c) for t, c, _ in triples]
    assert len(set(pairs)) == len(pairs), "同 (表, 列) 被 _ensure_column 重复补列"
    missing_pins = [p for p in _PINNED_PAIRS if p not in set(pairs)]
    assert not missing_pins, f"代表对缺失（提取锚点漂移）: {missing_pins}"


def test_ddl_leading_token_matches_column_name():
    """DDL 串首 token 必须等于 column 实参——防复制粘贴造成「补 A 列却写 B 列 DDL」。"""
    bad = [
        (t, c, ddl)
        for t, c, ddl in _ensure_column_calls()
        if not ddl.split() or ddl.split()[0] != c
    ]
    assert not bad, f"_ensure_column 的 DDL 首 token 与列名不一致: {bad}"


# ---- 2. 反向 parity:CP 补列 ⊆ ORM models ----


def test_every_ensure_column_exists_in_orm_models():
    from bok_voice_business_db.models import Base

    tables = Base.metadata.tables
    missing_tables: list[str] = []
    missing_columns: list[tuple[str, str]] = []
    for table, column, _ddl in _ensure_column_calls():
        t = tables.get(table)
        if t is None:
            missing_tables.append(table)
        elif column not in t.columns:
            missing_columns.append((table, column))
    assert not missing_tables, (
        "_ensure_column 补列的表在 ORM models 中不存在:"
        f" {sorted(set(missing_tables))}"
    )
    assert not missing_columns, (
        "反向 parity 缺口（CP _ensure_column 有列而 models 漏声明 = ORM 查询面静默"
        f"缺列）: {missing_columns}"
    )
