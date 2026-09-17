-- ============================================================================
-- Bok Voice control-plane 全量 schema —— P0 引导件(**生成物,勿手改**)
-- ============================================================================
-- 生成方式: scripts/dump_postgres_ddl.py
--   1) 对源库真跑 control_plane.deps.build_engine()(建表 + 幂等补列迁移 + 数据迁移);
--   2) 源容器内 pg_dump --schema-only 导出(见下方"源命令")。
--   schema 唯一真源 = packages/business-db ORM 模型 + deps.build_engine() 的幂等迁移;
--   **改表后必须重跑本脚本重新生成**,再应用到 Supabase。
--
-- 生成日期: 2026-09-18
-- 源镜像:   pgvector/pgvector:pg16
-- 源命令:   docker exec pg-ddl pg_dump -U postgres --schema-only --no-owner --no-privileges postgres
-- 回环校验: pgvector/pgvector:pg16 上应用本文件 + 重跑 build_engine() = 零 DDL 变更(生成时实测)
-- 规模:     CREATE TABLE 25 张 / CREATE INDEX 36 条 / 数据语句 0 条
--           (--schema-only:正常应 0 条数据语句;带 DEFAULT/COMMENT 属 schema 本身)
--
-- 目标: 全新 Supabase(Postgres)项目首次引导。应用方式(Main 线程):
--   psql/SQL Editor 整文件执行(--no-owner --no-privileges,对象归执行者所有)。
--
-- 幂等性: 首次应用即可;重复应用会因 CREATE TABLE 已存在报错——后续变更请直接依赖
--   build_engine() 的启动幂等迁移,不要重复整文件执行。
--
-- pgvector 依赖(重要):
--   * knowledge_chunks.embedding 是 vector(384),故本文件含
--     `CREATE EXTENSION IF NOT EXISTS vector`。必须落在带 pgvector 的目标库上
--     (Supabase 内置,无需安装)。
--   * 若目标库把扩展装在别的 schema,可先手动
--     `create extension if not exists vector with schema extensions;`
--     再执行本文件(IF NOT EXISTS 会自动跳过重复创建)。
--
-- 数据面(本文件不含,由 CP 启动时幂等补齐):
--   * 垫话罐头 filler_entries 种子(三语×六场景)由 build_engine() 首启灌入;
--   * root 用户由 BOK_ROOT_USERNAME/BOK_ROOT_PASSWORD 置定时的 _seed_root_user() 建;
--   * 其余业务数据(账号/对象/话术/通话)按运营在 web 侧录入。
-- ============================================================================
--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15 (Debian 16.15-1.pgdg12+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg12+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts (
    id character varying(64) NOT NULL,
    org_id character varying(64) NOT NULL,
    display_name character varying(255) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: audit_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_events (
    id character varying(64) NOT NULL,
    ts character varying(40) NOT NULL,
    action character varying(64) NOT NULL,
    subject_type character varying(64) NOT NULL,
    subject_id character varying(128) NOT NULL,
    actor character varying(128) NOT NULL,
    outcome character varying(32) NOT NULL,
    detail_json text NOT NULL,
    request_id character varying(64) NOT NULL,
    call_id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    object_id character varying(64) NOT NULL,
    persona_id character varying(64) NOT NULL
);


--
-- Name: call_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.call_sessions (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    object_id character varying(64) NOT NULL,
    persona_id character varying(64) NOT NULL,
    template_id character varying(64) NOT NULL,
    created_by character varying(64) NOT NULL,
    mode character varying(32) NOT NULL,
    direction character varying(32) NOT NULL,
    language character varying(16) NOT NULL,
    contact_phone character varying(64) NOT NULL,
    consent_status character varying(32) NOT NULL,
    recording_enabled boolean NOT NULL,
    disposition character varying(64) NOT NULL,
    escalated_to_human boolean NOT NULL,
    status character varying(32) NOT NULL,
    whatsapp_status character varying(16) NOT NULL,
    customer_whatsapp character varying(64) NOT NULL,
    kind character varying(32) NOT NULL,
    target_lang character varying(16) NOT NULL,
    glossary text NOT NULL,
    voices_json text NOT NULL,
    session_report text NOT NULL,
    session_reports_json text DEFAULT '[]'::text NOT NULL,
    started_at timestamp without time zone,
    ended_at timestamp without time zone,
    duration_s integer DEFAULT 0 NOT NULL,
    node_id character varying(64) DEFAULT ''::character varying NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: campaign_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.campaign_items (
    id character varying(64) NOT NULL,
    campaign_id character varying(64) NOT NULL,
    seq integer NOT NULL,
    object_id character varying(64) NOT NULL,
    phone character varying(64) NOT NULL,
    status character varying(16) NOT NULL,
    call_id character varying(64) NOT NULL,
    attempts integer NOT NULL,
    last_error character varying(255) NOT NULL,
    scenario character varying(16) NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.campaigns (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    template_id character varying(64) NOT NULL,
    persona_id character varying(64) NOT NULL,
    language character varying(16) NOT NULL,
    status character varying(16) NOT NULL,
    gap_seconds integer NOT NULL,
    scripts_json text NOT NULL,
    site_id character varying(64) NOT NULL,
    call_windows_json text DEFAULT '[]'::text NOT NULL,
    max_concurrency integer DEFAULT 1 NOT NULL,
    redispatch_json text DEFAULT ''::text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    finished_at timestamp without time zone
);


--
-- Name: conversation_template_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation_template_revisions (
    id character varying(64) NOT NULL,
    template_id character varying(64) NOT NULL,
    revision integer NOT NULL,
    snapshot text NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: conversation_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation_templates (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    opening text NOT NULL,
    core text NOT NULL,
    objection text NOT NULL,
    closing text NOT NULL,
    tone_override character varying(255) NOT NULL,
    language character varying(16) NOT NULL,
    steps_json text NOT NULL,
    hotwords text NOT NULL,
    owner_user_id character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: filler_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.filler_entries (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    lang character varying(16) NOT NULL,
    category character varying(16) NOT NULL,
    text text NOT NULL,
    triggers text NOT NULL,
    voice_id character varying(128) NOT NULL,
    priority integer NOT NULL,
    per_call_cap integer NOT NULL,
    enabled boolean NOT NULL,
    hit_count integer NOT NULL,
    source character varying(16) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: global_insights; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.global_insights (
    id character varying(64) NOT NULL,
    kind character varying(64) NOT NULL,
    statement text NOT NULL,
    confidence double precision NOT NULL,
    language character varying(16) NOT NULL,
    status character varying(32) NOT NULL
);


--
-- Name: global_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.global_settings (
    id character varying(64) NOT NULL,
    asr_json text NOT NULL,
    llm_json text NOT NULL,
    tts_json text NOT NULL,
    vad_json text NOT NULL,
    sip_json text NOT NULL,
    campaign_json text DEFAULT ''::text NOT NULL,
    policy character varying(64) NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: knowledge_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_chunks (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    text text NOT NULL,
    path character varying(512) NOT NULL,
    source character varying(64) NOT NULL,
    content_hash character varying(64) NOT NULL,
    embedding public.vector(384) NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
);


--
-- Name: node_commands; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node_commands (
    id character varying(64) NOT NULL,
    node_id character varying(64) NOT NULL,
    action character varying(32) NOT NULL,
    target_version character varying(64) DEFAULT ''::character varying NOT NULL,
    args_json text NOT NULL,
    status character varying(16) NOT NULL,
    result character varying(255) DEFAULT ''::character varying NOT NULL,
    created_by character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    delivered_at timestamp without time zone,
    closed_at timestamp without time zone
);


--
-- Name: node_licenses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node_licenses (
    id character varying(64) NOT NULL,
    org_id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    key_hash character varying(128) NOT NULL,
    max_nodes integer NOT NULL,
    note character varying(255) NOT NULL,
    status character varying(16) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nodes (
    id character varying(64) NOT NULL,
    org_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    token_hash character varying(128) NOT NULL,
    platform character varying(32) NOT NULL,
    version character varying(64) NOT NULL,
    status character varying(16) NOT NULL,
    metrics_json text NOT NULL,
    last_seen_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    license_id character varying(64) NOT NULL,
    fingerprint character varying(128) NOT NULL,
    revoked_source character varying(16) DEFAULT ''::character varying NOT NULL,
    revoked_at character varying(32) DEFAULT ''::character varying NOT NULL
);


--
-- Name: object_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.object_profiles (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    display_name character varying(255) NOT NULL,
    role_template character varying(64) NOT NULL,
    language character varying(16) NOT NULL,
    background text NOT NULL,
    phone character varying(64) NOT NULL,
    tracking_no character varying(64) NOT NULL,
    courier character varying(64) NOT NULL,
    address character varying(255) NOT NULL,
    contact_channel character varying(32) NOT NULL,
    digest text NOT NULL,
    template_id character varying(64) NOT NULL,
    status character varying(32) NOT NULL
);


--
-- Name: object_topics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.object_topics (
    id character varying(64) NOT NULL,
    object_id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    topic text NOT NULL,
    summary text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: orgs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orgs (
    id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    status character varying(16) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: persona_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.persona_profiles (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    company character varying(255) NOT NULL,
    tone text NOT NULL,
    language character varying(16) NOT NULL,
    reference_audio character varying(512) NOT NULL,
    tts_provider character varying(32) NOT NULL
);


--
-- Name: qa_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qa_entries (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    owner_user_id character varying(64) NOT NULL,
    question_text text NOT NULL,
    answer_text text NOT NULL,
    lang character varying(16) NOT NULL,
    scope character varying(16) NOT NULL,
    step_index integer NOT NULL,
    voice_id character varying(128) NOT NULL,
    enabled boolean NOT NULL,
    hit_count integer NOT NULL,
    source character varying(16) NOT NULL,
    cluster_head_id character varying(64) NOT NULL,
    template_id character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: roster_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.roster_entries (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    call_id character varying(64) NOT NULL,
    object_id character varying(64) NOT NULL,
    channel character varying(16) NOT NULL,
    number character varying(64) NOT NULL,
    display_name character varying(255) NOT NULL,
    summary text NOT NULL,
    status character varying(16) NOT NULL,
    claimed_by character varying(64) NOT NULL,
    claimed_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: settlements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.settlements (
    id character varying(64) NOT NULL,
    call_id character varying(64) NOT NULL,
    status character varying(32) NOT NULL,
    metrics_json text NOT NULL,
    summary text NOT NULL,
    transcript_doc_path character varying(512) NOT NULL,
    settlement_doc_path character varying(512) NOT NULL,
    new_topics_json text NOT NULL,
    global_insight_id character varying(64) NOT NULL,
    error text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: sip_sites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sip_sites (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    name character varying(255) NOT NULL,
    livekit_url character varying(512) NOT NULL,
    sip_edge character varying(16) NOT NULL,
    trunk_id character varying(64) NOT NULL,
    numbers_json text NOT NULL,
    region character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: turns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.turns (
    id character varying(64) NOT NULL,
    call_id character varying(64) NOT NULL,
    turn_id character varying(64) NOT NULL,
    role character varying(32) NOT NULL,
    transcript text NOT NULL,
    emotion character varying(64) NOT NULL,
    provider character varying(64) NOT NULL,
    latency_ms integer NOT NULL,
    language character varying(32) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    org_id character varying(64) NOT NULL,
    line character varying(8) NOT NULL,
    speaker character varying(32) NOT NULL,
    gen character varying(16) NOT NULL,
    template_step integer NOT NULL,
    started_ms integer NOT NULL,
    ended_ms integer NOT NULL,
    perceived_ms integer NOT NULL
);


--
-- Name: usage_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_records (
    id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    call_id character varying(64) NOT NULL,
    provider character varying(64) NOT NULL,
    kind character varying(64) NOT NULL,
    units integer NOT NULL,
    tokens integer NOT NULL,
    audio_seconds double precision NOT NULL,
    latency_ms integer NOT NULL,
    cost_estimate double precision NOT NULL,
    status character varying(32) NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id character varying(64) NOT NULL,
    org_id character varying(64) NOT NULL,
    account_id character varying(64) NOT NULL,
    username character varying(64) NOT NULL,
    password_hash character varying(255) NOT NULL,
    display_name character varying(255) NOT NULL,
    role character varying(16) NOT NULL,
    status character varying(16) NOT NULL,
    permissions_json text NOT NULL,
    created_at timestamp without time zone NOT NULL
);


--
-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);


--
-- Name: audit_events audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT audit_events_pkey PRIMARY KEY (id);


--
-- Name: call_sessions call_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.call_sessions
    ADD CONSTRAINT call_sessions_pkey PRIMARY KEY (id);


--
-- Name: campaign_items campaign_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaign_items
    ADD CONSTRAINT campaign_items_pkey PRIMARY KEY (id);


--
-- Name: campaigns campaigns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaigns
    ADD CONSTRAINT campaigns_pkey PRIMARY KEY (id);


--
-- Name: conversation_template_revisions conversation_template_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_template_revisions
    ADD CONSTRAINT conversation_template_revisions_pkey PRIMARY KEY (id);


--
-- Name: conversation_templates conversation_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_templates
    ADD CONSTRAINT conversation_templates_pkey PRIMARY KEY (id);


--
-- Name: filler_entries filler_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.filler_entries
    ADD CONSTRAINT filler_entries_pkey PRIMARY KEY (id);


--
-- Name: global_insights global_insights_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.global_insights
    ADD CONSTRAINT global_insights_pkey PRIMARY KEY (id);


--
-- Name: global_settings global_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.global_settings
    ADD CONSTRAINT global_settings_pkey PRIMARY KEY (id);


--
-- Name: knowledge_chunks knowledge_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunks
    ADD CONSTRAINT knowledge_chunks_pkey PRIMARY KEY (id);


--
-- Name: node_commands node_commands_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_commands
    ADD CONSTRAINT node_commands_pkey PRIMARY KEY (id);


--
-- Name: node_licenses node_licenses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node_licenses
    ADD CONSTRAINT node_licenses_pkey PRIMARY KEY (id);


--
-- Name: nodes nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nodes
    ADD CONSTRAINT nodes_pkey PRIMARY KEY (id);


--
-- Name: object_profiles object_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.object_profiles
    ADD CONSTRAINT object_profiles_pkey PRIMARY KEY (id);


--
-- Name: object_topics object_topics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.object_topics
    ADD CONSTRAINT object_topics_pkey PRIMARY KEY (id);


--
-- Name: orgs orgs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orgs
    ADD CONSTRAINT orgs_pkey PRIMARY KEY (id);


--
-- Name: persona_profiles persona_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persona_profiles
    ADD CONSTRAINT persona_profiles_pkey PRIMARY KEY (id);


--
-- Name: qa_entries qa_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qa_entries
    ADD CONSTRAINT qa_entries_pkey PRIMARY KEY (id);


--
-- Name: roster_entries roster_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roster_entries
    ADD CONSTRAINT roster_entries_pkey PRIMARY KEY (id);


--
-- Name: settlements settlements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.settlements
    ADD CONSTRAINT settlements_pkey PRIMARY KEY (id);


--
-- Name: sip_sites sip_sites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sip_sites
    ADD CONSTRAINT sip_sites_pkey PRIMARY KEY (id);


--
-- Name: turns turns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_pkey PRIMARY KEY (id);


--
-- Name: usage_records usage_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_records
    ADD CONSTRAINT usage_records_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: ix_accounts_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_accounts_org_id ON public.accounts USING btree (org_id);


--
-- Name: ix_audit_events_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_action ON public.audit_events USING btree (action);


--
-- Name: ix_audit_events_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_request_id ON public.audit_events USING btree (request_id);


--
-- Name: ix_audit_events_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_events_ts ON public.audit_events USING btree (ts);


--
-- Name: ix_call_sessions_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_call_sessions_account_id ON public.call_sessions USING btree (account_id);


--
-- Name: ix_call_sessions_object_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_call_sessions_object_id ON public.call_sessions USING btree (object_id);


--
-- Name: ix_campaign_items_call_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_campaign_items_call_id ON public.campaign_items USING btree (call_id);


--
-- Name: ix_campaign_items_campaign_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_campaign_items_campaign_id ON public.campaign_items USING btree (campaign_id);


--
-- Name: ix_campaigns_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_campaigns_account_id ON public.campaigns USING btree (account_id);


--
-- Name: ix_conversation_template_revisions_template_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_conversation_template_revisions_template_id ON public.conversation_template_revisions USING btree (template_id);


--
-- Name: ix_conversation_templates_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_conversation_templates_account_id ON public.conversation_templates USING btree (account_id);


--
-- Name: ix_filler_entries_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_filler_entries_account_id ON public.filler_entries USING btree (account_id);


--
-- Name: ix_knowledge_chunks_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_knowledge_chunks_account_id ON public.knowledge_chunks USING btree (account_id);


--
-- Name: ix_node_commands_node_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_commands_node_id ON public.node_commands USING btree (node_id);


--
-- Name: ix_node_commands_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_commands_status ON public.node_commands USING btree (status);


--
-- Name: ix_node_licenses_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_licenses_account_id ON public.node_licenses USING btree (account_id);


--
-- Name: ix_node_licenses_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_node_licenses_org_id ON public.node_licenses USING btree (org_id);


--
-- Name: ix_nodes_license_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_nodes_license_id ON public.nodes USING btree (license_id);


--
-- Name: ix_nodes_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_nodes_org_id ON public.nodes USING btree (org_id);


--
-- Name: ix_object_profiles_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_object_profiles_account_id ON public.object_profiles USING btree (account_id);


--
-- Name: ix_object_topics_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_object_topics_account_id ON public.object_topics USING btree (account_id);


--
-- Name: ix_object_topics_object_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_object_topics_object_id ON public.object_topics USING btree (object_id);


--
-- Name: ix_persona_profiles_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_persona_profiles_account_id ON public.persona_profiles USING btree (account_id);


--
-- Name: ix_qa_entries_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_qa_entries_account_id ON public.qa_entries USING btree (account_id);


--
-- Name: ix_roster_entries_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_roster_entries_account_id ON public.roster_entries USING btree (account_id);


--
-- Name: ix_roster_entries_call_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_roster_entries_call_id ON public.roster_entries USING btree (call_id);


--
-- Name: ix_roster_entries_object_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_roster_entries_object_id ON public.roster_entries USING btree (object_id);


--
-- Name: ix_settlements_call_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_settlements_call_id ON public.settlements USING btree (call_id);


--
-- Name: ix_sip_sites_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sip_sites_account_id ON public.sip_sites USING btree (account_id);


--
-- Name: ix_turns_call_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_turns_call_id ON public.turns USING btree (call_id);


--
-- Name: ix_turns_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_turns_org_id ON public.turns USING btree (org_id);


--
-- Name: ix_turns_turn_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_turns_turn_id ON public.turns USING btree (turn_id);


--
-- Name: ix_usage_records_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_records_account_id ON public.usage_records USING btree (account_id);


--
-- Name: ix_usage_records_call_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_records_call_id ON public.usage_records USING btree (call_id);


--
-- Name: ix_users_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_users_account_id ON public.users USING btree (account_id);


--
-- Name: ix_users_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_users_org_id ON public.users USING btree (org_id);


--
-- Name: ix_users_username; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_users_username ON public.users USING btree (username);


--
-- Name: uq_nodes_license_fingerprint; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_nodes_license_fingerprint ON public.nodes USING btree (license_id, fingerprint) WHERE ((license_id)::text <> ''::text);


--
-- PostgreSQL database dump complete
--


