# Bok Voice — LiveKit 多账号客服语音助手

本地优先、可生产化、多账号的实时业务语音助手。基座：LiveKit Agents。MVP：浏览器 WebRTC + 本地自托管 LiveKit Server；未来接 SIP/PSTN，目标支撑 20–50 路并发客服。

## 核心概念

三种身份：账号 / 对象 / AI 我方人设。两种通话模式：`simulation`（AI 扮演对象、对话人为操作员，用于训练）、`live`（AI 扮演我方、对话人为对象/客户，用于生产）。业务与向量数据用 PostgreSQL + pgvector，Bok 作为 Markdown 事实源与知识治理。

## 目录结构

```text
apps/agent/           # LiveKit Agent Server
apps/control-plane/   # FastAPI REST + 业务逻辑
apps/web/             # Next.js Web
packages/core/        # 领域模型 + 接口
packages/business-db/ # SQLAlchemy / Postgres 模型 + repository
packages/knowledge/   # KnowledgeService / MarkdownSource / VectorStore
services/livekit-server/  # LiveKit 配置
docs/                 # PRD / PLAN / ARCHITECTURE / CONTRACTS / ISSUES / TODO
tests/                # Python 测试
```

## 快速开始（后端）

```bash
# 一次建环境 + 装依赖（需要 Python >=3.11）
./scripts/bootstrap.sh

# 跑测试（无需模型 / 无需 LiveKit / 无需 Postgres，默认内存 repo + fake provider）
./scripts/test.sh
```

注意：`apps/agent` 的真实 LiveKit 运行依赖需单独安装：`pip install -e "apps/agent[livekit]"`。

## 快速开始（Docker 基础设施）

```bash
# 启动全部服务：Postgres + LiveKit + Control Plane + Web + Agent Worker
docker compose up -d --build
```

如果第三方镜像源限流导致 `--build` 拉不到基础镜像，用离线重建脚本（从现有镜像派生本地
`python:3.12-slim` / `node:22-alpine` 基础镜像，再走标准 Dockerfile 构建；pip/PyPI 需可达）：

```bash
./scripts/rebuild_images_offline.sh
docker compose up -d --force-recreate agent web control-plane
```

默认监听：

- Web：`http://localhost:3000`
- Control Plane：`http://localhost:8000`
- Agent Worker：注册到 LiveKit（日志见 `docker compose logs agent`）
- LiveKit：`7880`（HTTP）、`7881`（RTC UDP）、`7882`（RTC UDP）
- Postgres：`5432`

运行端到端 HTTP 测试（依赖 8000 端口上的 Control Plane）：

```bash
python scripts/e2e_http.py
```

预期输出以 `E2E HTTP PASSED` 结束。当前覆盖：对象建档、知识导入/检索与账号隔离、创建通话、写入 Turn、结算、主管命令。

浏览器级 E2E（需 Playwright + Chromium，位于 `tools/browser-e2e`）：

```bash
cd tools/browser-e2e && npm install && npx playwright install chromium && node e2e.mjs
```

脚本会打开 `http://localhost:3000/calls/new`，点“接通→进房→挂断”，验证 `BROWSER_E2E_PASSED` 且日志出现 `participant joined: agent-...`（Agent 被 LiveKit 派发并进房）。

当前已验证：所有服务在 Docker 内运行、Python 测试通过、HTTP 端到端通过、Agent Worker 注册到本地 LiveKit、浏览器进房触发 Agent 派发。真实语音内容（STT/LLM/TTS）仍依赖 Provider Key/本地模型，为下一里程碑。

## 语音模型 sidecar（Qwen3-ASR / Qwen3-TTS）

Agent 的 ASR 与 TTS 走两个本地 HTTP sidecar（macOS 开发机直接跑在宿主机；Windows 生产用 WSL2 + vLLM，见 `services/qwen3-asr-sidecar/docker-compose.qwen3-asr.yml`）。

```bash
# 首次：建 venv 并安装依赖（模型权重已放在 data/models/）
services/qwen3-asr-sidecar/setup-macos.sh
services/qwen3-tts-sidecar/setup-macos.sh

# 启动（幂等，模型加载 1–5 分钟）
./scripts/start_sidecars.sh

# 验证：三语言转写 + 预置/克隆合成 + 克隆粤语音色回灌
./scripts/smoke_sidecars.py

# 停止
./scripts/stop_sidecars.sh
```

端口：ASR `8787`、TTS `8788`。Control Plane 的 `/api/asr/health`、`/api/tts/health` 与设置/人设页的「克隆/试听」都通过这两个 sidecar 工作。

三语浏览器 E2E（普通话/粤语/英语，注入假麦克风音频）：

```bash
cd tools/browser-e2e && node trilingual.mjs   # 期望 TRILINGUAL_E2E 3/3 PASSED
```

## 状态

当前为可运行、可测试的工程骨架：接口、领域模型、repository、control-plane、LiveKit agent 骨架、Web 基座均以 Fake/占位实现保证无 Key 可跑。真实 sherpa / GPT-SoVITS / 火山 / 讯飞 / DeepSeek / Ollama 分别通过对应 Provider 适配器接入。
