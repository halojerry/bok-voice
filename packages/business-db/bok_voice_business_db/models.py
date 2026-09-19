from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_hex() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # 租户缝（B1 身份）
    display_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PersonaProfile(Base):
    __tablename__ = "persona_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    company: Mapped[str] = mapped_column(String(255), default="")
    tone: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    reference_audio: Mapped[str] = mapped_column(String(512), default="")
    # 人设级 TTS 引擎："" = 跟随全局设置；qwen3_tts / minimax / volcano_streaming / fake。
    # 决定该人设通话用哪套音色（本地克隆 vs 云端 MiniMax/火山），避免克隆 ID 串引擎。
    tts_provider: Mapped[str] = mapped_column(String(32), default="")


class ObjectProfile(Base):
    __tablename__ = "object_profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role_template: Mapped[str] = mapped_column(String(64), default="customer")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    background: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    # 快递场景变量:姓名=display_name;单号/物流公司/收货地址供话术变量与 LLM 引用。
    tracking_no: Mapped[str] = mapped_column(String(64), default="")
    courier: Mapped[str] = mapped_column(String(64), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    # 联系渠道({聯絡方式} 话术变量):空=按对象语言缺省(zh→微信/cantonese|en→WhatsApp)。
    contact_channel: Mapped[str] = mapped_column(String(32), default="")
    # 对象级滚动摘要（蒸馏沉淀,settle 时并入;分析/回访视角一屏可见）
    digest: Mapped[str] = mapped_column(Text, default="")
    template_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")


class ConversationTemplate(Base):
    __tablename__ = "conversation_templates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    opening: Mapped[str] = mapped_column(Text, default="")
    core: Mapped[str] = mapped_column(Text, default="")
    objection: Mapped[str] = mapped_column(Text, default="")
    closing: Mapped[str] = mapped_column(Text, default="")
    tone_override: Mapped[str] = mapped_column(String(255), default="")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    # 分步话术:JSON 数组 [{"goal": "这一步要达成的目标", "ref": "参考说法(可含 {变量})"}, ...]。
    # 由 agent 每轮按步骤推进,LLM 结合客户回复只回应当前步。opening/core 四段保留兼容。
    steps_json: Mapped[str] = mapped_column(Text, default="")
    # 本套话术专属 ASR 热词(2026-09-08):逗号/顿号/分号/换行分隔,随会话装配并入
    # asr_hotword_context 下发 /api/start context(数字主导词会被过滤,防幻听号码)。
    hotwords: Mapped[str] = mapped_column(Text, default="")
    # 话术图(2026-09-18 Phase 2):意图节点+绑定边 JSON(spec
    # docs/superpowers/specs/2026-09-18-qa-flow-graph.md);空串=未启用,
    # 运行时零变化。校验/解析见 packages/core/bok_voice_core/flow_graph.py。
    graph_json: Mapped[str] = mapped_column(Text, default="")
    # 发布冻结快照(W2-T1 模板发布两态,2026-09-19):发布时把九键
    # (steps_json/graph_json/hotwords/tone_override/opening/core/objection/
    # closing/language)的 live 值原样收进 JSON dict;空串=从未发布。
    # 「已发布」≡本列非空,「有未发布改动」≡ live 与冻结不一致(CP 派生布尔)。
    # 建单装配经机器通道 overlay 恒吃冻结版;编辑保存(PUT)永不触碰本列。
    published_json: Mapped[str] = mapped_column(Text, default="")
    # 话务员级归属(B3):''=账号共享 / user_id=话务员个人——user 只见自己的+共享。
    owner_user_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ObjectTopic(Base):
    __tablename__ = "object_topics"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    topic: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CallSession(Base):
    __tablename__ = "call_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    object_id: Mapped[str] = mapped_column(String(64), index=True)
    persona_id: Mapped[str] = mapped_column(String(64), default="")
    template_id: Mapped[str] = mapped_column(String(64), default="")
    # 建单人身份(B3):user_id——运行时 QA 检索按「共享+建单人个人」收窄;战役建单无身份=''。
    created_by: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(32), default="simulation")
    direction: Mapped[str] = mapped_column(String(32), default="webrtc")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    contact_phone: Mapped[str] = mapped_column(String(64), default="")
    consent_status: Mapped[str] = mapped_column(String(32), default="unknown")
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    disposition: Mapped[str] = mapped_column(String(64), default="")
    escalated_to_human: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    # WhatsApp 對接狀態:""(未提供) / offered(客戶應承加專員,未俾號碼) /
    # captured(客戶讀出咗自己號碼) / handled(專員已標記對接)。customer_whatsapp 存號碼。
    whatsapp_status: Mapped[str] = mapped_column(String(16), default="")
    customer_whatsapp: Mapped[str] = mapped_column(String(64), default="")
    # 会话种类:""=客服通话(A 线) / interpret=双端同传(B 线 v2)。
    # interpret 会话:language=我方语言,target_lang=对方语言,object_id 通常为空。
    kind: Mapped[str] = mapped_column(String(32), default="")
    target_lang: Mapped[str] = mapped_column(String(16), default="")
    # B 线同传术语表(P0-2,2026-09-16):「源=译」或纯词条,分隔符见 interpret
    # parse_glossary;建单随会话存,token 分发进 dispatch metadata。客服通话恒空。
    glossary: Mapped[str] = mapped_column(Text, default="")
    # B 线会话级音色(2026-09-17):同传页建单我方/对方语言各选一把 MiniMax 音色
    # 的 JSON map。token 分发进 dispatch metadata → interpret 按 target_lang 取键。
    voices_json: Mapped[str] = mapped_column(Text, default="")
    # 官方 SessionReport JSON(agent shutdown 上报):真实逐模型 usage/权威 chat_history。
    session_report: Mapped[str] = mapped_column(Text, default="")
    # P1-A per-worker SessionReport 历史(2026-09-17 全量 debug):JSON 数组,元素
    # {"worker","report","ts"}——B 线 fwd/rev 双 worker 各留一份,同 worker 重发
    # (重试)替换、异 worker 追加。server_default 与 deps.build_engine 迁移 DDL
    # 同形（DEFAULT '[]'，单引号字面量 SQLite/Postgres 双认）。
    session_reports_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    # 仪表盘时长统计（2026-09-17）：started_at=首次接通时刻；ended_at=终态时刻；
    # duration_s=接通秒数（未接通=0）。DateTime nullable 与 campaigns.finished_at 同形。
    started_at: Mapped[datetime | None] = mapped_column(DateTime, default=None, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, default=None, nullable=True)
    duration_s: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 通话绑定节点(site-delivery M1,2026-09-16 thin-node 拓扑):建单时钉死承载节点,
    # /api/token 签发前校验其未吊销——root 熔断对「坐席 JWT 建单」路径同样生效。
    # ''=无绑定(单机全栈形态),行为零变化。server_default 与迁移 DDL 同形（DEFAULT ''）。
    node_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # 人工协助面(W4-T1,2026-09-19 意向规则引擎):''=无 / notified=已通知人工
    # (WhatsApp 捕获顺手置入或 /api/calls/{id}/assist 打铃) / done=人工已接手
    # (takeover 顺手置入或 assist done)。写者=CP 侧 whatsapp capture / assist /
    # takeover 端点;幂等纪律:done 不降级 notified。server_default 与迁移 DDL 同形。
    assist_status: Mapped[str] = mapped_column(String(16), default="", server_default="")
    # 挂断意向码(W4-T2 agent 评估回写):挂断快照命中意向规则时随 end_call 落列,
    # ''=未命中(默认 disposition 语义零变化)。
    intent_code: Mapped[str] = mapped_column(String(32), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class IntentRule(Base):
    """意向规则（W4-T1，2026-09-19）：挂断评估的条件规则，命中 → intent_code/disposition。

    两级作用域：account_id ''=全局行（admin/root 写）、账号行=话务员写；
    读=两级行合并（``account_id IN ('', acct)``，owner_scope IN 同款先例）。
    条件形状（fact/op/value，INTENT_FACTS 12 键）与评估纯函数见共享契约
    packages/core/bok_voice_core/intent_rules.py（CLI/CP/agent 三方共用，CP 只消费）。
    """

    __tablename__ = "intent_rules"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uuid_hex)
    # ''=全局行 admin 写、账号行话务员写、读=两级行合并。
    account_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    name: Mapped[str] = mapped_column(String(64), default="")
    intent_code: Mapped[str] = mapped_column(String(32), default="")
    label: Mapped[str] = mapped_column(String(64), default="")
    disposition: Mapped[str] = mapped_column(String(32), default="")
    # 条件数组 JSON（validate_conditions 严格轨校验后才落库）；坏 JSON 读侧=空数组。
    conditions_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    # 小者先（与 qa_entries.priority 同约定）；eval 侧 (priority, id) 升序取首条命中。
    priority: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class RosterEntry(Base):
    """名册（认领池）：通话中捕获的客户 WhatsApp/微信号码，专员认领后对接。

    去重键 = account+object+channel+number 且 status != handled；captured 自动入册。
    status: unclaimed(待认领) / claimed(已认领) / handled(已对接)。
    """
    __tablename__ = "roster_entries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    call_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    object_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    channel: Mapped[str] = mapped_column(String(16), default="whatsapp")
    number: Mapped[str] = mapped_column(String(64), default="")
    display_name: Mapped[str] = mapped_column(String(255), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="unclaimed")
    claimed_by: Mapped[str] = mapped_column(String(64), default="")
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Campaign(Base):
    """外呼战役:对象名单串行逐个拨(spec Wave3)。status: draft/running/paused/done/stopped。"""
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    template_id: Mapped[str] = mapped_column(String(64), default="")
    persona_id: Mapped[str] = mapped_column(String(64), default="")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    gap_seconds: Mapped[int] = mapped_column(Integer, default=5)
    # mock 演练台词：object_id → [句子]，JSON 串。与 scenario 同 spirit 的测试钩子
    # （生产空）。campaign 级存一份（item.scenario 只有 16 字符放不下台词），
    # 起拨时按 item.object_id 取出来塞进 dial 块 `script`。
    scripts_json: Mapped[str] = mapped_column(Text, default="")
    # 电话边缘站点（spec 2026-09-13 P1.5）：空串=未挂站点，dial 块回退 settings
    # `sip`（单站点旧行为零变化）；挂站点时 trunk 取站点注册值、settings 兜底。
    site_id: Mapped[str] = mapped_column(String(64), default="")
    # 外呼时段窗（2026-09-17 竞品对齐）：JSON 数组 [{"days":[1..7],"start":"HH:MM","end":"HH:MM"}]，
    # days=ISO 星期(1=周一)；空数组=不限。≤3 组，解析归一见 campaign.parse_call_windows。
    # server_default 与 deps._ensure_column 迁移 DDL 同形。
    call_windows_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    # 任务级最大并发：0=不限；≥1=同刻至多 N 通在途。缺省 1=旧串行行为。
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # 未接通自动重拨：{"max_attempts":2,"interval_minutes":30,"on":["no_answer"]}；空串=不重拨（旧行为）。
    redispatch_json: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class SipSite(Base):
    """电话边缘站点(spec 2026-09-13 sip-edge-thin-node-v2 §7 P1.5)。

    站点 = 电话边缘拓扑的最小维度:本机/VPS 的 LiveKit 地址、SIP 边缘形态
    (none=纯 WebRTC 无电话边缘 / local=本机 livekit-sip / cloud=云端 SIP)、
    注册后的 outbound trunk id(`ST_...`,长生命周期只建不逐通建)、可用主叫
    号码池(numbers_json=JSON 数组)、部署区域。campaign 经 site_id 挂站点,
    未挂站点时 dial 块回退 settings `sip`(单站点旧行为零变化)。

    `site-local` 不是本表行:它是 `get_default_site` 的虚拟默认站点
    (env LIVEKIT_URL + sip_edge=none),不入库、不进 list_sites。
    """

    __tablename__ = "sip_sites"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    livekit_url: Mapped[str] = mapped_column(String(512), default="")
    sip_edge: Mapped[str] = mapped_column(String(16), default="local")  # none/local/cloud
    trunk_id: Mapped[str] = mapped_column(String(64), default="")
    numbers_json: Mapped[str] = mapped_column(Text, default="[]")
    region: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class CampaignItem(Base):
    """战役单条:一个对象一通。scenario=mock 剧本钩子(测试/演练用,生产空)。"""
    __tablename__ = "campaign_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    object_id: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    call_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str] = mapped_column(String(255), default="")
    scenario: Mapped[str] = mapped_column(String(16), default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class Turn(Base):
    __tablename__ = "turns"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    turn_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    transcript: Mapped[str] = mapped_column(Text)
    emotion: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    language: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # ---- 分析账本列（spec 2026-09-10 §6.1）：缺省值兜底旧库/旧调用；
    # 既有库的列由 deps.build_engine 幂等 _ensure_column 补齐。
    org_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    line: Mapped[str] = mapped_column(String(8), default="a")  # a=客服(A 线) / b=同传(B 线)
    speaker: Mapped[str] = mapped_column(String(32), default="")
    gen: Mapped[str] = mapped_column(String(16), default="")
    template_step: Mapped[int] = mapped_column(Integer, default=0)
    started_ms: Mapped[int] = mapped_column(Integer, default=0)
    ended_ms: Mapped[int] = mapped_column(Integer, default=0)
    perceived_ms: Mapped[int] = mapped_column(Integer, default=0)


class Settlement(Base):
    __tablename__ = "settlements"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    summary: Mapped[str] = mapped_column(Text, default="")
    transcript_doc_path: Mapped[str] = mapped_column(String(512), default="")
    settlement_doc_path: Mapped[str] = mapped_column(String(512), default="")
    new_topics_json: Mapped[str] = mapped_column(Text, default="[]")
    global_insight_id: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class GlobalInsight(Base):
    __tablename__ = "global_insights"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), default="objection")
    statement: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    language: Mapped[str] = mapped_column(String(16), default="zh")
    status: Mapped[str] = mapped_column(String(32), default="active")


