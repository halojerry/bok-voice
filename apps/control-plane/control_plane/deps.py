from __future__ import annotations

import json as _json
import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from bok_voice_business_db.repository import InMemoryBusinessRepository, SqlAlchemyBusinessRepository

# 垫话罐头种子(2026-09-13 乙节 B5):三语×六场景。trigger=用户口吻示例句(取材
# 9/9-9/12 285 条实机配对的高频问法),text=该场景确定性垫话。同场景多 text 变体
# 的意图:per_call_cap 撞顶后的第二选择,不是同通随机。
_FILLER_SEEDS: list[dict] = [
    # ---- zh ----
    {"id": "filler:zh-comp-1", "lang": "zh", "category": "compensate", "text": "嗯……怎么赔，我给您讲。", "triggers": _json.dumps(["那要怎么赔给我呢", "怎么赔偿", "能赔多少钱", "你们是怎么赔偿方式", "赔给我"], ensure_ascii=False), "priority": 5, "cap": 2},
    {"id": "filler:zh-comp-2", "lang": "zh", "category": "compensate", "text": "嗯，等我先跟您说下赔法。", "triggers": _json.dumps(["怎么个赔法", "赔偿标准是什么", "按什么赔"], ensure_ascii=False), "priority": 3, "cap": 2},
    {"id": "filler:zh-check-1", "lang": "zh", "category": "check", "text": "嗯……我看一下。", "triggers": _json.dumps(["帮我查一下", "快递三天了还没到", "查一下我的快递", "到哪里了"], ensure_ascii=False), "priority": 4, "cap": 2},
    {"id": "filler:zh-check-2", "lang": "zh", "category": "check", "text": "嗯，收到了，我查着了。", "triggers": _json.dumps(["还没收到货", "物流更新了吗", "我的件到哪了"], ensure_ascii=False), "priority": 3, "cap": 2},
    {"id": "filler:zh-ack-1", "lang": "zh", "category": "ack", "text": "好，收到。", "triggers": _json.dumps(["我的微信号是", "单号是", "在淘宝买的", "好像是拼多多"], ensure_ascii=False), "priority": 4, "cap": 2},
    {"id": "filler:zh-ack-2", "lang": "zh", "category": "ack", "text": "嗯，收到您说的。", "triggers": _json.dumps(["我买的", "快递是顺丰", "对，拼多多"], ensure_ascii=False), "priority": 3, "cap": 2},
    {"id": "filler:zh-emp-1", "lang": "zh", "category": "empathy", "text": "理解您的心情，我们跟进。", "triggers": _json.dumps(["想投诉", "太过分了", "等太久了", "怎么这么慢"], ensure_ascii=False), "priority": 5, "cap": 1},
    {"id": "filler:zh-min-1", "lang": "zh", "category": "minimal", "text": "嗯——。", "triggers": _json.dumps(["嗯", "好", "哦", "行", "好的"], ensure_ascii=False), "priority": 2, "cap": 2},
    {"id": "filler:zh-def-1", "lang": "zh", "category": "default", "text": "嗯，好。", "triggers": _json.dumps([], ensure_ascii=False), "priority": 1, "cap": 2},
    # ---- cantonese ----
    {"id": "filler:can-comp-1", "lang": "cantonese", "category": "compensate", "text": "嗯……点样赔，等我讲你知。", "triggers": _json.dumps(["点样赔偿", "赔几多", "几时赔到", "想问下赔几多", "点赔"], ensure_ascii=False), "priority": 5, "cap": 2},
    {"id": "filler:can-comp-2", "lang": "cantonese", "category": "compensate", "text": "嗯，等我同你讲下赔法先。", "triggers": _json.dumps(["点解咁赔", "赔偿标准係咩"], ensure_ascii=False), "priority": 3, "cap": 2},
    {"id": "filler:can-check-1", "lang": "cantonese", "category": "check", "text": "嗯……我睇下。", "triggers": _json.dumps(["帮我查下张单", "我件货到边度", "三日都未到", "查下物流"], ensure_ascii=False), "priority": 4, "cap": 2},
    {"id": "filler:can-check-2", "lang": "cantonese", "category": "check", "text": "收到，等我查返先。", "triggers": _json.dumps(["仲未收到", "件货去咗边"], ensure_ascii=False), "priority": 3, "cap": 2},
    {"id": "filler:can-ack-1", "lang": "cantonese", "category": "ack", "text": "好，收到。", "triggers": _json.dumps(["我WhatsApp系", "我微信係", "單號係", "拼多多買"], ensure_ascii=False), "priority": 4, "cap": 2},
    {"id": "filler:can-ack-2", "lang": "cantonese", "category": "ack", "text": "明白，记低咗。", "triggers": _json.dumps(["淘寶買", "京東買"], ensure_ascii=False), "priority": 3, "cap": 2},
    {"id": "filler:can-emp-1", "lang": "cantonese", "category": "empathy", "text": "明白你嘅心情，我哋跟紧。", "triggers": _json.dumps(["想投诉", "等咗好耐", "太过分", "点解咁慢"], ensure_ascii=False), "priority": 5, "cap": 1},
    {"id": "filler:can-min-1", "lang": "cantonese", "category": "minimal", "text": "嗯——。", "triggers": _json.dumps(["嗯", "好", "哦", "係"], ensure_ascii=False), "priority": 2, "cap": 2},
    {"id": "filler:can-def-1", "lang": "cantonese", "category": "default", "text": "嗯，好。", "triggers": _json.dumps([], ensure_ascii=False), "priority": 1, "cap": 2},
    # ---- en ----
    {"id": "filler:en-comp-1", "lang": "en", "category": "compensate", "text": "Okay let me walk you through it.", "triggers": _json.dumps(["how much compensation", "how does the compensation work", "how will you compensate me"], ensure_ascii=False), "priority": 5, "cap": 2},
    {"id": "filler:en-check-1", "lang": "en", "category": "check", "text": "Let me see.", "triggers": _json.dumps(["check my parcel", "where is my order", "my parcel was due three days"], ensure_ascii=False), "priority": 4, "cap": 2},
    {"id": "filler:en-ack-1", "lang": "en", "category": "ack", "text": "Got it.", "triggers": _json.dumps(["my number is", "it's from amazon", "on temu", "on ebay"], ensure_ascii=False), "priority": 4, "cap": 2},
    {"id": "filler:en-emp-1", "lang": "en", "category": "empathy", "text": "I am really sorry about that.", "triggers": _json.dumps(["I want to complain", "this is unacceptable", "so slow"], ensure_ascii=False), "priority": 5, "cap": 1},
    {"id": "filler:en-min-1", "lang": "en", "category": "minimal", "text": "Mm hm.", "triggers": _json.dumps(["yeah", "okay", "sure"], ensure_ascii=False), "priority": 2, "cap": 2},
    {"id": "filler:en-def-1", "lang": "en", "category": "default", "text": "Sure.", "triggers": _json.dumps([], ensure_ascii=False), "priority": 1, "cap": 2},
]

