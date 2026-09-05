# Bok Voice 仓库地图（REPO MAP）

> 目录 → 用途 → 归属（dev / packaged / both）→ 关键文件。与 `RUNTIME_TOPOLOGY.md`（端口/数据流）搭配读。
> 本文随结构/端口变化更新；删文件先查本图引用。

## 根目录

| 路径 | 用途 | 归属 |
|---|---|---|
| `tools/bok.py` | 编排/模型/健康/自检唯一入口（serve/status/down/doctor/prod/download） | both |
| `scripts/` | CI 构建、E2E、延迟测量、smoke、sidecar 启动（见下「脚本」） | dev/CI |
| `apps/agent/` | LiveKit agent 运行时（A 线客服 + B 线同传 worker） | both |
| `apps/control-plane/` | FastAPI 业务服务 :8000（对象/人设/知识/话术/通话/审计/token/webhook） | both |
| `apps/web/` | Next.js 静态导出（Tauri 托管 UI：calls/interpret/supervisor/objects/personas/settings…） | both |
| `packages/core/` | 领域模型 + 策略（`bok_voice_core`：policies/types） | both |
| `packages/business-db/` | SQLAlchemy 仓库（`bok_voice_business_db`：global_settings 默认等） | both |
| `packages/knowledge/` | 知识服务 / Markdown / 向量（沉淀知识库） | both |
| `packages/observability/` | 结构化日志 + 审计（`bok_voice_obs`） | both |
| `services/qwen3-asr-sidecar/` | ASR HTTP sidecar :8787（`app.py`：start/chunk/finish、partial 滑窗 + 增量 finish） | both |
| `services/qwen3-tts-sidecar/` | TTS HTTP sidecar :8788（`app.py`：克隆音色/流式；本地回退档） | both |
| `services/llm-mlx/` | 仅 `.venv`（mlx_lm 0.31.3）；server 启动命令在 bok.py，无仓库代码 | dev |
| `services/realtime-translation/` | B 线 v1 同传 :8790（Node，冻结留 POC） | both(旧) |
| `services/livekit-server/` | `livekit.yaml`（self-host 配置，钉端口/prometheus/json 日志） | both |
| `desktop/` | Tauri 桌壳 + runtime 装配（src 前端源 / src-tauri Rust / runtime symlink） | packaged |
| `tests/` | pytest 全量（含 `fixtures/audio/{zh,cantonese,en}.wav` E2E 音频 + 术语门禁） | dev/CI |
| `docs/` | RUNTIME_TOPOLOGY / REPO_MAP / CONTRACTS / DEV_TOOLS / archive 决策归档 | dev |
| `dev/docker/` | 可选 Docker 开发栈（归档，不进 CI） | dev-only |

## apps/agent（agent_runtime）

| 文件 | 职责 |
|---|---|
| `main.py` | worker 入口（A 线 `bok-voice`） |
| `agent.py` | A 线装配：语言钉定/ASR/LLM/抢跑/打断/话术推进 hook/心跳/收尾 |
| `interpret.py` | B 线同传 worker（fwd/rev，Hy-MT2 :1236 + MiniMax 三语音色） |
| `flow.py` | 话术分步推进引擎（FlowController/rule_verdict/should_auto_advance） |
| `control_plane.py` | CP HTTP 客户端 |
| `web_search.py` | 联网检索（默认关） |
| `providers/livekit_plugins.py` | 本地模型插件：LanguageState/PinnedLanguageState/ContextState(前缀/尾/对象档案)/MlxLlmLLM/DeepSeekLLM/StatelessMTLLM/Qwen3ASR(STT/流式句级)/Qwen3TTS/MiniMaxTTS(classic 池+bidi 实验)/VolcanoTTS |
| `providers/registry.py` `fakes.py` `volc_v3_protocol.py` | 插件注册 / 测试 fake / Volcano 协议 |
| `plugins/` | 上下文/情绪/知识/结算子模块（context/emotion/knowledge/settlement） |

## 脚本（scripts/）

- 构建：`bootstrap.sh` `test.sh` `build_livekit.sh` `build_runtime.sh` `build_release.sh` `verify_bundle.sh`（--staging/--app/--doctor）`stub_external_bin.sh`
- E2E：`e2e_trilingual_livekit.py`（三语，真 /api/token，一案一通话）`e2e_flow_scenario.py` `e2e_multi_turn.py` `e2e_http.py` `e2e_pipeline.py`
- 测量/探针：`measure_latency.py`（需真栈）`measure_prompt.py`（本地）`probe_cantonese_digits.py` `smoke_sidecars.py` `pad_test_audio.py` `test_deepseek.py` `test_volcano_v3.py`
- 平台：`setup-windows.ps1`

## 关键入口

| 场景 | 入口 |
|---|---|
| 本机开发拉起全栈 | `python tools/bok.py serve`（无 Docker） |
| 桌面打包 | `cd desktop && npx tauri build --bundles app`（CI release.yml） |
| 模型首启下载 | `python tools/bok.py setup download` |
| 打包自检 | `bok.py doctor --packaged` + `scripts/verify_bundle.sh` |
| 生产常驻（launchd） | `bok.py prod install` / `prod status` |
| A 线通话 | 前端 /calls → LiveKit :7880 → agent worker（每通语言固定） |
| B 线同传 v2 | 前端 /interpret → LiveKit :7880 → interp worker ×2（:1236 MT + MiniMax） |
| B 线同传 v1(冻结) | 前端 /translate → ws://127.0.0.1:8790 |

## 运行时装配（packaged）

```text
desktop/runtime/python/        独立 CPython（依赖 requirements-runtime-<平台>.txt）
desktop/runtime/llama/         Windows llama-server.exe + cudart DLL
desktop/runtime/bline-node_modules/
desktop/src-tauri/binaries/    externalBin：livekit-server / node（<name>-<target-triple>）
```

## 已清理的遗留

- Ollama：已从编排/默认配置/B 线翻译移除（docs/archive 留存说明）
- Docker：开发可选栈归档 `dev/docker/`，CI 不构建
- CosyVoice：无运行时引用
- `.superpowers/`：subagent 工作台（gitignored，实测档案留档）不入 gh 推送
