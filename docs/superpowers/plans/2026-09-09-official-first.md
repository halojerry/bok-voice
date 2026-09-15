# 官方优先改造（LiveKit 官方栈收编）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按「能用官方就不用手搓」原则，把三路 subagent 调研证实可收编的官方件全部落地（CP/dispatch 修复 → TW4 → 官方 UI 件），并把分叉边界门禁化。

**Architecture:** 四个独立 PR 的阶段（P0 门禁化 → P1 infra/CP → P2 TW4 → P3 官方 UI 件），P1 与 P2/P3 无依赖可并行；agent 运行时核心路径经调研证实已官方化（28 项机制用 20 项），**本轮零 agent 侧改动**。

**Tech Stack:** livekit-api(Python, 已装 1.2.1)、livekit-cli(brew 新装)、Tailwind 4 + @tailwindcss/postcss、shadcn registry @agents-ui + components-react 2.9.24（已在）、streamdown（新增）。

## Global Constraints

- `livekit-agents>=1.8.0,<1.9` 锁不动；`@livekit/components-react 2.9.24` 不动；`motion ^13.1.1` 不动（官方 peer ^12，仅做视觉验证不做降级）
- 北极星 PERCEIVED_MS 不回归：本轮**不碰音频链路**（ASR/LLM/TTS/VAD/endpointing 零改动）
- 术语门禁：新文件不得出现旧拼写 `yue` 字面量（测试会红）
- 每阶段独立分支+PR（conventional commits），合前跑该阶段验证命令；P2/P3 各自可回滚
- 调研结论持久化到 `.superpowers/sdd/2026-09-09-official-first/FINDINGS.md`（三份报告归档）
- 守住清单（**禁止**本轮"顺手官方化"）：尾部冻结重放（livekit_plugins.py:963-1083，KV 严格前缀契约）、FlowController 规则先行（flow.py，官方 Tasks 每任务多一次 4B LLM 循环撞 prefill 墙）、回声守卫（agent.py:508-528，APM 处理不了浏览器远端轨）、B 线 whoIs 双栏与半双工（interpret-console.tsx:645-741，业务特有）、lib/audio.ts Tauri CoreAudio 层（官方 useMediaDeviceSelect 不覆盖原生输出）、挂断结算链（CallStudio.tsx:721-749）

---

### Phase 0: 原则门禁化（PR #A）

### Task 0.1: 调研归档 + AGENTS.md 条款
- Create: `.superpowers/sdd/2026-09-09-official-first/FINDINGS.md`（三份 subagent 报告全文归档：前端/UI、agent 运行时、infra/工具链；附录=Phase 4 记档项）
- Modify: `AGENTS.md` 架构边界段「官方契约优先」条目，升级为「官方组件优先」并附三档判据：
  1. 新功能先查官方组件/官方模式（shadcn registry 可增量拉；`lk docs`/Docs MCP 查事实）
  2. 自定义 STT/LLM/TTS 插件、业务工作台 UI、bok.py 编排 = 官方授权的自定义层，非手搓
  3. 实证分叉清单（尾部冻结重放/规则先行/回声守卫/Tauri 设备层）——动这些必须先复核 FINDINGS.md 对应证据
- Steps: 写 FINDINGS.md → 改 AGENTS.md → `git commit -m "docs: 官方组件优先原则门禁化+三路官方栈调研归档"`

### Phase 1: CP/dispatch 修复 + webhook + lk CLI（PR #B，独立于 UI）

### Task 1.1: dispatch 工具函数（TDD）
- Create: `apps/control-plane/control_plane/dispatch_utils.py`
  ```python
  async def has_active_dispatch(lkapi, room: str, agent_name: str = "bok-voice") -> bool:
      # list_dispatch(room) → 任一 dispatch.agent_name==agent_name 且 state.jobs 含 PENDING/RUNNING → True
  async def cleanup_dispatch(lkapi, room: str) -> int:
      # list_dispatch → 对每个 dispatch delete_dispatch(dispatch_id)；返回删除数；全程 best-effort try/except + 日志
  ```