# 节点鉴权(P1,终审修复):唯一索引建前去重语句。历史配额竞态可能在 nodes 表留下
# 同 (license_id, fingerprint) 重复行,令 CREATE UNIQUE INDEX 失败——每组保留
# MAX(id) 一行。方言可移植写法(SQLite/Postgres 通用,禁 sqlite rowid),
# tests/test_db_portability.py 门禁;tests/test_nodes_hardening.py 直接执行本语句回归。
NODES_FP_DEDUPE_SQL = (
    "DELETE FROM nodes WHERE license_id <> '' AND id NOT IN ("
    "SELECT MAX(id) FROM nodes WHERE license_id <> '' "
    "GROUP BY license_id, fingerprint)"
)


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
            from sqlalchemy import inspect as sa_inspect
            from sqlalchemy import text

            def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
                # 存在性探测走 SQLAlchemy inspector：方言无关（sqlite/postgres 同一套），
                # 且由方言把范围限定到当前 schema。旧实现用不带 schema 过滤的
                # information_schema 查询，多 schema 库（共享 PG 实例、Supabase 的
                # auth/storage schema、并排 staging schema）里有同名表就被误判「列已存在」
                # → ALTER 跳过、存量库升级静默丢列（2026-09-15 真 Postgres 冒烟实证）。
                # 表不存在=跳过该列（半旧库不再让整块补列中断）。
                insp = sa_inspect(conn)
                if not insp.has_table(table):
                    return
                if column not in {c["name"] for c in insp.get_columns(table)}:
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
                    "accounts",
                    "org_id",
                    "org_id VARCHAR(64) DEFAULT ''",
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
                    "turns",
                    "language",
                    "language VARCHAR(32) DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "object_profiles",
                    "digest",
                    "digest TEXT",
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
                    "contact_channel",
                    "contact_channel VARCHAR(32) DEFAULT ''",
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
                    "conversation_templates",
                    "hotwords",
                    "hotwords TEXT DEFAULT ''",
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
                # B 线同传术语表(P0-2,2026-09-16):建单随会话存,token 分发进
                # dispatch metadata → agent 装配 ASR 热词 + MT prompt 术语槽。
                _ensure_column(
                    conn,
                    "call_sessions",
                    "glossary",
                    "glossary TEXT DEFAULT ''",
                )
                # B 线会话级音色(2026-09-17):同传页建单我方/对方各选一把音色的
                # JSON map,token 分发进 dispatch metadata → interpret 优先消费。
                _ensure_column(
                    conn,
                    "call_sessions",
                    "voices_json",
                    "voices_json TEXT DEFAULT ''",
                )
                _ensure_column(
                    conn,
                    "call_sessions",
                    "session_report",
                    "session_report TEXT",
                )
                # P1-A（2026-09-17 全量 debug）：B 线 fwd/rev 双 worker 各自上报
                # SessionReport——主列 session_report 只留首份，per-worker 历史落
                # JSON 数组列（元素 {"worker","report","ts"}，同 worker 重发=替换、
                # 异 worker=追加）。DEFAULT '[]' 单引号字面量 SQLite/Postgres 双认
                # （方言门禁 tests/test_db_portability.py）。
                _ensure_column(
                    conn,
                    "call_sessions",
                    "session_reports_json",
                    "session_reports_json TEXT DEFAULT '[]'",
                )
                # turns 分析账本列（spec 2026-09-10 §6.1）：org/线别/说话人/生成源/
                # 话术步/时间轴。缺省值兜底旧行，二启幂等（列在即跳过）。
                _ensure_column(conn, "turns", "org_id", "org_id VARCHAR(64) DEFAULT ''")
                _ensure_column(conn, "turns", "line", "line VARCHAR(8) DEFAULT 'a'")
                _ensure_column(conn, "turns", "speaker", "speaker VARCHAR(32) DEFAULT ''")
                _ensure_column(conn, "turns", "gen", "gen VARCHAR(16) DEFAULT ''")
                _ensure_column(conn, "turns", "template_step", "template_step INTEGER DEFAULT 0")
                _ensure_column(conn, "turns", "started_ms", "started_ms INTEGER DEFAULT 0")
                _ensure_column(conn, "turns", "ended_ms", "ended_ms INTEGER DEFAULT 0")
                _ensure_column(conn, "turns", "perceived_ms", "perceived_ms INTEGER DEFAULT 0")
                # 外呼 SIP 配置段（spec 2026-09-12 Wave2）：空串=读侧回落默认段。
                _ensure_column(conn, "global_settings", "sip_json", "sip_json TEXT NOT NULL DEFAULT ''")
                # 外呼战役 mock 演练台词（spec 2026-09-12 Wave3）：object_id→[句子]
                # JSON，空串=无台词（真实通话恒空）。
                _ensure_column(conn, "campaigns", "scripts_json", "scripts_json TEXT DEFAULT ''")
                # 战役挂站点（spec 2026-09-13 P1.5 Task 2）：空串=未挂站点，dial 块
                # trunk 回退 settings `sip`（单站点旧行为零变化）。
                _ensure_column(conn, "campaigns", "site_id", "site_id VARCHAR(64) DEFAULT ''")
                # 话务员级资源(B3):话术/QA 个人归属 + 通话建单人——''=共享/无主。
                _ensure_column(conn, "conversation_templates", "owner_user_id", "owner_user_id VARCHAR(64) DEFAULT ''")
                _ensure_column(conn, "qa_entries", "owner_user_id", "owner_user_id VARCHAR(64) DEFAULT ''")
                _ensure_column(conn, "call_sessions", "created_by", "created_by VARCHAR(64) DEFAULT ''")
                # 页面权限(B4):主管按人配置话务员可见面——''=默认集（7 键，报表默认关），
                # 否则=JSON 数组精确集合（'[]'=全关，见 control_plane/permissions.py）。
                _ensure_column(conn, "users", "permissions_json", "permissions_json TEXT DEFAULT ''")
                # 节点鉴权(P1):license 绑定与机器指纹（node_licenses 新表走 create_all）。
                _ensure_column(conn, "nodes", "license_id", "license_id VARCHAR(64) DEFAULT ''")
                _ensure_column(conn, "nodes", "fingerprint", "fingerprint VARCHAR(128) DEFAULT ''")
                # 熔断真实化(site-delivery M1,2026-09-16):sticky 吊销来源/时刻。
                # revoked_source ''=在册 / 'root'=root 吊销(注册端点不得复活,须
                # /unrevoke) / 'auto_clone'=克隆自动吊销(原机重注册复活保留)。
                _ensure_column(conn, "nodes", "revoked_source", "revoked_source VARCHAR(16) DEFAULT ''")
                _ensure_column(conn, "nodes", "revoked_at", "revoked_at VARCHAR(32) DEFAULT ''")
                # 通话绑定节点(thin-node 拓扑):建单钉死承载节点,token 签发前校验。
                _ensure_column(conn, "call_sessions", "node_id", "node_id VARCHAR(64) DEFAULT ''")
                # 节点鉴权(P1,深测): (license_id, fingerprint) 部分唯一索引——多实例
                # 部署下配额竞态的库级兜底(进程内由 NodeStore.register_licensed 的
                # 锁收口)。只约束 license 绑定行:开放模式存量空值行不受影响。
                # 部分索引 WHERE 语法 SQLite/Postgres 双支持。
                # 建索引前先去重(终审修复):历史竞态可能已留下同 (license_id,
                # fingerprint) 重复行,直接 CREATE UNIQUE INDEX 会失败。每组保留
                # MAX(id) 一行——方言可移植写法(SQLite/Postgres 通用,禁 rowid)。
                _deduped = conn.execute(text(NODES_FP_DEDUPE_SQL))
                if _deduped.rowcount > 0:
                    print(
                        "[deps] nodes duplicate (license_id, fingerprint) rows "
                        f"deduped before unique index: removed={_deduped.rowcount} "
                        "(kept newest MAX(id) row per group)"
                    )
                try:
                    conn.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_license_fingerprint "
                        "ON nodes (license_id, fingerprint) WHERE license_id <> ''"
                    ))
                except Exception as exc:
                    # 专属告警(终审修复):不再落泛化的 migration skipped 文案,
                    # 索引建不起来(如仍有个别脏行)必须可定位。
                    print(f"[deps] uq_nodes_license_fingerprint create skipped: {exc}")
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
            _migrate_knowledge_content_hash(engine)
        except Exception as exc:  # pragma: no cover - sqlite / missing extension
            print(f"[deps] vector schema skipped: {exc}")
        # 垫话罐头库种子(2026-09-13 乙节):表空才灌,幂等——运营改词条/删除后
        # 不复活。种子=三语×六场景,trigger 是用户口吻示例句(HybridLexical 用)。
        try:
            from sqlalchemy import text

            with engine.begin() as conn:
                n = conn.execute(text("SELECT COUNT(*) FROM filler_entries")).scalar()
                if not n:
                    for row in _FILLER_SEEDS:
                        conn.execute(
                            text(
                                # enabled 用 TRUE 字面量（SQLite 3.23+/Postgres 都认）：
                                # 写 1 在 SQLite（INTEGER 亲和）能存，Postgres 的 boolean
                                # 列直接 DatatypeMismatch——整块种子被 except 吞掉，
                                # 分发部署首启垫话库静默为空（2026-09-15 真 PG 冒烟实证）。
                                "INSERT INTO filler_entries"
                                " (id, account_id, lang, category, text, triggers, voice_id,"
                                " priority, per_call_cap, enabled, hit_count, source, created_at)"
                                " VALUES (:id, 'acc-001', :lang, :category, :text, :triggers, '',"
                                " :priority, :cap, TRUE, 0, 'curated', CURRENT_TIMESTAMP)"
                            ),
                            row,
                        )
                    print(f"[deps] filler_entries seeded rows={len(_FILLER_SEEDS)}")
        except Exception as exc:  # pragma: no cover - 种子失败不阻断启动
            print(f"[deps] filler_entries seed skipped: {exc}")
        return engine
    return None


