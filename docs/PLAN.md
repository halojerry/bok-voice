# Bok Voice 技术计划（截至当前实现）

## M1 — LiveKit 本地自托管 + WebRTC 最小会话 [完成]

- docker-compose：LiveKit Server + Postgres(+pgvector) + Redis(生产) + Control Plane + Web + Agent Worker。
- Agent Worker 注册到 LiveKit；浏览器 WebRTC 进房 + Agent 派发验证通过（`tools/browser-e2e`）。
- 验收：浏览器连接、进房、派发、挂断。

## M2 — 业务数据 + 知识库 [基本完成]

- SQLAlchemy 模型 + repository（Postgres 运行，SQLLite 测试可跑）。
- Postgres/pgvector schema；`knowledge_chunks` 表 + `SqlVectorStore`（持久向量索引）。
- `EmbeddingService` 接口（生产可换 BGE/ONNX；CI 用确定性 `CharHashEmbedding`，`dim=384`）。
- Bok `LocalMarkdownSource` 写入 vault + 导入/检索接口；账号隔离测试通过。

## M3 — 五层编排 + Provider 降级 [部分完成]

- `AgentSession` 接线：VAD + STT(`StreamAdapter` 包 sherpa) + LLM + TTS。
- ASR：sherpa SenseVoice（含粤）；LLM：DeepSeek（优先）/ Ollama / ScriptedLLM（CI）；TTS：火山流式（可降级 beep）。
- `ContextInjector` 从 `ControlPlaneClient` 拉对象/人设/知识 → 注入指令（只注入结构化对象卡/人设/产品片段，不含原始历史正文）。
- `ScriptedLLM`：确定性路径按「注入知识命中 `EXPECT_KW` → 输出指定话术」。
- `SessionManifest` + `ProviderRegistry`（降级状态机为骨架）。

## M4 — 结算 + 全局蒸馏 [部分完成]

- `SettlementTrigger` 纯函数指标（填充/犹豫/密度/情绪分布）。
- 挂断自动结算：`session.on("close")` → `/settle`（幂等），落通话文档/结算文档路径。
- 对象历史主题合并与全局洞察脱敏蒸馏：待完成。

## M5 — 主管台 + 双模式 [部分完成]

- 主管台 UI：旁听/暂停/接管/转人工（已接真实 API；whisper 后续）。
- `simulation` / `live` 双模式。

## M6 — SIP / 生产化 [未开始]

- LiveKit SIP trunk、Egress、扩容压测（20–50 路）、安全合规。
