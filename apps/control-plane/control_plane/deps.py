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

# QA 自沉淀引擎两表（2026-09-25，qa_digest.py 消费；DDL 细节见 build_engine 内注释）：
# - qa_homophones：ASR 同音错写对子（wrong=客户原话被抄成的错形 / right=词条规范
#   问法），support=证据次数，复合主键 (wrong, right)——UPSERT 语义由引擎侧
#   读后写实现（support 取 max），不依赖方言特有 ON CONFLICT；
# - qa_digest_runs：闲时循环审计+水位双用——每轮一行各步骤计数，最近一次成功
#   finished_at 即下一次挖掘的水位（busy 跳过轮不落行、不推水位）。
QA_DIGEST_TABLE_DDL: tuple[str, ...] = (
    'CREATE TABLE IF NOT EXISTS qa_homophones ('
    ' wrong VARCHAR(255) NOT NULL,'
    ' "right" VARCHAR(255) NOT NULL,'
    ' support INTEGER NOT NULL DEFAULT 0,'
    " source VARCHAR(32) NOT NULL DEFAULT 'auto',"
    " created_at VARCHAR(32) NOT NULL DEFAULT '',"
    ' PRIMARY KEY (wrong, "right")'
    ')',
    'CREATE TABLE IF NOT EXISTS qa_digest_runs ('
    ' id VARCHAR(64) NOT NULL,'
    " started_at VARCHAR(32) NOT NULL DEFAULT '',"
    " finished_at VARCHAR(32) NOT NULL DEFAULT '',"
    ' adopted_variant INTEGER NOT NULL DEFAULT 0,'
    ' adopted_fresh INTEGER NOT NULL DEFAULT 0,'
    ' disabled INTEGER NOT NULL DEFAULT 0,'
    ' homophones INTEGER NOT NULL DEFAULT 0,'
    ' pregen INTEGER NOT NULL DEFAULT 0,'
    " error TEXT NOT NULL DEFAULT '',"
    ' PRIMARY KEY (id)'
    ')',
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
        else:
            # 远程 Postgres（云端 Supabase pooler 过 NAT/隧道）空闲连接会被中间
            # 设备静默断开（2026-09-18 生产实证：psycopg "SSL error: unexpected
            # eof while reading" / "server closed the connection unexpectedly"
            # 持续成批出现）。checkout 前轻量 ping，死连接透明回收重连——防
            # 「池里躺满僵尸连接」的首用失败连环 500。不放大池容量：占用泄漏
            # 以修复占用方为准（campaign tick 每轮确定性 close，见 campaign.py）。
            kwargs["pool_pre_ping"] = True
        engine = create_engine(url, **kwargs)
        from bok_voice_business_db import models

        models.create_all(engine)
        # create_all 不会给已存在的表加列 —— 幂等补上新增列。注意 SQLite 不支持
        # `ADD COLUMN IF NOT EXISTS`（MySQL 语法，会抛错被吞），必须先查列是否存在。
        # W2-T1：published_json 本次是否刚建列（一次性回填存量行的闸）。
        _published_created = False
        try:
            from sqlalchemy import inspect as sa_inspect
            from sqlalchemy import text

            def _ensure_column(conn, table: str, column: str, ddl: str) -> bool:
                # 存在性探测走 SQLAlchemy inspector：方言无关（sqlite/postgres 同一套），
                # 且由方言把范围限定到当前 schema。旧实现用不带 schema 过滤的
                # information_schema 查询，多 schema 库（共享 PG 实例、Supabase 的
                # auth/storage schema、并排 staging schema）里有同名表就被误判「列已存在」
                # → ALTER 跳过、存量库升级静默丢列（2026-09-15 真 Postgres 冒烟实证）。
                # 表不存在=跳过该列（半旧库不再让整块补列中断）。
                # 返回值=本次是否「刚建列」（W2-T1 一次性回填用：仅刚建列才跑存量
                # 数据迁移；既有调用点全部忽略返回值，零影响）。
                insp = sa_inspect(conn)
                if not insp.has_table(table):
                    return False
                if column not in {c["name"] for c in insp.get_columns(table)}:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
                    return True
                return False

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
                # 发音词典（2026-09-27）：多行 `原词/读法` 文本，装配时下发 MiniMax
                # pronunciation_dict 让人名/专名读准。空串=默认读音（运行时不下发键）。
                _ensure_column(
                    conn,
                    "object_profiles",
                    "pronunciation",
                    "pronunciation TEXT DEFAULT ''",
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
                # 意向规则引擎(W4-T1,2026-09-19):人工协助面状态 + 挂断意向码。
                # server_default 与 models.CallSession 同形（DEFAULT ''，方言安全）。
                _ensure_column(conn, "call_sessions", "assist_status",
                               "assist_status VARCHAR(16) DEFAULT ''")
                _ensure_column(conn, "call_sessions", "intent_code",
                               "intent_code VARCHAR(32) DEFAULT ''")
                # 战役调度三字段（2026-09-17 竞品对齐）：时段窗/任务级并发/自动重拨。
                # server_default 同形（DEFAULT '[]'/1/''，单引号字面量 SQLite/PG 双认）。
                _ensure_column(conn, "campaigns", "call_windows_json",
                               "call_windows_json TEXT DEFAULT '[]'")
                _ensure_column(conn, "campaigns", "max_concurrency",
                               "max_concurrency INTEGER DEFAULT 1")
                _ensure_column(conn, "campaigns", "redispatch_json",
                               "redispatch_json TEXT DEFAULT ''")
                # 仪表盘时长统计（2026-09-17）：started_at/ended_at=接通与终态时刻，
                # duration_s=接通秒数。DateTime 列迁移 DDL 用 TIMESTAMP——PG 无
                # DATETIME 类型（实测 ERROR: type "datetime" does not exist），SQLite
                # 双认（类型名任意）；与 ORM DateTime 列（PG 编译 TIMESTAMP WITHOUT
                # TIME ZONE）同域。
                _ensure_column(conn, "call_sessions", "started_at",
                               "started_at TIMESTAMP")
                _ensure_column(conn, "call_sessions", "ended_at",
                               "ended_at TIMESTAMP")
                _ensure_column(conn, "call_sessions", "duration_s",
                               "duration_s INTEGER DEFAULT 0")
                # 全局外呼时段窗段（2026-09-17 T3b）：settings.campaign 落库列。
                # 空 blob=不限时段；与 models.GlobalSetting.campaign_json server 侧
                # 同形（TEXT NOT NULL DEFAULT ''，SQLite/PG 双认；NOT NULL 对齐
                # 同函数 sip_json 先例与 ORM create_all 产物，旧库 ADD COLUMN
                # 带 DEFAULT 合法）。
                _ensure_column(conn, "global_settings", "campaign_json",
                               "campaign_json TEXT NOT NULL DEFAULT ''")
                # 通知域（W5-T1）：settings.sms 段（webhook provider 骨架）落库列。
                # 空 blob=未配置 → 读侧回落 default_settings()["sms"]；DDL 与
                # models.GlobalSetting.sms_json server 侧同形（同 campaign_json 先例）。
                _ensure_column(conn, "global_settings", "sms_json",
                               "sms_json TEXT NOT NULL DEFAULT ''")
                # 模型路由统一（2026-09-25 阶段 0）：五车道（a_reply/judge/mt/
                # settle/mining）本地↔云端路由表 + 档位预置，契约单点在
                # packages/core/bok_voice_core/model_routes.py。空 blob=全 local
                # （env 缺省链），读侧零配置即用；DDL 同 sms_json/campaign_json
                # 先例（TEXT NOT NULL DEFAULT ''，SQLite/PG 双认，方言门禁）。
                _ensure_column(conn, "global_settings", "model_routing_json",
                               "model_routing_json TEXT NOT NULL DEFAULT ''")
                # 同义簇(qa-canvas Phase1,spec 2026-09-17):qa_entries 变体指向
                # 簇头条目——''=独立条目/簇头本体,非空=本条是指向条目的变体。
                _ensure_column(
                    conn,
                    "qa_entries",
                    "cluster_head_id",
                    "cluster_head_id VARCHAR(64) DEFAULT ''",
                )
                # 匹配优先级(2026-09-18 Phase 3.1):小者先,默认 10;INTEGER NOT
                # NULL+DEFAULT 10 方言安全,旧库补列即全员默认=零变化。
                _ensure_column(
                    conn,
                    "qa_entries",
                    "priority",
                    "priority INTEGER NOT NULL DEFAULT 10",
                )
                # VectorQ 每词条自适应阈值(2026-09-25):可空 float,NULL=用全局
                # 默认档(0.80)。生产端=CP qa_digest 的 drift 反馈(repeat_after_play
                # 一升二禁/清白命中回落);消费端=agent qa_gate 逐条目读
                # entry["hit_threshold"]。可空无 DEFAULT——与 models.QaEntry.
                # hit_threshold(nullable=True)create_all 路径同形,方言安全。
                _ensure_column(
                    conn,
                    "qa_entries",
                    "hit_threshold",
                    "hit_threshold FLOAT",
                )
                # 话术图(2026-09-18 Phase 2):模板可选携带意图节点+绑定边 JSON,
                # ''=未启用(旧库补列即空串,装配零变化)。TEXT+DEFAULT '' 方言安全。
                _ensure_column(
                    conn, "conversation_templates", "graph_json", "graph_json TEXT DEFAULT ''"
                )
                # 模板发布两态(W2-T1,2026-09-19):发布冻结快照九键 JSON,''=从未发布。
                # 「本次刚建列」记录到外层标志——只有刚建列才跑下方存量回填
                # (幂等铁律:二启/新建模板重启都不得再回填,否则未发布模板被误标已发布)。
                _published_created = bool(
                    _ensure_column(
                        conn,
                        "conversation_templates",
                        "published_json",
                        "published_json TEXT DEFAULT ''",
                    )
                )
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

        # ---- QA 自沉淀引擎表（2026-09-25 CP 闲时循环，qa_digest.py 消费）----
        # 两表 CREATE TABLE IF NOT EXISTS 幂等（二启零变化）；方言可移植：无
        # sqlite 专有语法，id 沿仓库现有表风格用 TEXT 主键（uuid 由引擎侧生成，
        # 避免 AUTOINCREMENT/SERIAL 跨方言分叉）。"right" 带双引号标识符——
        # RIGHT 是 SQL 保留字（Postgres 拒绝裸用），引号形式 SQLite/PG 双认。
        try:
            from sqlalchemy import text

            with engine.begin() as conn:
                for ddl in QA_DIGEST_TABLE_DDL:
                    conn.execute(text(ddl))
        except Exception as exc:  # pragma: no cover - 建表失败不阻断启动
            print(f"[deps] qa digest tables skipped: {exc}")

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
        # ---- 模板发布两态一次性回填(W2-T1)：仅当 published_json 列本次启动刚创建。
        # 存量行视为已发布——冻结当时 live 九键,保证迁移后运行时(机器通道 overlay)
        # 读到=原 live,零行为变化。排在 yue→cantonese 数据迁移之后:冻结的 language
        # 必须是规范值,否则冻结串与迁移后 live 恒不一致(假 has_changes + overlay
        # 回吐旧值)。每次启动都跑=错误:新建未发布模板重启即被「视为已发布」。
        if _published_created:
            try:
                import json as _pubjson

                with engine.begin() as conn:
                    _rows = conn.execute(
                        text(
                            "SELECT id, steps_json, graph_json, hotwords, tone_override,"
                            " opening, core, objection, closing, language"
                            " FROM conversation_templates WHERE published_json=''"
                        )
                    ).fetchall()
                    _backfilled = 0
                    for _row in _rows:
                        (
                            _rid, _steps, _graph, _hot, _tone,
                            _opening, _core, _obj, _closing, _lang,
                        ) = _row
                        # 回填判据(任务书口径):steps_json/opening/core 任一非空——
                        # 空壳模板(九键全空)不回填,保持「从未发布」语义。
                        if not (_steps or _opening or _core):
                            continue
                        _payload = {
                            "steps_json": _steps or "",
                            "graph_json": _graph or "",
                            "hotwords": _hot or "",
                            "tone_override": _tone or "",
                            "opening": _opening or "",
                            "core": _core or "",
                            "objection": _obj or "",
                            "closing": _closing or "",
                            "language": _lang or "",
                        }
                        conn.execute(
                            text("UPDATE conversation_templates SET published_json=:v WHERE id=:id"),
                            {"v": _pubjson.dumps(_payload, ensure_ascii=False), "id": _rid},
                        )
                        _backfilled += 1
                if _backfilled:
                    print(
                        "[deps] conversation_templates published_json backfilled "
                        f"rows={_backfilled} (one-shot on column creation)"
                    )
            except Exception as exc:  # pragma: no cover - 回填失败不阻断启动
                print(f"[deps] published_json backfill skipped: {exc}")
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


# ---- 模型路由存储（2026-09-25 阶段 0，CP 进程内消费面）----
# global_settings.model_routing_json 列不进仓库层 get/save_settings（业务库包不
# 随本改动动列语义），CP 侧经这里直读直写。SQL 路径用 session factory（main
# startup 绑定，与 repo 同一 engine）；engine=None（单机内存形态/tests）回落
# 模块级内存——与 InMemoryBusinessRepository 同生命周期，行为对齐。

_ROUTING_ROW_ID = "global"
_routing_state: dict = {"session_factory": None, "memory": ""}


def bind_routing_storage(session_factory) -> None:
    """main startup 调用：绑定与 repo 同 engine 的 session factory（None=内存态）。"""
    _routing_state["session_factory"] = session_factory


def read_model_routing_raw() -> str:
    """读路由表原始 JSON 串（空串=未配置，parse_routing 宽容面接管）。读失败软回落。"""
    factory = _routing_state["session_factory"]
    if factory is None:
        return str(_routing_state["memory"] or "")
    from sqlalchemy import text

    try:
        with factory() as session:
            row = session.execute(
                text("SELECT model_routing_json FROM global_settings WHERE id = :gid"),
                {"gid": _ROUTING_ROW_ID},
            ).fetchone()
        return str(row[0] or "") if row else ""
    except Exception as exc:  # pragma: no cover - 读失败不破坏结算/挖掘主链
        print(f"[deps] model_routing read skipped: {exc!r}")
        return ""


def write_model_routing_raw(raw: str) -> None:
    """写路由表原始 JSON 串（幂等 upsert：行缺省时补一行业务默认值的全行）。"""
    raw = str(raw or "")
    factory = _routing_state["session_factory"]
    if factory is None:
        _routing_state["memory"] = raw
        return
    from sqlalchemy import text

    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

    with factory() as session:
        row = session.execute(
            text("SELECT id FROM global_settings WHERE id = :gid"), {"gid": _ROUTING_ROW_ID}
        ).fetchone()
        if row is None:
            # 行缺省=先经 ORM 落一行业务默认值（default_settings 全段 JSON；
            # updated_at 等 nullable/DateTime 类型由 ORM 兜住，SQLite/PG 双认），
            # 再原位 UPDATE 路由列——不手写跨方言 INSERT，避免与 ORM 默认值漂移
            # （首版手写 INSERT 曾漏 updated_at NOT NULL，SQLite 冒烟实证）。
            repo = SqlAlchemyBusinessRepository(factory())
            repo.save_settings(dict(repo.get_settings()))
        session.execute(
            text("UPDATE global_settings SET model_routing_json = :raw WHERE id = :gid"),
            {"raw": raw, "gid": _ROUTING_ROW_ID},
        )
        session.commit()