class GlobalSetting(Base):
    __tablename__ = "global_settings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default="global")
    asr_json: Mapped[str] = mapped_column(Text, default="{}")
    llm_json: Mapped[str] = mapped_column(Text, default="{}")
    tts_json: Mapped[str] = mapped_column(Text, default="{}")
    vad_json: Mapped[str] = mapped_column(Text, default="{}")
    # 外呼（SIP）配置段（spec 2026-09-12 Wave2）：mode/trunk/主叫号/超时/许可号码。
    # 空串=老库尚未补列或从未保存 → 读侧回落 default_settings()["sip"]。
    sip_json: Mapped[str] = mapped_column(Text, default="")
    # 全局外呼时段窗段（2026-09-17 T3b）：{"call_windows": [...]}，形状归一见
    # campaign.parse_call_windows；空串/空 dict=不限时段。迁移 DDL 与
    # deps._ensure_column 同形。server_default 必须带上：dump_postgres_ddl 从
    # 模型生成 Supabase 引导件，ORM-only default 不进 DDL → 裸 INSERT 直撞
    # NotNullViolation（CI postgres-smoke 实证）。
    campaign_json: Mapped[str] = mapped_column(Text, default="", server_default="")
    policy: Mapped[str] = mapped_column(String(64), default="offline_first")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AuditEventRecord(Base):
    """Append-only business audit trail, mirrored from the JSONL sink.

    Keeps a queryable copy of every audited action (voice clone, settle,
    template/object/persona edits, settings save, knowledge import…) so the
    UI / reports can render a traceable history without scraping log files.
    """

    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), default="")
    subject_id: Mapped[str] = mapped_column(String(128), default="")
    actor: Mapped[str] = mapped_column(String(128), default="")
    outcome: Mapped[str] = mapped_column(String(32), default="ok")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    request_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    call_id: Mapped[str] = mapped_column(String(64), default="")
    account_id: Mapped[str] = mapped_column(String(64), default="")
    object_id: Mapped[str] = mapped_column(String(64), default="")
    persona_id: Mapped[str] = mapped_column(String(64), default="")


