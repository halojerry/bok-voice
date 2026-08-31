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

---

## 桌面分发与可观测性（v0.2）

### 桌面壳（Tauri）

- **位置**：`desktop/`，Tauri v2 应用：`desktop/src-tauri`（Rust 编排）+ `desktop/src/bridge.ts`（前端桥）。
- **启动模型**：打开应用即拉起本机服务，**不做开机自启**。Rust 侧 `setup` 调用
  `python tools/bok.py serve`，随后轮询 `:3000/:8000/:8787/:8788/:1235/:8790` 健康度并推送事件。
- **主窗口**：指向 `http://localhost:3000`（网页工作台）；Web 端在浏览器模式会自动回退为普通页面。
- **命令**：`health` / `start` / `stop` / `open_logs` / `manifest`，供前端设置页「本机桌面服务」面板调用。
- **仓库根解析**：`BOK_ROOT` 环境变量 → Tauri `resource_dir` → `CARGO_MANIFEST_DIR` 上溯（dev）。

### 平台模型与首启下载

- 模型权重**不进入仓库**。`tools/bok.py download` 用 `huggingface_hub.snapshot_download`
  拉取到平台级 `app-data/models`，支持断点续传。
- app-data：macOS `~/Library/Application Support/BokVoice`，Windows `%LOCALAPPDATA%\BokVoice`。
- `tools/bok.py manifest` 输出 JSON：平台、app-data、端口、每模型 repo + 字节 + sha256 前缀，
  供 CI 生成 `models.sha256.json` 与桌面「已安装清单」。

### 可追溯 / 可审计日志

- `packages/observability/bok_voice_obs/`：结构化 JSON 日志 + 请求关联 + 审计事件。
- **日志字段**（每行 JSON）：`ts / level / service / component / message`，可选 `event`、
  `request_id / call_id / account_id / object_id / persona_id / actor / span_id`、`data`、`exc`。
- **关联注入**：`CorrelationMiddleware` 读取/生成 `x-request-id/x-call-id/x-account-id/...`
  header，并写入响应头；Agent 通过 `call_id` 建立自己的关联。
- **落盘**：`app-data/logs/app.jsonl`（按组件、20MB × 10 滚动）；审计写入 `app-data/audit/YYYY-MM-DD.jsonl`（只追加）。
- **审计事件**：`voice.clone / settle.create / template.create|update|delete / object.* / persona.* / knowledge.import / settings.save`；
  `GET /api/audit` 支持按 `account_id / action / call_id` 过滤。有数据库时同步 `audit_events` 表。

### CI/CD

- **CI**（`.github/workflows/ci.yml`）：Python `compileall+pytest`、Node `realtime-translation` 测试、
  Web `tsc --noEmit`、`docker compose config+build`、`bok manifest/status` 冒烟。
- **Release**（`.github/workflows/release.yml`）：tag `v*` 触发矩阵
  macos-14(dmg) / windows-latest(msi)；`build_release.sh` 构建 web、跑测试、派生图标、
  生成 `models.sha256.json`；`tauri build` 产安装包并上传 artifact。
