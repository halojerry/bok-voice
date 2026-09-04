# Bok Voice 仓库地图（REPO MAP）

> 目录 → 用途 → 归属（dev / packaged / both）→ 关键入口。

## 根目录

| 路径 | 用途 | 归属 |
|---|---|---|
| `tools/bok.py` | 编排/模型/健康/自检入口 | both |
| `scripts/` | CI 构建、E2E、延迟测量、smoke | dev+CI |
| `apps/agent/` | A 线智能体（livekit-agents worker） | both |
| `apps/control-plane/` | FastAPI 业务服务 :8000 | both |
| `apps/web/` | Next.js 前端（静态导出进 Tauri） | both |
| `packages/core/` | 领域模型 + 接口 | both |
| `packages/business-db/` | SQLAlchemy 仓库（SQLite/Postgres） | both |
| `packages/knowledge/` | 知识服务 / Markdown / 向量 | both |
| `packages/observability/` | 结构化日志 + 审计 | both |
| `services/qwen3-asr-sidecar/` | ASR HTTP sidecar :8787 | both |
| `services/qwen3-tts-sidecar/` | TTS HTTP sidecar :8788 | both |
| `services/realtime-translation/` | B 线同传 worker :8790（Node,v1 已冻结留 POC） | both |
| `services/livekit-server/` | livekit.yaml 配置 | both |
| `desktop/` | Tauri 桌壳 + runtime 装配 | packaged |
| `desktop/src-tauri/` | Rust shell + tauri.conf.json + externalBin | packaged |
| `tests/` | pytest（含 fixtures/audio 测试音频） | dev/CI |
| `docs/` | RUNTIME_TOPOLOGY / REPO_MAP / 归档 | dev |
| `dev/docker/` | 可选 Docker 开发栈（归档，不进 CI） | dev-only |

## 关键入口

| 场景 | 入口 |
|---|---|
| 本机开发拉起全栈 | `python tools/bok.py serve`（无 Docker） |
| 桌面打包 | `desktop/`（tauri build，CI release.yml） |
| 模型首启下载 | `bok.py setup download` / 前端 /setup |
| 打包自检 | `bok.py doctor --packaged` + `scripts/verify_bundle.sh` |
| B 线同传 v2 | 前端 /interpret → LiveKit :7880 → interp worker ×2（agent_name 显式分发） |
| B 线同传 v1(冻结) | 前端 /translate → ws://127.0.0.1:8790 |
| A 线通话 | 前端 /calls → LiveKit :7880 → agent worker |

## 运行时装配（packaged）

```text
desktop/runtime/python/        standalone CPython（依赖按 requirements-runtime-<平台>.txt）
desktop/runtime/llama/         Windows llama-server.exe + cudart DLL
desktop/runtime/bline-node_modules/
desktop/src-tauri/binaries/    externalBin：livekit-server / node（<name>-<target-triple>）
```

## 已清理的遗留

- Ollama：已从编排/默认配置/B 线翻译移除（docs/archive 留存历史说明）
- Docker：开发可选栈归档到 `dev/docker/`，CI 不再构建
- CosyVoice：无运行时引用
