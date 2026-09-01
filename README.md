# Bok Voice — 本地语音客服 + 同声传译工作台

本地优先、可审计的实时语音客服（A 线）与多通道同声传译（B 线）桌面应用。
分发版**双击即用**：无需安装 Docker / Node / Ollama / CUDA / 系统 Python。

## 文档

- [运行时关系拓扑](docs/RUNTIME_TOPOLOGY.md) — 安装后各组件/端口/数据流/生命周期，验收基准
- [仓库地图](docs/REPO_MAP.md) — 目录与文件用途

## 分发版（用户视角）

1. 安装 `BokVoice.app`（macOS Apple Silicon）或 Windows 安装包（NVIDIA GPU）
2. 首启向导下载模型（幂等、断点续传，约 13GB / 5.6GB）
3. 打开即用：A 线客服语音（普通话/粤语/英语 + 三语克隆），B 线同声传译

硬件要求：

| 平台 | 要求 |
|---|---|
| macOS | Apple Silicon（M 系列），建议内存 ≥32GB |
| Windows | NVIDIA GPU（CUDA 12.4、驱动 ≥550、显存 ≥8GB），无 CPU 兜底 |

数据全部落在本机 app-data（`~/Library/Application Support/BokVoice` 或
`%LOCALAPPDATA%\BokVoice`）：SQLite 业务库、模型、知识库 vault、日志、审计。

## 开发（一条命令，无 Docker）

```bash
./scripts/bootstrap.sh        # 建 .venv312 + 安装依赖（Python ≥3.11）
./scripts/test.sh             # pytest
cd services/realtime-translation && npm ci && npm test   # B 线单测
python tools/bok.py serve     # 拉起 control-plane/LiveKit/sidecars/LLM/B-line/agent
```

开发机模型默认从 `~/.lmstudio/models` 读取（Mac）；Windows 开发机模型走
`python tools/bok.py download`。旧的可选 Docker 栈归档在 `dev/docker/`（不进 CI）。

## 打包与发布

- `scripts/build_runtime.sh`：装配内嵌运行时（standalone Python、Node、LiveKit、llama CUDA）
- `scripts/verify_bundle.sh`：上传前硬门禁（结构 + 体积 ≤1.3GB + bundle 内 doctor）
- GitHub Actions：`ci.yml` 全量测试；tag `v*` 触发 `release.yml` 出 mac zip + Windows exe 并发布 Release

## 状态

零 Ollama、零 Docker 依赖；SQLite 持久化；打包产物经 CI 门禁验证后才发布。
