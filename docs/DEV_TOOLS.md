# Dev 工具链（对齐 LiveKit 官方）

本项目栈锁定：`livekit-server`（自部署）+ `livekit-agents 1.7.1` + `livekit-client 2.22`。
以下工具/渠道让「查官方、排障、跟版本」全程有官方一手资料。

## 1. LiveKit Docs MCP（coding agent 直接查官方文档）

- Endpoint：`https://docs.livekit.io/mcp`（免费、无需 key，Streamable HTTP）。
- 能力：`get_docs_overview`（目录）、`get_pages`（整页 markdown，**也能按 URL 抓公开
  GitHub 仓库源码**）、`docs_search`、`code_search`（LiveKit 公开 repo 代码检索）、
  `get_changelog`（盯破坏性变更）、`get_pricing_info`。
- 配置（JSON 风格 MCP client）：
  ```json
  {
    "mcpServers": {
      "livekit-docs": { "type": "http", "url": "https://docs.livekit.io/mcp" }
    }
  }
  ```
- 无 MCP 时的 CLI 等价物：`lk docs`（封装同款检索）。

## 2. 排障渠道分工（按问题类型）

| 问题类型 | 去哪问 |
|---|---|
| 行为/配置「怎么跑」类 | community.livekit.io 的 **Self Hosting** 分类（官方 staff 常驻） |
| 确凿 bug | GitHub issues（livekit/agents、livekit/livekit，按 repo 报） |
| 快速互动 | Slack（livekit.io 入口） |

> 自部署（fork 开源版）**无公开 SLA**。我们用「进程守护 + webhook 补位 + 压测演练」
> 补齐：崩溃恢复语义见下。

## 3. 版本锁定与升级

- **钉死**：server 具体 tag（当前 v1.13.6，Mac 走 `brew install livekit`）、
  `livekit-agents==1.7.x`（当前 1.7.1）。
- **监控**：订阅 GitHub releases atom——
  `https://github.com/livekit/livekit/releases.atom`、
  `https://github.com/livekit/agents/releases.atom`；或用 docs MCP `get_changelog`。
- **只吃 patch，minor 隔一个再升**。升级前必须过本地回归
  （`scripts/e2e_*` + pytest——正好当升级门禁）。破坏性变更实例：
  server v1.12 引入 TTL TURN 凭证、v1.13.1 移除旧式兼容；agents 1.7.0 把 12 个
  OTel 内容属性改名加 `lk.pii.` 前缀。
- agents 2.0 若出现：路径几乎必然是
  `docs.livekit.io/reference/migration-guides/`（同 client SDK v2 先例）+ forum
  Announcements 发帖。

## 4. 压测 / 负载演练

- `lk load-test`（server 压测）、`lk perf agent-load-test --agent-name bok-voice`
  （模拟 agent 房间 + 回声说话人）——进大版本升级/生产档演练的验收。
- 官方 server 指标：`prometheus_port: 6789`（livekit.yaml 已开）→ import 官方
  Grafana dashboard（livekit repo `deploy/grafana/livekit-server-overview.json`）。
- agent worker 健康：`GET :8081/worker` → `{agent_name, active_jobs, worker_load}`。

## 5. 自部署崩溃恢复语义（官方源码核实）

| 场景 | 行为 | 我们的补位 |
|---|---|---|
| worker 进程活着、仅与 server 断连 | job 存活并迁移（重连后 `MigrateJobRequest` 挂回） | 无需处理 |
| worker 进程死亡 / job 崩溃 | **OSS 无自动重派**（`restart_policy` = Cloud-only，protocol 标注） | ① launchd KeepAlive 拉起 worker 本体（`bok.py prod install`）② 订阅 livekit webhook `participant_left`（identity=agent）→ 房间仍有人则调 `AgentDispatchService.CreateDispatch` 重派（token 方案只在建房时生效，这是官方指定通路） |
| agent 参与者超时 | server 15s 检测断连（ping 15s + cleanup 5s）→ TerminateJob | 同上补位；语义演练用 `lk perf agent-load-test` |

## 6. 本地常驻进程（生产档）

```bash
python tools/bok.py prod install   # 生成 launchd plist(KeepAlive 崩溃自拉起)
launchctl bootstrap gui/$(id -u) "$HOME/Library/Application Support/BokVoice/units"/*.plist
python tools/bok.py prod status    # 官方健康面汇总(server GET / + worker :8081/worker)
```

开发期仍用 `python tools/bok.py serve`（前台 + run/*.pid）。