def _migrate_knowledge_content_hash(engine: Engine) -> None:
    """KB 增量索引(2026-09-10): knowledge_chunks 补 content_hash 列并回填存量。

    create_all 不会给已存在的表加列;回填走 Python 侧 sha256(方言无关,
    Postgres 无内置 sha256,不为此引 pgcrypto)。知识库量级小,一次性成本可忽略。
    幂等:列已存在且全部行已有哈希时为纯 no-op。"""
    import hashlib

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select, text

    from bok_voice_business_db.vector_models import KnowledgeChunk

    insp = sa_inspect(engine)
    if "knowledge_chunks" not in insp.get_table_names():
        return
    with engine.begin() as conn:
        cols = {c["name"] for c in insp.get_columns("knowledge_chunks")}
        if "content_hash" not in cols:
            conn.execute(
                text("ALTER TABLE knowledge_chunks ADD COLUMN content_hash VARCHAR(64) DEFAULT ''")
            )
        rows = conn.execute(
            select(KnowledgeChunk.id, KnowledgeChunk.text).where(
                (KnowledgeChunk.content_hash == "") | (KnowledgeChunk.content_hash.is_(None))
            )
        ).all()
        for cid, txt in rows:
            digest = hashlib.sha256((txt or "").encode("utf-8")).hexdigest()[:32]
            conn.execute(
                text("UPDATE knowledge_chunks SET content_hash=:h WHERE id=:id"),
                {"h": digest, "id": cid},
            )


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
