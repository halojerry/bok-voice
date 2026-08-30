# TODO / Task 清单

## 基建

- [x] monorepo 骨架
- [x] docker-compose 启动验证（Postgres + LiveKit + Control Plane + Web + Agent）
- [x] Agent Worker 容器化并注册到 LiveKit
- [ ] CI（lint + pytest + next build + browser e2e）

## Backend

- [x] Provider 接口 / Fake providers
- [x] SessionManifest 与降级骨架
- [x] Control Plane 签发真实 LiveKit JWT（`/api/token`，接通时把通话置为 `active`）
- [x] DeepSeek LLM 插件（OpenAI 兼容，流式调用验证）`scripts/test_deepseek.py`
- [x] Ollama LLM 插件（本机 `huihui_ai/qwen3.5-abliterated:9b`）`scripts/test_ollama.py`
- [x] Agent 入口优先 DeepSeek，Ollama 兜底，CI 用 `ScriptedLLM`
- [x] AgentSession 完整接线：VAD + STT(`StreamAdapter`) + LLM + TTS 闭环
- [x] `ControlPlaneClient`：Agent 按 call 拉对象/人设/知识/落 turns/结算
- [x] Agent 注入知识/上下文（`ContextInjector` + `_instructions`，不含原始历史正文）
- [x] 挂断自动结算（`session.on("close")` → `/settle`，幂等）
- [x] 真实 Provider 适配器（sherpa ASR、DeepSeek LLM、火山 TTS WebSocket）
- [ ] Model Serving 服务（asr/tts/llm-service 独立部署）

## Data

- [x] SQLAlchemy 模型 + repository（Postgres 运行，SQLite 测试可跑）
- [x] Postgres/pgvector 迁移：`knowledge_chunks` + `SqlVectorStore`
- [x] `EmbeddingService` 接口 + 确定性 `CharHashEmbedding`（CI 不依赖模型/API）
- [x] Bok MarkdownSource（`LocalMarkdownSource` 写 vault）+ 导入/检索
- [x] 知识按 `account_id` 隔离（pipeline E2E 验证）
- [x] 通话状态持久化：`update_call(status/escalated_to_human/disposition)`
- [ ] SettlementWorker 幂等完整实现（含对象主题合并、全局洞察蒸馏）

## Frontend

- [x] Next.js 骨架 + API client
- [x] 统一官方风格工作台 Shell（侧边栏 + 顶栏 + 深色卡片）
- [x] 通话台对接官方 LiveKit 组件：`BarVisualizer` + `useVoiceAssistant` + `useTranscriptions` + `RoomAudioRenderer` + `VoiceAssistantControlBar`
- [x] 左/右业务面板接真实数据（对象卡/人设/指标/结算）
- [x] 新建通话走 `POST /api/calls` → 用真实 call_id 作 room；`/calls/[id]` 按 id 回放
- [x] 挂断→`hangup`+`settle` 自动沉淀
- [x] 对象/知识/人设/报表/主管台/设置页面（接真实 API）
- [x] 主管台按钮接 `join/pause/takeover/transfer` API
- [ ] SSE 推送（实时状态/结算完成通知）

## 质量

- [x] 单元/契约测试（pytest 9 cases）
- [x] HTTP 端到端（`scripts/e2e_http.py`）
- [x] 确定性业务端到端（`scripts/e2e_pipeline.py`：知识落盘/检索隔离/对象人设/注入→指定话术/结算）
- [x] 浏览器 E2E（`tools/browser-e2e`：进房 + Agent 派发 + 挂断）
- [ ] 组件 / WebRTC 真实语音 / 负载测试（20–50 路）

## 关键的已验证链路

- [x] sherpa SenseVoice（含粤）本地 + 容器内转写通过
- [x] DeepSeek 流式调用、火山 TTS V3 单向流式收到真实 PCM（`volc_v3_protocol.py` + 单元测试）
- [x] `FAKE_VAD→FAKE_STT_FINAL→LLM→TTS_PUSH` 确定性语音闭环
- [x] Agent 事件接线：`conversation_item_added` 落 user/assistant（唯一来源，去重），`close` 自动结算

## 尚未完成（下一里程碑）

- [ ] 用真实麦克风/真实音频源做 WebRTC 真人回环验证（Silero VAD + sherpa + DeepSeek + 火山出声）
- [x] 火山 TTS 从 V1 迁移到 V3（单向流式：`StartConnection` 无需握手，直接发一帧 `FullClientRequest` 收到 PCM；`X-Api-App-Id`/`X-Api-Access-Key`/`X-Api-Resource-Id`/`X-Api-Request-Id` 鉴权）
- [ ] Agent `entrypoint` 收尾：显式 `session.end()` / `finally`，消除 `did not exit in time` 告警
- [ ] 本地 VAD 低延迟优化（Silero CPU 较慢，考虑 GPU/降采样/分段）
- [ ] 全局洞察蒸馏 + 对象历史主题合并
- [ ] 主管台 whisper（私语）
- [ ] 20–50 路并发压测 + 容量告警

## 2026-08-30 新里程碑（Qwen3-ASR/TTS + B 线同传）

- [x] Qwen3-ASR sidecar（transformers/MPS 本地 + vLLM Docker 路径）`scripts/start_sidecars.sh` / `stop_sidecars.sh` / `smoke_sidecars.py` 全绿
- [x] Qwen3-TTS sidecar：预置音色 + 三语言克隆 + 试听（设置/人设页）
- [x] A 线三语 E2E `TRILINGUAL_E2E 3/3 PASSED`（zh/yue/en 转写 + 回复 + 语言跟随）
- [x] Ollama 原生 `/api/chat` + `think:false`（修复 9B thinking 慢/空回复）
- [x] control-plane 每请求独立 Session + turn 幂等（修复并发 500）
- [x] B 线 worker（ws://:8790）：`TranslationChannel` + `PlaybackScheduler`（金喜同传字段）+ 真实 Qwen3-ASR/Ollama/Qwen3-TTS providers + WebSocket 协议 + metrics JSONL
- [x] B 线 Web 面板 `/translate`（多通道、双字幕、调度指标、WebAudio 播放）
- [ ] Windows/WSL2 真机验证（vLLM ASR + TTS 容器）——脚本/镜像已就绪，待 Windows 环境
- [ ] B 线真机麦克风听感验收（页面已可连 worker，需人工说话验证）
- [ ] `system-audio-helper`（桌面系统音频采集）——只留接口，未实现
