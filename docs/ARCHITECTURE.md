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

## 分发与可观测性（2026-09-17 修订：Tauri 退役，浏览器化）

### 节点分发（原桌面壳章节）

- **Tauri 桌面壳已退役**（2026-09-17，`desktop/` 已删除）：分发改为「节点安装脚本
  （从云 CP 鉴权下载，客户链路零 GitHub）+ schtasks/launchd 常驻 + 纯浏览器 UI」。
- **装机入口**：Windows=`scripts/install-node.ps1 -Fetch -InstallService`（自举拉包+
  Task Scheduler 常驻）；bash=`scripts/bootstrap-node.sh`。两者的包/脚本都经
  `GET /api/nodes/downloads/{pkg|runtime|bootstrap}/…`（license/node_token 端点内自证）。
- **UI 托管**：`tools/node_agent.py` 内嵌 stdlib 静态服务（`--ui-dir` 给定即自动起
  :3000，SPA 404 回 index.html）；话务员浏览器直达，刷新即新版，无客户端版本断层。
- **原 Tauri 设备层**（macOS CoreAudio 系统输出切换）随壳退役；输出切换统一走浏览器
  `setSinkId`（Chromium；Safari 回退系统默认）。
- **仓库根解析**：`BOK_ROOT` 环境变量 → 仓库根上溯（`runtime_root()` 兼容祖先查找）。

### 平台模型与首启下载

- 模型权重**不进入仓库**。`tools/bok.py download` 用 `huggingface_hub.snapshot_download`
  拉取到平台级 `app-data/models`，支持断点续传（中国网络可配 `HF_ENDPOINT` 镜像）。
- app-data：macOS `~/Library/Application Support/BokVoice`，Windows `%LOCALAPPDATA%\BokVoice`。
- `tools/bok.py manifest` 输出 JSON：平台、app-data、端口、每模型 repo + 字节 + sha256 前缀，
  供 CI 生成 `models.sha256.json`。

### 可追溯 / 可审计日志

- `packages/observability/bok_voice_obs/`：结构化 JSON 日志 + 请求关联 + 审计事件。
- **日志字段**（每行 JSON）：`ts / level / service / component / message`，可选 `event`、
  `request_id / call_id / account_id / object_id / persona_id / actor / span_id`、`data`、`exc`。
- **关联注入**：`CorrelationMiddleware` 读取/生成 `x-request-id/x-call-id/x-account-id/...`
  header，并写入响应头；Agent 通过 `call_id` 建立自己的关联。
- **落盘**：`app-data/logs/app.jsonl`（按组件、20MB × 10 滚动）；审计写入 `app-data/audit/YYYY-MM-DD.jsonl`（只追加）。
- **审计事件**：`voice.clone / settle.create / template.create|update|delete / object.* / persona.* / knowledge.import / settings.save / node.command_queued / node.artifact_downloaded`；
  `GET /api/audit` 支持按 `account_id / action / call_id` 过滤。有数据库时同步 `audit_events` 表。

### CI/CD

- **CI**（`.github/workflows/ci.yml`）：Python `compileall+pytest`、Node `realtime-translation` 测试、
  Web `tsc --noEmit` + `npm test` + 瘦客户端静态探针、`bok manifest/status` 冒烟
  （`Desktop shell (Rust)` 必检上下文已随 Tauri 退役移除——GitHub branch protection 同步删）；
  节点侧 `node-handshake.yml`：Linux 真 CP 握手（含 commands 通道+工件下载鉴权步）+
  kill-switch 探针 / Windows ps1 干跑 + 生命周期探针。
- **Release**（`.github/workflows/release.yml`）：tag `v*` 触发矩阵
  macos-14 / windows-latest：组装 runtime（`build_runtime.sh`）→
  `build_node_pkg.sh` + `build_runtime_pkg.sh` 产代码包/运行时包 → 冒烟（解包+布局+
  版本注入）→ 挂 GitHub Release（私有仓=私有资产）。操作员 `publish_node_pkg.sh`
  推云 CP 工件卷；客户升级走 commands 通道。