- Test: `tests/test_dispatch_utils.py`（AsyncMock lkapi 三例：有 RUNNING job 跳过 / 无 job 清掉 / API 异常吞掉返回 0）
- API 名以 `grep "def list_dispatch\|def delete_dispatch" .venv312/lib/python3.12/site-packages/livekit/api/agent_dispatch_service.py` 校正
- Steps: 写失败测试 → 跑红 → 实现 → 跑绿 → commit `fix(cp): dispatch 工具函数——防重判定与主动回收`

### Task 1.2: 接入三处调用点
- Modify: `apps/control-plane/control_plane/main.py`
  - `_redispatch`（~:1488，create_dispatch 在 :1501）：创建前先 `has_active_dispatch()`，True 则跳过并打点 `REDISPATCH_SKIP`
  - hangup 端点（~:781）：ENDED 结算成功后 best-effort `cleanup_dispatch(room)`
  - reaper ENDED 分支（:739-755）：同上（房已确认空）
- Test: 扩 `tests/test_dispatch_utils.py` 补调用点单测（monkeypatch 工具函数断言被调）
- Steps: 写测试跑红 → 接线 → 跑绿 → `python -m compileall -q apps` → 全量 `./scripts/test.sh` → commit `fix(cp): 崩溃重派防叠加+通话结束主动回收 dispatch（僵尸通话 P1 收口）`

### Task 1.3: 启用 livekit.yaml webhook
- Modify: `services/livekit-server/livekit.yaml:18-23` 去注释（urls 指 `http://127.0.0.1:8000/api/webhook/livekit`，api_key devkey；先 grep 确认 CP 端点存在）
- Steps: 改配置 → `python tools/bok.py down && serve` → 演练：通话中 kill agent worker → 日志见 room webhook → CP 自动重派且不叠加（配合 Task 1.2）→ commit `feat(infra): 启用 livekit webhook 崩溃补位`

### Task 1.4: lk CLI 收编
- `brew install livekit-cli`；Modify: `docs/DEV_TOOLS.md` §4 增 lk 段：`lk token create --api-key devkey --api-secret devsecret --room smoke --identity smoke`（对本地 server 验证）、`lk docs search`（与 Docs MCP 等价）、`lk perf agent-load-test --agent-name bok-voice` 列为升级门禁配套（与 scripts/load_audio_concurrency.py 并跑）
- 不收编：`lk room join`（推不了 16k PCM 定制音频）、`lk agent init`（已有自有骨架）
- Steps: 安装 → 本地验证两命令 → 写文档 → commit `docs(dev-tools): 收编 lk CLI——token/room 调试与官方 load-test 门禁`

### Phase 2: Tailwind 4 迁移（PR #C，阻塞 P3）

### Task 2.1: 官方 codemod 迁移
- Modify: `apps/web/package.json`（tailwindcss ^4 + @tailwindcss/postcss，删 autoprefixer）、`apps/web/postcss.config.mjs`（`'@tailwindcss/postcss': {}` 单插件）、`apps/web/app/globals.css`（头部 `@tailwind` 三行 → `@import "tailwindcss";` + `@source` 指向组件目录；`:79/:148` 裸 `rounded` → `rounded-sm` 等改名）
- 已知有利条件：`tailwind.config.ts` theme.extend 为空；边框全部显式 `border-[var(--card-border)]`；Next 16 原生支持；WKWebView 支持 oklch/color-mix
- Steps: `git checkout -b feat/tw4` → `npx @tailwindcss/upgrade@latest` → 手工复核 codemod diff（重点 globals.css 与 .btn/.stage-*）→ `npx tsc --noEmit && npm run build` 绿 → 还原 `.lk-grid-cell-base` 手动降配（globals.css:101-104 删除，恢复官方 `bg-current/10` 写法）→ commit `build(web): Tailwind 3.4 → 4 迁移`

