#!/usr/bin/env python3
"""真实 Postgres 冒烟（P0 分发型拓扑回归闸门，2026-09-15）。

为什么存在：tests/test_db_portability.py 只把全部表编译成 sqlite/postgres 两种
方言的 DDL——纯静态、不连库；而业务库「SQL 方言可移植」是 AGENTS.md「分发型
拓扑」条的硬约束（新列/新表禁用 sqlite 专有语法）。本脚本把「真 Postgres 上
建表 + 幂等补列迁移 + 全量 CRUD 回程」真正跑一遍，把静态门禁漏掉的运行时方言
差异（长度约束/类型强制/多 schema/事务语义）挡在合入前。

三段覆盖：
  ① build_engine 幂等：连跑两次（create_all / 补列 / yue→cantonese 数据迁移 /
     垫话种子），断言核心表与迁移列在真库上确实存在，且没有任何被 catch-all
     吞掉的「[deps] ... skipped」静默降级；
  ② 存量库升级演练：临时库（用后即焚，绝不碰共享库）里 DROP 掉迁移新增列并铺
     一层「同名诱饵 schema」，再跑 build_engine——验证 ALTER TABLE ADD COLUMN
     真的执行、且存在性探测按 schema 收窄（不被别的 schema 同名表骗过）；
  ③ CRUD 回程：users/对象/话术/快答/通话/轮次/名册/战役/审计全表写一轮，换一个
     全新 Session 复核落库值（绕开身份映射缓存，是真 DB 往返），长值顺带验
     VARCHAR 长度约束（SQLite 不校验长度、PG 会截断报错——经典方言差异）。

用法（本机 Docker）：
    docker run -d --name pg-smoke -p 127.0.0.1:5433:5432 \\
        -e POSTGRES_PASSWORD=postgres postgres:16
    DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5433/postgres \\
        .venv312/bin/python scripts/smoke_postgres.py

退出码：0=SMOKE OK；1=断言失败（=真方言 bug）；2=环境不齐（缺/错 DATABASE_URL）。
脚本自身幂等：所有行 id 带 uuid 后缀，不删表、不删库、不动共享 schema——
唯一的临时库（升级演练用）名字带 uuid 且用后 DROP。
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import time
import uuid
from typing import Any

# 本地裸 venv（未 pip install 各包）也能跑：与 tests/conftest.py 同款路径自举；
# CI 里 workflow 已 pip install -e 各包，插入重复路径是无害 no-op。
from pathlib import Path as _Path

for _part in (
    "packages/core",
    "packages/business-db",
    "packages/knowledge",
    "packages/observability",
    "apps/control-plane",
):
    _p = str(_Path(__file__).resolve().parents[1] / _part)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# 常量：迁移列清单 / 业务表下限集
# ---------------------------------------------------------------------------

# 迁移列清单：与 apps/control-plane/control_plane/deps.py build_engine() 里的
# _ensure_column(...) 逐条对齐。deps 加列时这里要同步加——升级演练靠它把列
# DROP 掉造「存量旧库」，漏一条就少验一条。
_MIGRATION_COLUMNS: dict[str, tuple[str, ...]] = {
    "accounts": ("org_id",),
    "persona_profiles": ("tts_provider",),
    "object_profiles": (
        "template_id", "digest", "tracking_no", "courier", "contact_channel", "address",
    ),
    # graph_json：话术图（2026-09-18 qa-flow-graph Phase 2）；
    # published_json：发布冻结快照（2026-09-19 W2-T1 模板发布两态）。
    "conversation_templates": ("steps_json", "hotwords", "owner_user_id", "graph_json", "published_json"),
    "call_sessions": (
        "template_id", "whatsapp_status", "customer_whatsapp", "kind",
        "target_lang", "session_report", "created_by",
        # 仪表盘时长统计（2026-09-17 campaign-scheduling-dashboard Task 1）：
        # TIMESTAMP 列的真库 ALTER 路径只有这里验得到。
        "started_at", "ended_at", "duration_s",
    ),
    "settlements": ("summary",),
    "turns": (
        "language", "org_id", "line", "speaker", "gen",
        "template_step", "started_ms", "ended_ms", "perceived_ms",
    ),
    "global_settings": ("sip_json",),
    # 战役调度三字段（2026-09-17 campaign-scheduling-dashboard Task 1）。
    "campaigns": ("scripts_json", "call_windows_json", "max_concurrency", "redispatch_json"),
    # cluster_head_id：同义簇（qa-canvas Phase 1）；priority：匹配优先级（Phase 3.1）。
    "qa_entries": ("owner_user_id", "cluster_head_id", "priority"),
    "users": ("permissions_json",),
}

# 业务表下限集：与 tests/test_db_portability.py EXPECTED_TABLES 同源（+垫话罐头表，
# 它由 create_all 建、deps 只负责灌种子）。
_EXPECTED_TABLES: frozenset[str] = frozenset({
    "accounts", "persona_profiles", "object_profiles", "conversation_templates",
    "object_topics", "call_sessions", "turns", "settlements", "global_insights",
    "global_settings", "audit_events", "conversation_template_revisions",
    "usage_records", "qa_entries", "orgs", "nodes", "filler_entries",
    "roster_entries", "campaigns", "campaign_items", "users",
})


class SmokeFailure(RuntimeError):
    """断言失败 = 真方言 bug（退出码 1）。"""


class SmokeSkipped(RuntimeError):
    """环境不支持某段演练 → 降级为 WARNING，不算失败。"""


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", flush=True)


def _expect(cond: bool, what: str) -> None:
    if not cond:
        raise SmokeFailure(what)
    print(f"  [ok] {what}", flush=True)


# ---------------------------------------------------------------------------
# ① build_engine 幂等 + schema 落地检查
# ---------------------------------------------------------------------------

def _build_engine_captured(url: str | None = None) -> tuple[Any, list[str]]:
    """跑 deps.build_engine()，捕获它打到 stdout 的 [deps] 诊断行。

    deps 的迁移块都是 `except Exception: print(...)` 兜底——静默降级在 SQLite 上
    无所谓，在 Postgres 上等于「补列/数据迁移没跑」却不报错。冒烟必须把那些行
    捞出来看，而不是只信「没抛异常」。
    """
    from control_plane.deps import build_engine

    saved = os.environ.get("DATABASE_URL")
    if url is not None:
        os.environ["DATABASE_URL"] = url
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            engine = build_engine()
    finally:
        if url is not None:
            if saved is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = saved
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return engine, lines


def _check_deps_diagnostics(lines: list[str]) -> None:
    """[deps] 诊断行分类：只允许 pgvector 缺失（本冒烟不在 P0 范围）。"""
    allowed = ("vector schema skipped",)
    for line in lines:
        print(f"  [deps] {line}")
        if "[deps]" not in line:
            continue
        if any(token in line for token in allowed):
            _warn(f"pgvector 未启用（vanilla postgres:16 无 vector 扩展）: {line}")
            continue
        # 补列迁移/数据迁移/垫话种子被吞 = PG 上静默降级，直接判失败。
        if "seeded rows=" in line:
            continue
        raise SmokeFailure(f"build_engine 出现被吞掉的迁移失败（PG 上静默降级）: {line}")


def _check_schema(engine: Any) -> None:
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    tables = set(insp.get_table_names())
    missing = sorted(_EXPECTED_TABLES - tables)
    _expect(not missing, f"create_all 建齐 {len(_EXPECTED_TABLES)} 张业务表（缺 {missing}）")
    for table, columns in _MIGRATION_COLUMNS.items():
        have = {c["name"] for c in insp.get_columns(table)}
        missing_cols = [c for c in columns if c not in have]
        _expect(not missing_cols, f"{table} 迁移列齐备（缺 {missing_cols}）")


def _report_pgvector(engine: Any) -> None:
    """pgvector 单独报：缺扩展=WARNING；扩展在但表没建=真 bug（判失败）。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        has_ext = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first() is not None
        kb_table = conn.execute(text("SELECT to_regclass('public.knowledge_chunks')")).scalar()
    if has_ext and kb_table:
        print("  [ok] pgvector: 扩展 + knowledge_chunks 均就位")
        return
    if has_ext and not kb_table:
        raise SmokeFailure("pgvector 扩展可用但 knowledge_chunks 未建（deps 的向量建表被吞）")
    _warn(
        "pgvector: 扩展缺席（vanilla postgres:16）—— knowledge_chunks 未建，"
        "向量检索不在 P0 冒烟范围；deps.build_engine 已 best-effort 跳过"
    )