class TemplateRevision(Base):
    """话术模板版本快照（话术优化的基础设施:update 即存旧版,结算可按版本分组）。"""

    __tablename__ = "conversation_template_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    snapshot: Mapped[str] = mapped_column(Text)  # 旧版整行 JSON
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(64))
    units: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    audio_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class QaEntry(Base):
    """快答库(Q→A 检索快路,2026-09-09):用户话语命中 → 跳过 LLM 播预生成回答。

    scope=global(步不变社交轮/FAQ) | step(仅限 step_index 步);source=curated
    (运营精选) | mined(turns 高频配对挖掘)。应答音频由 tts-pregen 物化进
    app-data/tts-cache,本表不存音频——闸门只认「缓存有音频」的条目。
    """

    __tablename__ = "qa_entries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True, default="acc-001")
    # 话务员级归属(B3):''=账号共享 / user_id=话务员个人。
    owner_user_id: Mapped[str] = mapped_column(String(64), default="")
    question_text: Mapped[str] = mapped_column(Text)
    answer_text: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(16), default="zh")
    scope: Mapped[str] = mapped_column(String(16), default="global")
    step_index: Mapped[int] = mapped_column(Integer, default=-1)
    voice_id: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="curated")
    # 同义簇(spec 2026-09-17 Phase1):非空=本条是指向条目的变体(一层星形)。
    # 纯展示/组织字段——qa_gate 匹配/罐头 key 均不读它。
    cluster_head_id: Mapped[str] = mapped_column(String(64), default="")
    # 匹配优先级(2026-09-18 Phase 3.1):阈值过关者中小者先;默认 10=与 graph
    # DEFAULT_PRIORITY 同约定,全默认时胜者与旧纯分数档逐字节同。
    # server_default 与 _ensure_column 的 DDL DEFAULT 10 镜像(models.py 惯例:
    # create_all 路径与 ALTER 路径必须同形,否则 schema-drift 门禁红)。
    priority: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    template_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class FillerEntry(Base):
    """垫话罐头库(2026-09-13 乙节):客户上一句 → 确定性命中一条垫话。

    镜像 qa_entries 全套基建:triggers=JSON 数组(用户口吻示例句,HybridLexical
    匹配);category=compensate/check/ack/empathy/minimal/default(五类分类器
    同一套枚举,命中加分);per_call_cap=同条目每通封顶(防同语境连击);音频由
    tts-pregen --fillers 按人设物化进 tts-cache(pin),本表不存音频。匹配是
    确定性的(同输入同条目)——用户拍板:随机抽签才是机器感,真人客服同样
    场景说同样话。
    """

    __tablename__ = "filler_entries"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True, default="acc-001")
    lang: Mapped[str] = mapped_column(String(16), default="zh")
    category: Mapped[str] = mapped_column(String(16), default="default")
    text: Mapped[str] = mapped_column(Text)
    triggers: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组(示例句)
    voice_id: Mapped[str] = mapped_column(String(128), default="")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    per_call_cap: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="curated")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Org(Base):
    """租户（P0 骨架：身份体系 P1 落地，先立 org 缝）。"""
    __tablename__ = "orgs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/suspended
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Node(Base):
    """部署节点（客户机房 GPU 盒）：注册时签发 node_token，只存 sha256。"""
    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    token_hash: Mapped[str] = mapped_column(String(128), default="")
    platform: Mapped[str] = mapped_column(String(32), default="")  # cuda-win / mac-mlx
    version: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="offline")  # online/offline/revoked
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    last_seen_at: Mapped[object] = mapped_column(DateTime, default=None, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # P1 节点鉴权：注册时绑定的 license 与机器指纹（sha256 hex，非原始序列号）。
    # license_id 空=加固模式前注册的存量节点（心跳只查 token 不查 license）。
    license_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), default="")
    # 熔断真实化（site-delivery M1，2026-09-16）：sticky 吊销来源与时刻。
    # revoked_source: ''=在册 / 'root'=root 显式吊销（sticky——注册端点不得复活，
    # 须 root /unrevoke 解除）/ 'auto_clone'=克隆/挪机自动吊销（原机指纹重注册
    # 复活路径保留）。revoked_at=ISO 字符串（吊销时刻；解除后保留作历史）。
    # server_default 与 deps._ensure_column 迁移 DDL 同形（DEFAULT ''）。
    revoked_source: Mapped[str] = mapped_column(String(16), default="", server_default="")
    revoked_at: Mapped[str] = mapped_column(String(32), default="", server_default="")


