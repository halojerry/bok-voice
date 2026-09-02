# Bok Voice 桌面壳（Tauri）

把本地优先的 Bok 客服语音助手 + 同声传译做成一套可分发的桌面应用：
用户安装后打开应用即拉起本机服务，无需手配模型/端口/环境变量。

## 它做什么

- 启动一次 `python tools/bok.py serve`，幂等拉起 `control-plane(:8000)`、
  `web(:3000)`、`asr(:8787)`、`tts(:8788)`、`llm(:1235)`、`b-line(:8790)`，
  及可选的 `livekit(:7880)`。
- 主窗口指向 `http://127.0.0.1:3000`（dev 网页工作台；打包版直接内嵌静态产物 `apps/web/out`，经 `tauri://localhost` 加载），服务未就绪时显示启动页并自动跳转。
- 通过 `@tauri-apps/api` 桥接把服务健康、日志目录、模型清单暴露给前端。
- 模型首启下载走 `tools/bok.py download`，全部落在平台级 `app-data` 目录；
  macOS `~/Library/Application Support/BokVoice`，Windows `%LOCALAPPDATA%\BokVoice`。

## 本机开发

```bash
# 1. 生成图标（仅一次）
python3 desktop/scripts/gen_icon.py

# 2. 安装 Tauri CLI
cd desktop && npm ci

# 3. 派生 icns/ico（CI 也会做）
cd desktop && npm run icons

# 4. 开发：先起后端全栈（无 Docker）
python tools/bok.py serve
#    再起 web dev：cd apps/web && npm run dev
cd desktop && npm run dev
```

打包（CI 出包；本机仅做编译自检）：

```bash
bash scripts/stub_external_bin.sh   # cargo 编译占位（真实二进制由 build_runtime.sh 提供）
cd desktop/src-tauri && cargo test && cargo check
# 真实 release：push tag v* -> GitHub Actions 产出 mac zip + Windows exe
```

## 目录结构

```text
desktop/
  package.json          # tauri CLI 入口
  src/bridge.ts         # 前端调 Tauri 命令的桥
  src-tauri/
    Cargo.toml
    tauri.conf.json
    src/main.rs
    src/lib.rs          # 服务编排 / 健康检查 / 打开日志 / manifest
    icons/
    capabilities/default.json
  dist/index.html       # 兜底启动页（窗口未指向 web 时）
```

## 可审计性

- 所有服务日志为结构化 JSONL：`app-data/logs/app.jsonl`（按组件滚动、含
  `request_id/call_id/account_id/object_id` 关联字段）。
- 业务审计写入 `app-data/audit/YYYY-MM-DD.jsonl`（只追加），并在有数据库时同步
  到 `audit_events` 表，可通过 `/api/audit` 查询。

> 注意：`desktop` 是“壳 + 编排”，真实模型权重由 `bok.py download` 拉取，
> 不作为仓库内容（≈13GB）。