# ---------------------------------------------------------------------------
# ② 存量库升级演练（临时库，用后即焚）
# ---------------------------------------------------------------------------

def _migration_upgrade_probe(url: str) -> None:
    """在独立临时库里验「存量旧库 → build_engine 补列」这条升级路径。

    做法：先让当前代码把新 schema 整个建出来 → DROP 掉全部迁移新增列（=旧库形状）
    → 在旁路 schema 里铺同名同列表（诱饵）→ 再跑一次 build_engine → 断言列都回来了。

    诱饵的意义：deps._ensure_column 早期实现用不带 schema 过滤的
    information_schema 查询判存在性，多 schema 库（共享 PG 实例、Supabase 的
    auth/storage schema、并排的 staging schema）会被同名表骗过 → 真列不补、
    升级静默丢列。临时库用 uuid 命名，绝不碰共享库。
    """
    from sqlalchemy import create_engine, inspect as sa_inspect, text
    from sqlalchemy.engine.url import make_url
    from sqlalchemy.pool import NullPool

    probe_db = f"bok_smoke_probe_{uuid.uuid4().hex[:10]}"
    probe_url = make_url(url).set(database=probe_db).render_as_string(hide_password=False)
    admin = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    created = False
    first_engine = second_engine = None
    try:
        try:
            with admin.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{probe_db}"'))
            created = True
        except Exception as exc:  # 无 CREATEDB 权限的共享库：降级跳过
            raise SmokeSkipped(f"当前 DB 用户不能建临时库（{type(exc).__name__}），升级演练跳过") from exc

        # (1) 先建全新 schema（唯一一次用当前模型建表）
        first_engine, first_lines = _build_engine_captured(probe_url)
        _expect(first_engine is not None, "升级演练：临时库首次建表成功")
        _check_deps_diagnostics(first_lines)

        work = create_engine(probe_url, poolclass=NullPool)
        try:
            with work.begin() as conn:
                # (2) 造「旧库」：把迁移新增列统统 DROP（含 NOT NULL DEFAULT '' 的 sip_json，
                #     顺带留一行存量数据，验带数据 ALTER 的回填）。
                #     注意 ORM 的 default= 是 Python 侧默认，裸 SQL 必须显式给值。
                conn.execute(text(
                    "INSERT INTO public.global_settings"
                    " (id, asr_json, llm_json, tts_json, vad_json, sip_json, policy, updated_at)"
                    " VALUES ('global', '{\"speaker_yue\": \"old\"}', '{}', '{}', '{}',"
                    " '{\"mode\": \"mock\"}', 'offline_first', CURRENT_TIMESTAMP)"
                ))
                for table, columns in _MIGRATION_COLUMNS.items():
                    for column in columns:
                        conn.execute(text(f'ALTER TABLE public."{table}" DROP COLUMN IF EXISTS "{column}"'))
                # (3) 存量脏数据：语言旧值 yue + reference_audio 旧键——第二次
                #     build_engine 的数据迁移必须改写它们（方言无关，但只在真库上验得到）。
                conn.execute(text(
                    "INSERT INTO public.persona_profiles"
                    " (id, account_id, name, company, tone, language, reference_audio)"
                    " VALUES ('probe-persona', 'probe', '旧人设', '', '', 'yue',"
                    " '{\"yue\": \"Cantonese_crisp_news_anchor_vv2\"}')"
                ))
                # (4) 诱饵 schema：同名表 + 迁移列齐全
                conn.execute(text("CREATE SCHEMA smoke_decoy"))
                for table, columns in _MIGRATION_COLUMNS.items():
                    col_ddl = ", ".join(f'"{c}" VARCHAR(64)' for c in columns)
                    conn.execute(text(
                        f'CREATE TABLE smoke_decoy."{table}" (id VARCHAR(64) PRIMARY KEY, {col_ddl})'
                    ))
        finally:
            work.dispose()

        # (5) 再跑 build_engine：应把列全部补回 public（诱饵不该骗过存在性探测），
        #     并把存量脏数据迁移成规范值。
        second_engine, second_lines = _build_engine_captured(probe_url)
        _check_deps_diagnostics(second_lines)
        insp = sa_inspect(second_engine)
        for table, columns in _MIGRATION_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table, schema="public")}
            missing = [c for c in columns if c not in have]
            _expect(not missing, f"升级演练：public.{table} 补列成功（缺 {missing}）")
        with second_engine.connect() as conn:
            sip = conn.execute(text("SELECT sip_json FROM public.global_settings WHERE id='global'")).scalar()
            persona = conn.execute(text(
                "SELECT language, reference_audio FROM public.persona_profiles WHERE id='probe-persona'"
            )).one()
            asr_json = conn.execute(text("SELECT asr_json FROM public.global_settings WHERE id='global'")).scalar()
        _expect(sip == "", f"升级演练：NOT NULL DEFAULT '' 列对存量行回填（sip_json={sip!r}）")
        _expect(
            persona[0] == "cantonese" and "cantonese" in (persona[1] or ""),
            f"升级演练：language/reference_audio 旧值 yue→cantonese（实得 {persona!r}）",
        )
        _expect(
            "speaker_cantonese" in (asr_json or ""),
            f"升级演练：全局设置 speaker_yue→speaker_cantonese（实得 {asr_json!r}）",
        )
    finally:
        for eng in (first_engine, second_engine):
            if eng is not None:
                with contextlib.suppress(Exception):
                    eng.dispose()
        if created:
            with contextlib.suppress(Exception), admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{probe_db}" WITH (FORCE)'))
        admin.dispose()