class NodeLicense(Base):
    """节点许可证（P1 节点鉴权）：root 签发，节点注册的第二因子。

    加固模式（BOK_AUTH_REQUIRED=1 或 BOK_CP_TOKEN 已设）下 register 必须携带
    有效 license_key；key 明文只在签发响应出现一次，库内恒为 sha256。
    吊销 license = 其名下全部节点 token 即刻失效（心跳 401）。max_nodes 配额
    按未吊销节点数计；同一 (license_id, fingerprint) 重注册幂等复用同一 node_id
    （机器重装/重启不烧配额），指纹不同且配额满 = 拒绝（克隆/挪机检出）。
    """

    __tablename__ = "node_licenses"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    account_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    key_hash: Mapped[str] = mapped_column(String(128), default="")
    max_nodes: Mapped[int] = mapped_column(default=1)
    note: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/revoked
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class NodeCommand(Base):
    """节点指令（P3 commands 通道，2026-09-17 落地）：root 经 CP 下发，节点心跳领走执行。

    action 白名单（update/restart/shutdown——CP 出口与端点双闸，绝不推任意
    shell）；target_version 独立列（update 的目标版本——完成判定=节点心跳上报
    version 收敛自动关单，不做 JSON 反刮）。status: pending → delivered →
    done/failed（shutdown/restart 是 fire-and-expect：delivered 即终态，结果
    由节点 status 变化间接可见；update 失败经心跳 acks 报回 failed）。
    """

    __tablename__ = "node_commands"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    action: Mapped[str] = mapped_column(String(32), default="")
    target_version: Mapped[str] = mapped_column(String(64), default="", server_default="")
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[str] = mapped_column(String(255), default="", server_default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    delivered_at: Mapped[object] = mapped_column(DateTime, default=None, nullable=True)
    closed_at: Mapped[object] = mapped_column(DateTime, default=None, nullable=True)


class User(Base):
    """平台用户（三层 RBAC：root/admin/user，thin-node spec §7 2026-09-14 修订）。

    密码只存 scrypt hash（control_plane/auth.py）；username 全库唯一。
    org/account 是数据边界：user 只见本 account（+本人资源），admin 见整 org，
    root 跨 org。机器通道（BOK_CP_TOKEN / node_token）与用户身份严格分离。
    """

    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    account_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")  # root/admin/user
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/disabled
    # B4 页面权限：JSON 数组串（''=默认集，读侧见 control_plane/permissions.py）。
    permissions_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


def create_all(engine) -> None:
    Base.metadata.create_all(engine)
