# Bok Voice 架构

```text
Web (Next.js + 官方 LiveKit Agents UI 组件)
        │ REST + LiveKit token
        ▼
Control Plane (FastAPI) ── SQLAlchemy ── PostgreSQL + pgvector
        │                ├── BusinessRepository (calls/objects/personas/turns/settlements)
        │                └── KnowledgeService ── LocalMarkdownSource(vault) + SqlVectorStore(knowledge_chunks)
        │
        │  REST (get_call/object/persona, search_knowledge, add_turn, settle)
        ▼
Agent Worker (livekit-agents) ── ControlPlaneClient ── ContextInjector → LLM → TTS
        │
  LiveKit Server (Room / Session / RTC / Agent dispatch)
```

- **数据实体**：`Account`、`PersonaProfile`、`ObjectProfile`、`ObjectTopic`、`CallSession`、`Turn`、`Settlement`、`GlobalInsight`、`UsageRecord`、`KnowledgeChunk(pgvector)`。
- **知识库**：Markdown 为首选事实源（`accounts/{account}/knowledge/...`），`KnowledgeChunk` 为持久向量索引；`EmbeddingService` 抽象（生产换 BGE/ONNX，CI 用确定性 `CharHashEmbedding`）。所有查询强制 `account_id` 过滤。
- **Agent 接线**：`entrypoint(ctx)` 从 `ctx.room.name`(= call_id) 解析通话，经 `ControlPlaneClient` 拉对象/人设/知识，`ContextInjector` 组装 `Agent(instructions=...)`（只注入结构化上下文，不注入原始历史正文）。
- **turns 持久化**：`session.on("conversation_item_added")` 作为唯一来源落 user/assistant；`session.on("close")` 触发 `POST /settle`（幂等）。
- **Provider**：通过接口抽象；`SessionManifest` 会话开始锁定；通话中硬故障仅降级一次。

### 火山 TTS Provider（V3 单向流式）

- 端点：`wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream`。
- 鉴权：HTTP header `X-Api-App-Id` / `X-Api-Access-Key` / `X-Api-Resource-Id` / `X-Api-Request-Id`（旧控制台鉴权；新控制台 `X-Api-Key` 亦受支持）。
- 调用流：**无需 `StartConnection/StartSession/TaskRequest` 握手**，直接发送一帧 `FullClientRequest(NoSeq)`，payload 为 `{"user","req_params"}`，服务端以 `AudioOnlyServer/FullServerResponse` 事件回推，`TTSResponse` 的 payload 即 PCM。
- 协议：`apps/agent/agent_runtime/providers/volc_v3_protocol.py`（独立、可单测、可替换、可降级；失败回退 beep）。
- 配置：`.env` 中 `VOLC_RESOURCE_ID=seed-tts-2.0`、`VOLC_SPEAKER=zh_female_vv_uranus_bigtts`；语种/方言用 `VOLC_LANGUAGE`（如 `vi`）/`VOLC_DIALECT`（如 `yue`），需配套支持该语种的音色。
- **确定性 CI 路径**：`USE_FAKE_MEDIA=1` + `SCRIPTED_LLM=1`，`ScriptedLLM` 校验注入知识后输出指定话术；无云 API、无麦克风可跑。
- **业务端到端**：`scripts/e2e_pipeline.py` 覆盖知识落盘/检索隔离/对象人设/注入→指定话术/结算。
