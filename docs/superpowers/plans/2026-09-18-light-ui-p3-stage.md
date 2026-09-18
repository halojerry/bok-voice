# Light UI P3 — 舞台：@agents-ui 重拉换装 + interpret 字幕流重排

Spec: `docs/superpowers/specs/2026-09-18-light-ui-redesign-design.md` §6/§9（P3 行）。
Base: `feat/light-ui-p3-stage` @ `ac8b126`（= P2 壳头）。

## Goal

舞台页对齐官方 agents 页（浅色）：registry 重拉最新 @agents-ui 组件替换 vendored
旧快照；CallStudio 中列舞台/对话流列换官方件（`themeMode="light"`）；interpret
字幕流按 Elements Conversation 形态重排（气泡、我方右对方左、浅色）；ListenPanel
chrome 浅色；侧栏进 CallStudio/同传自动折叠（P2 spec 判据，终审移交）。

## 铁律（对每个 task 生效）

- **运行时行为一根手指不碰**（spec §6）：connect() 流程（token 源 `/api/token`、
  preConnectBuffer 与 mic 发布次序）、setSinkId 设备层、回声守卫、挂断路径、
  同传启停/大字幕/延迟面板逻辑——全部原样。**只动视觉层与展示组件**。
- 换官方件=换「渲染这层皮」：状态/回调/数据源（useAgent/useSessionMessages/
  useTranscriptions/现有 api 调用）保持现有接线；新组件的 props 映射在旧值上做适配，
  不反向改业务状态。
- 色板纪律同 P2：浅色语义 token + 青只作活信号；Aura color 用青系浅底重校。
- `interpret-console.tsx` 大字幕浮窗黑底白字**原样保留**（字幕机本色豁免，
  P1 豁免区行号 1605-1672 一带，以 `bg-black/85` 定位）。
- 新 localStorage 键零新增（自动折叠**不写**用户偏好键——进舞台路由临时收起、
  离开恢复存储偏好）。
- 门禁每 task：`cd apps/web && npx tsc --noEmit && npm run build`；
  术语/扫色 grep 门禁零命中（豁免区除外）。

## Task 1 — @agents-ui registry 重拉 + 浅色适配

- 从 `https://livekit.com/ui/r/{name}.json`（components.json `@agents-ui`）重拉：
  `agent-session-provider / agent-control-bar / agent-audio-visualizer-aura /
  agent-chat-transcript / agent-chat-indicator / start-audio-button /
  agent-disconnect-button` + block `agent-session-view-01`。
- 新件落 `components/agents-ui/`（registry 元数据指定的路径/依赖照装——
  npm install 允许；**运行时新依赖需在报告说明**）。react-shader-toy 已 vendored
  不重拉（除非新 Aura 硬依赖新版本，报告说明）。
- 与现有快照 diff：**保留 P1 已扫的浅色 token 类**（旧快照若有 --live/muted-foreground
  映射，重拉后重打同款映射）；vendored 偏离点（如 dialog `bg-foreground/10` 先例）
  有则保留并注释。
- 本 task 只落组件不换消费方（CallStudio 仍用旧导出名编译通过——新组件若同名
  覆盖旧文件，须保证导出 API 兼容或在 CallStudio 侧零改动的 shim）。

**Gates:** tsc/build 绿；`git diff --stat` 只含 components/agents-ui/** +
package.json/lock；CallStudio/interpret-console 零改动。

## Task 2 — CallStudio 舞台换装（官方件）

文件：`apps/web/components/CallStudio.tsx`（surgical，1280 行，禁顺手重构）。

- 中列舞台：`VoiceAgentInterface`（包 Aura）换/配 `themeMode="light"` + color 青系
  浅底重校；`VoiceAssistantControlBar`（@livekit/components-react 旧实验件）→
  官方 `AgentControlBar`，controls 按 CallStudio 现有能力裁剪（麦克风/输出设备/
  挂断），`onDisconnect` 接**现有**挂断路径（函数原样复用）；`StartAudio` →
  官方 `StartAudioButton`（交互语义等价：首次出声解锁）。
- 对话流列：换最新 `AgentChatTranscript`（messages 源仍是现有
  useSessionMessages/useTranscriptions 映射）；thinking 态 `AgentChatIndicator`。
- 左右两列卡片浅色已由 P1 完成，本 task 只查漏（`--stage-*`/暗代色残留 → 语义 token）。
- 状态/连接/设备/回声守卫代码块**零 diff**（评审会核对）。

**Gates:** tsc/build；`grep -n "VoiceAssistantControlBar\|StartAudio[^B]" CallStudio.tsx`
零命中（旧件退役）；行为面自查清单进报告（connect 次序/设备/挂断路径未动）。

## Task 3 — interpret 字幕流重排 + ListenPanel 浅色

文件：`apps/web/components/interpret-console.tsx`（2085 行，surgical）、
`apps/web/components/listen-panel.tsx`。

- 字幕流列表区按 Elements Conversation 形态重排：气泡化（我方右、对方左）、
  浅色 token（我方气泡 `bg-(--live-soft)` 文 `--live-ink`，对方 `bg-muted`）、
  原文/译文行结构保留（`原文：/译文：`分行、stripVoiceTags 后文本不动）。
  **只动列表项 JSX 与样式**；数据流（transcriptions 订阅、延迟面板计算、
  启停、原声开关）零改动。
- 大字幕浮窗区（`bg-black/85` 块）零 diff。
- ListenPanel：chrome（面板壳/按钮/徽标）浅色语义 token 收尾；官方
  LiveKitRoom/useTranscriptions 用法不动。

**Gates:** tsc/build；`git diff` 不含大字幕区（行号以 bg-black/85 定位核对）；
同传页结构走查（静态渲染层）无回归。

## Task 4 — 舞台路由自动折叠 + 抽屉遮罩渐隐 + 全站验收

文件：`components/layout/sidebar.tsx`（+ 若需 `app-shell.tsx` 微调）。

- 进 `/calls/new`、`/interpret`、`/translate` 自动折叠桌面侧栏（不写 localStorage；
  离开路由恢复存储偏好；用户在舞台页手动切换仅临时生效）。舞台路由判定走
  `matchesPath`（GUARD_ONLY 含 translate）。
- 抽屉遮罩常挂 + opacity 过渡（终审 backlog①：关闭瞬拆闪断）；aria/焦点圈定
  留 P4（backlog②）。
- 全站验收：build 后 21 路由 HTML 齐全；浏览器走查 CallStudio（连接表单/设备区/
  舞台三列结构、侧栏自动折叠触发与恢复）、interpret（双栏字幕、延迟面板、
  大字幕黑块原样、自动折叠）、ListenPanel、首页。**舞台全流程真机手测
  （建单→连接→说话→打断→挂断）需本地栈，标注为 Ethan 验收项**；走查做
  结构与渲染层断言。

**Gates:** tsc/build；走查断言表进报告；ledger 收尾。

## 范围外（P4）

dashboard 图表 --live、skeleton/空态全量、mood 色板校准、抽屉 dialog-a11y、
campaigns dialing/in_call 撞色。