# ---------------------------------------------------------------------------
# ③ CRUD 回程（写一轮 + 换新 Session 复核）
# ---------------------------------------------------------------------------

def _crud_roundtrip(engine: Any, summary: list[tuple[str, int, str]]) -> None:
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository
    from bok_voice_core.policies import select_session_manifest
    from bok_voice_core.types import CallMode, TurnEvent
    from control_plane.auth import hash_password
    from control_plane.deps import build_repository, build_session_factory

    run = uuid.uuid4().hex[:10]
    acct = f"smoke-acct-{run}"
    write = build_repository(engine)

    # ---- users：password_hash + permissions_json 双字段回程（SQL 库登录依赖）----
    username = f"smoke-{run}"
    pw_hash = hash_password("Smoke-Passw0rd!")
    perms = '["calls", "qa"]'
    user = write.create_user(
        username=username, password_hash=pw_hash, role="user", account_id=acct,
        display_name=f"冒烟话务员-{run}", permissions_json=perms,
    )

    # ---- conversation_templates：owner_user_id（B3 话务员级归属）----
    tpl = write.create_template({
        "id": f"tpl-smoke-{run}", "account_id": acct, "name": f"冒烟话术-{run}",
        "language": "zh", "owner_user_id": user["id"],
        "steps_json": '[{"goal": "确认身份", "ref": "你好"}]', "hotwords": "顺丰,单号",
    })

    # ---- qa_entries：owner_user_id + bool/int 列 ----
    qa = write.create_qa_entry({
        "id": f"qa-smoke-{run}", "account_id": acct, "owner_user_id": user["id"],
        "question_text": "怎么赔", "answer_text": "我帮您登记", "lang": "zh",
        "scope": "step", "step_index": 3, "enabled": False, "source": "mined",
    })

    # ---- object_profiles：快递场景变量列 + 长值（VARCHAR 长度约束只在 PG 生效）----
    long_name = "冒烟客户名" * 50  # 250 字 < String(255)
    obj = write.create_object(acct, {
        "id": f"obj-smoke-{run}", "display_name": long_name, "language": "zh",
        "phone": "+85211112222", "tracking_no": "SF" + run.upper(), "courier": "顺丰",
        "address": "香港湾仔活道 " + run, "contact_channel": "whatsapp",
        "template_id": tpl["id"], "background": "postgres 冒烟对象",
    })

    # ---- call_sessions：建单快照（created_by + template_id 双盖章）----
    manifest = select_session_manifest(
        session_id=f"call-smoke-{run}", account_id=acct, object_id=obj["id"],
        persona_id="", mode=CallMode.LIVE, language="zh",
        template_id=tpl["id"], created_by=user["id"],
    )
    call = write.create_call(manifest)

    # ---- turns：分析账本七列（line/speaker/gen/template_step/started/ended/perceived）----
    write.create_turn(TurnEvent(
        trace_id=call["id"], call_id=call["id"], turn_id="t0", role="customer",
        transcript="你好，我个快递三日都未到", emotion="neutral", provider="qwen3_asr",
        latency_ms=420, language="cantonese", org_id="", line="a", speaker="customer",
        gen="", template_step=0, started_ms=100, ended_ms=520, perceived_ms=0,
    ))
    write.create_turn(TurnEvent(
        trace_id=call["id"], call_id=call["id"], turn_id="t1", role="assistant",
        transcript="唔好意思，我帮您查下", emotion="neutral", provider="minimax",
        latency_ms=880, language="cantonese", org_id="", line="a", speaker="agent_ai",
        gen="llm", template_step=2, started_ms=900, ended_ms=1780, perceived_ms=1320,
    ))

    # ---- roster_entries：认领池（status/claimed_by/claimed_at 读改写回程）----
    roster = write.upsert_roster_entry(
        account_id=acct, call_id=call["id"], object_id=obj["id"], channel="whatsapp",
        number="8529" + run[:6], display_name=long_name, summary=f"冒烟名册 {run}",
    )
    write.update_roster_entry(
        roster["id"], status="claimed", claimed_by=user["id"],
        claimed_at="2026-09-15T00:00:00+00:00",
    )

    # ---- campaigns / campaign_items：外呼战役 + 名单项 + 脚本 JSON ----
    campaign = write.create_campaign(
        acct, name=f"冒烟战役-{run}", template_id=tpl["id"], persona_id="",
        language="zh", gap_seconds=3, object_ids=[obj["id"]],
        scripts={obj["id"]: ["演练台词一", "演练台词二"]},
    )
    items = write.list_items(campaign["id"])
    item = items[0]
    write.update_item(item["id"], status="done", call_id=call["id"])

    # ---- audit_events：追加 + 按账号/动作过滤列出 ----
    event_id = f"audit-smoke-{run}"
    ts = "2026-09-15T12:34:56.123456+00:00"  # 32 字 < String(40)
    write.append_audit({
        "event_id": event_id, "ts": ts, "action": "smoke.postgres", "subject_type": "smoke",
        "subject_id": run, "actor": "smoke", "outcome": "ok",
        "detail": {"run": run, "dialect": "postgresql"},
        "request_id": f"req-{run}", "call_id": call["id"], "account_id": acct,
        "object_id": obj["id"], "persona_id": "",
    })

    # ==== 复核：换全新 Session（绕开身份映射缓存 = 真 DB 往返）====
    fresh = SqlAlchemyBusinessRepository(build_session_factory(engine)())

    got_user = fresh.get_user_by_username(username)
    _expect(got_user is not None, "users: get_user_by_username 找得到冒烟用户")
    assert got_user is not None
    _expect(got_user["password_hash"] == pw_hash, "users.password_hash 回程一致")
    _expect(got_user["permissions_json"] == perms, "users.permissions_json 回程一致")
    _expect(got_user["role"] == "user" and got_user["account_id"] == acct, "users.role/account_id 回程一致")

    got_obj = fresh.get_object(obj["id"])
    assert got_obj is not None
    _expect(got_obj["display_name"] == long_name, "object_profiles.display_name 长值未被截断")
    _expect(
        (got_obj["courier"], got_obj["tracking_no"], got_obj["contact_channel"], got_obj["template_id"])
        == ("顺丰", obj["tracking_no"], "whatsapp", tpl["id"]),
        "object_profiles 快递变量+template_id 回程一致",
    )

    got_tpl = fresh.get_template(tpl["id"])
    assert got_tpl is not None
    _expect(got_tpl["owner_user_id"] == user["id"], "conversation_templates.owner_user_id 回程一致")
    _expect(got_tpl["hotwords"] == "顺丰,单号", "conversation_templates.hotwords 回程一致")
    _expect(
        {t["id"] for t in fresh.list_templates(acct, owner_scope=user["id"])} == {tpl["id"]},
        "conversation_templates owner_scope 收窄检索命中本人条目",
    )

    got_qa = fresh.get_qa_entry(qa["id"])
    assert got_qa is not None
    _expect(got_qa["owner_user_id"] == user["id"], "qa_entries.owner_user_id 回程一致")
    _expect(got_qa["step_index"] == 3 and got_qa["enabled"] is False, "qa_entries int/bool 列回程一致")
    _expect(
        {q["id"] for q in fresh.list_qa_entries(acct, enabled=False, owner_scope=user["id"])} == {qa["id"]},
        "qa_entries owner_scope + enabled 组合检索命中",
    )

    got_call = fresh.get_call(call["id"])
    assert got_call is not None
    _expect(got_call["created_by"] == user["id"], "call_sessions.created_by 建单盖章回程一致")
    _expect(got_call["template_id"] == tpl["id"], "call_sessions.template_id 建单快照回程一致")
    _expect(got_call["status"] == "ringing", "call_sessions.status 回程一致")

    turns = fresh.get_turns(call["id"])
    _expect(len(turns) == 2, f"turns: 落库 2 轮（实际 {len(turns)}）")
    by_id = {t.turn_id: t for t in turns}
    t1 = by_id["t1"]
    _expect(
        (t1.line, t1.speaker, t1.gen, t1.template_step, t1.started_ms, t1.ended_ms, t1.perceived_ms)
        == ("a", "agent_ai", "llm", 2, 900, 1780, 1320),
        "turns 分析账本七列回程一致（line/speaker/gen/step/started/ended/perceived）",
    )
    _expect(by_id["t0"].language == "cantonese", "turns.language 回程一致（cantonese 规范值）")

    got_roster = fresh.get_roster_entry(roster["id"])
    assert got_roster is not None
    _expect(got_roster["status"] == "claimed", "roster_entries.status 认领状态回程一致")
    _expect(got_roster["claimed_by"] == user["id"], "roster_entries.claimed_by 回程一致")
    _expect(
        str(got_roster["claimed_at"]).startswith("2026-09-15T00:00:00"),
        f"roster_entries.claimed_at ISO 字符串读改写回程一致（{got_roster['claimed_at']}）",
    )

    got_campaign = fresh.get_campaign(campaign["id"])
    assert got_campaign is not None
    _expect(got_campaign["gap_seconds"] == 3, "campaigns.gap_seconds int 列回程一致")
    _expect(
        fresh.get_campaign_scripts(campaign["id"]) == {obj["id"]: ["演练台词一", "演练台词二"]},
        "campaigns.scripts_json JSON 回程一致",
    )
    got_item = fresh.get_item(item["id"])
    assert got_item is not None
    _expect(
        (got_item["status"], got_item["call_id"], got_item["seq"]) == ("done", call["id"], 0),
        "campaign_items 更新回程一致（status/call_id/seq）",
    )

    events = fresh.list_audit_events(account_id=acct, action="smoke.postgres")
    hit = [e for e in events if e["id"] == event_id]
    _expect(len(hit) == 1, f"audit_events 按账号+动作列出命中 1 条（实际 {len(hit)}）")
    _expect(hit[0]["detail"] == {"run": run, "dialect": "postgresql"}, "audit_events.detail JSON 回程一致")
    _expect(hit[0]["ts"] == ts, "audit_events.ts 字符串主键列回程一致")

    # ==== 次级路径：同一批接线在 PG 上的事务/聚合语义（sqlite 与 PG 分叉高发区）====
    # 垫话种子必须真的落库：seed 里的 enabled 布尔字面量曾在 PG 上整块被吞（见 deps.py）。
    filler_n = fresh.count_filler_entries()
    _expect(filler_n > 0, f"filler_entries 垫话种子真落库（{filler_n} 条）")

    # 全局设置：JSON blob 列 + sip_json（NOT NULL DEFAULT ''）读写回程
    write.save_settings(write.default_settings())
    settings = fresh.get_settings()
    _expect(
        settings["sip"]["mode"] == "mock" and settings["asr"]["provider"] == "qwen3_asr",
        "global_settings JSON 段（sip/asr）读写回程一致",
    )

    # 结算：JSON 列 + upsert 同键二写
    write.append_settlement(call["id"], {
        "status": "done", "metrics": {"turns": 2}, "summary": f"冒烟结算 {run}",
    })
    settlement = fresh.get_settlement(call["id"])
    assert settlement is not None
    _expect(
        settlement["metrics"] == {"turns": 2} and settlement["summary"] == f"冒烟结算 {run}",
        "settlements metrics_json/summary 回程一致",
    )

    # 话术版本：同 (template_id, revision) 二写走 IntegrityError→rollback 分支
    # （PG 的失败语句会毒化事务，rollback 后必须还能继续用同一 Session）
    first_rev = write.append_template_revision(tpl["id"], 1, '{"v": 1}')
    dup_rev = write.append_template_revision(tpl["id"], 1, '{"v": 1}')
    _expect(
        first_rev.get("revision") == 1 and dup_rev.get("duplicate") is True,
        "template_revisions 唯一键冲突回滚后事务仍可用（PG 事务语义）",
    )
    _expect(
        len(fresh.list_template_revisions(tpl["id"])) == 1,
        "template_revisions 未重复落库",
    )

    # 轮次聚合：PG 的 avg() 返回 Decimal，代码侧 int(float(...)) 必须不炸
    stats = fresh.turn_stats()
    _expect(
        stats.get(call["id"]) == {"turns": 2, "avg_latency_ms": 650},
        f"turn_stats 聚合正确（Decimal→int，实得 {stats.get(call['id'])}）",
    )

    # 跨表 join 的挖掘查询（turns ⨝ call_sessions ⨝ object_profiles，含 outerjoin）
    convs = fresh.iter_call_conversations(acct)
    _expect(
        len(convs) == 1 and [t["role"] for t in convs[0]] == ["customer", "assistant"],
        "iter_call_conversations 跨表 join 命中本通话两轮",
    )

    # 终态守卫：条件 UPDATE 的 rowcount 在 PG 上必须真实反映「有没有改行」
    _expect(fresh.mark_active_if_live(call["id"]) is True, "mark_active_if_live: ringing→active 改到行")
    _expect(fresh.get_call(call["id"])["status"] == "active", "mark_active_if_live 状态落库")

    # 删除级联：通话删除连带 turns/settlements，PG 上 DELETE 语义一致
    doomed = write.create_call(select_session_manifest(
        session_id=f"call-smoke-gone-{run}", account_id=acct, object_id=obj["id"],
        persona_id="", mode=CallMode.LIVE, language="zh", created_by=user["id"],
    ))
    write.update_call(doomed["id"], status="ended")
    _expect(
        fresh.mark_active_if_live(doomed["id"]) is False,
        "mark_active_if_live: ended 终态守卫拒绝复活（rowcount=0）",
    )
    write.create_turn(TurnEvent(
        trace_id=doomed["id"], call_id=doomed["id"], turn_id="t0", role="customer",
        transcript="待删", line="a", speaker="customer",
    ))
    _expect(fresh.delete_call(doomed["id"]) is True, "delete_call 级联删除返回 True")
    _expect(fresh.get_call(doomed["id"]) is None and fresh.get_turns(doomed["id"]) == [], "delete_call 连带清掉通话与轮次")

    summary.extend([
        ("users", 1, "password_hash+permissions_json"),
        ("conversation_templates", 1, "owner_user_id+hotwords"),
        ("qa_entries", 1, "owner_user_id+bool/int"),
        ("object_profiles", 1, "快递变量+255 字长值"),
        ("call_sessions", 1, "created_by+template_id"),
        ("turns", 2, "分析账本七列"),
        ("roster_entries", 1, "claimed 读改写"),
        ("campaigns", 1, "scripts_json"),
        ("campaign_items", 1, "status/call_id"),
        ("audit_events", 1, "detail JSON"),
        ("(secondary paths)", 8, "设置/结算/版本冲突/聚合/守卫/级联"),
    ])
    print(f"  [ok] CRUD 回程 10 张表 + 8 条次级路径全部一致（run={run} account={acct}）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _run(url: str) -> int:
    from sqlalchemy import text

    summary: list[tuple[str, int, str]] = []
    started = time.time()

    print(f"[phase 1] build_engine 幂等 + schema 落地  ({url.split('@')[-1]})")
    engine, lines = _build_engine_captured()
    _expect(engine is not None, "deps.build_engine() 返回 engine")
    _check_deps_diagnostics(lines)
    engine2, lines2 = _build_engine_captured()
    _expect(engine2 is not None, "deps.build_engine() 第二次仍返回 engine（幂等）")
    _check_deps_diagnostics(lines2)
    with engine.connect() as conn:
        print(f"  [info] server: {conn.execute(text('SELECT version()')).scalar()}")
    _check_schema(engine)
    _report_pgvector(engine)

    print("[phase 2] 存量库升级演练（临时库，用后即焚）")
    try:
        _migration_upgrade_probe(url)
        print("  [ok] 补列迁移在真 Postgres 上执行且按 schema 收窄")
        summary.append(("(migration probe)", len(_MIGRATION_COLUMNS), f"{len(_MIGRATION_COLUMNS)} 表补列+数据迁移"))
    except SmokeSkipped as exc:
        _warn(str(exc))
        summary.append(("(migration probe)", 0, "skipped"))

    print("[phase 3] CRUD 回程（写一轮 + 新 Session 复核）")
    _crud_roundtrip(engine, summary)

    engine.dispose()
    if engine2 is not None:
        engine2.dispose()

    print("\n================ POSTGRES SMOKE SUMMARY ================")
    print(f"{'table / check':<28}{'rows':>5}  verification")
    for table, rows, note in summary:
        print(f"{table:<28}{rows:>5}  {note}")
    print(f"{'elapsed':<28}{time.time() - started:>5.1f}s")
    print("========================================================")
    print("SMOKE OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if argv and argv[0] not in ("-h", "--help"):
        url = argv[0].strip()
    if not url:
        print(
            "FATAL: 缺 DATABASE_URL（示例 "
            "postgresql+psycopg://postgres:postgres@127.0.0.1:5433/postgres）"
        )
        return 2
    # 只认 psycopg 3 驱动：postgresql:// 会被 SQLAlchemy 解析成 psycopg2（本仓不装）。
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not url.startswith("postgresql+psycopg://"):
        print(f"FATAL: 本冒烟只跑 Postgres（需要 postgresql+psycopg://），收到 {url.split('://')[0]}://")
        return 2
    os.environ["DATABASE_URL"] = url

    try:
        return _run(url)
    except SmokeFailure as exc:
        print(f"\nSMOKE FAILED: {exc}", flush=True)
        return 1
    except Exception as exc:  # 连接不上/驱动缺失等环境问题
        print(f"\nSMOKE ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