### Task 2.2: 视觉回归验收
按 QA sweep 12 页清单逐页人工过（重点 CallStudio/interpret/一体台/设置），aura shader 动画正常（motion ^13 与官方 peer ^12 的兼容性在此验证）→ dev 栈 `bok.py serve` + Tauri 壳各看一遍 → 发现视觉破损就地修或回滚整个分支 → commit + PR

### Phase 3: 官方 UI 件收编（PR #D，依赖 P2）

### Task 3.1: shadcn init + 官方件落地
- `cd apps/web && npx shadcn@latest init`（components.json）→ `npx shadcn@latest registry add @agents-ui` → 装 `utils/button/toggle/select/separator` + `@agents-ui/agent-chat-indicator` + `@agents-ui/agent-chat-transcript`；新增依赖 `streamdown`
- **已知坑**：registryDependencies 里 `message-scroller/message/bubble/marker` 四个 ui 原语在 shadcn 主 registry 404（AGENT.md:153）——从 livekit/components-js 仓 `packages/shadcn/components/ui/` 取源码落进 `apps/web/components/ui/`
- Steps: 装件 → 补四原语 → `npx tsc --noEmit` 绿 → commit `feat(web): shadcn init + @agents-ui 转写/指示器官方件落地`

### Task 3.2: CallStudio 转写面板换代
- Modify: `apps/web/components/CallStudio.tsx:80-113`——手绘 useTranscriptions 气泡 → `<AgentChatTranscript agentState={state} messages={useSessionMessages(session).messages} />`；删除自绘列表样式；`:128` failureReasons 空值守卫保留；B 线 interpret-console **不动**
- Steps: 换件 → tsc/build 绿 → dev 栈实机通话验证转写实时滚底+语音文字合并 → 三语 E2E + barge-in PASS → commit `feat(web): CallStudio 转写面板换官方 AgentChatTranscript`

### Task 3.3: 指示器 + 设备选择试点
- `CallStudio.tsx:107-112` 手写脉冲点 → `AgentChatIndicator`；`apps/web/app/(app)/interpret/page.tsx:465-486` 设备卡 → `useMediaDeviceSelect`+`usePersistentUserChoices` 试点（lib/audio.ts Tauri 输出层保留并行）
- Steps: 两处换件 → build 绿 → interpret 页设备选择/持久化人工验证 → commit `feat(web): 官方 chat-indicator+设备选择 hooks 试点`

### Task 3.4: 可选项（验收不通过则砍）
`VoiceAssistantControlBar`（legacy，CallStudio.tsx:150）mic 部分换 `AgentTrackToggle`；挂断按钮保持自写（业务链耦合）

### Phase 4: 记档不实施（写入 FINDINGS.md 附录）

- `perform_rpc` 替代 `_supervisor_watch` 2s 轮询（agent.py:2202-2232，暂停接管 2s→<100ms；与 PERCEIVED_MS 无关，单独立项）
- livekit-simulations 的 risks.yaml 场景覆盖方法论（半天，纯本地部分）
- 观察项：官方若出「带反向流的远端轨 FrameProcessor」→ 回声问题重评；上云 SaaS 补齐顺序=生产键→webhook→TLS→TURN→Redis
- N/A 结论存档：Rust SDK（Tauri 壳无原生媒体路径）、Agent Console（Cloud 入口与本地优先冲突）、Inference 参数（Cloud 专属）

## 验收门（每 PR 合并前）

- P0/P1：`./scripts/test.sh` 全绿 + `python -m compileall -q apps packages services tools scripts` + worker 演练（kill-recover 重派一次成功且无叠加）
- P2：`npx tsc --noEmit && npm run build` 绿 + 12 页视觉回归清单过 + aura 动画正常
- P3：build 绿 + 三语 E2E + `scripts/e2e_barge_in.py` PASS + 实机转写滚底验证
- 最终：合齐后 `python tools/bok.py serve` 全栈冒烟 + `bok.py doctor` 正常
