# Bok Voice 桌面壳（Tauri）

把本地优先的 Bok 客服语音助手 + 同声传译做成一套可分发的桌面应用：
用户安装后打开应用即拉起本机服务，无需手配模型/端口/环境变量。

## 它做什么

- 启动一次 `python tools/bok.py serve`，幂等拉起 `control-plane(:8000)`、
  `web(:3000)`、`asr(:8787)`、`tts(:8788)`、`llm(:1235)`、`b-line(:8790)`，
  及可选的 `livekit(:7880)`。
- 主窗口指向 `http://127.0.0.1:3000`（dev 网页工作台；打包版直接内嵌静态产物 `apps/web/out`，经 `tauri://localhost` 加载），服务未就绪时显示启动页并自动跳转。
- 前端经 `apps/web/lib/tauri.ts` 直连 Tauri `invoke`（`__TAURI_INTERNALS__`，
  非 Tauri 环境优雅失败态），把服务健康、日志目录、模型下载状态、音频设备、
  自启开关暴露给设置页。
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
  src-tauri/
    Cargo.toml
    tauri.conf.json
    src/main.rs
    src/lib.rs          # 服务编排 / 健康 / 日志 / setup / 自启 / 单实例（invoke 命令面）
    src/audio.rs        # CoreAudio 系统输出设备枚举/切换
    icons/
    capabilities/default.json
  dist/index.html       # 兜底启动页（窗口未指向 web 时）
```

> 前端没有独立桥文件：`desktop/src/` 已删，web 侧 `apps/web/lib/tauri.ts`
> 直接转发 invoke。

## 自启与常驻（定位注意）

- **自启注册的是当前运行中的二进制路径**（tauri-plugin-autostart）——从安装后的
  `.app`/安装包里开启才指向安装产物；在开发构建里打开会注册 dev 可执行文件。
- **`prod install` 站点机上 GUI 自启开关只是便利项**：launchd/schtasks 已接管
  整栈常驻（见 `docs/RUNTIME_TOPOLOGY.md` §3），桌面自启只决定 GUI 壳是否随
  登录打开，不重复托管服务。
- **第二实例聚焦依赖默认 `main` 窗口 label**：单实例插件把后续启动重定向到
  label 为 `main` 的既有窗口（tauri.conf.json 默认窗口），改 label 会丢聚焦。

## 可审计性

- 所有服务日志为结构化 JSONL：`app-data/logs/app.jsonl`（按组件滚动、含
  `request_id/call_id/account_id/object_id` 关联字段）。
- 业务审计写入 `app-data/audit/YYYY-MM-DD.jsonl`（只追加），并在有数据库时同步
  到 `audit_events` 表，可通过 `/api/audit` 查询。

> 注意：`desktop` 是“壳 + 编排”，真实模型权重由 `bok.py download` 拉取，
> 不作为仓库内容（≈13GB）。
