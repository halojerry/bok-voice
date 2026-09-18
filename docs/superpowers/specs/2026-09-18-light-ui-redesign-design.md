# Bok Voice Web 浅色 UI 改版设计（Light UI Redesign）

- 日期：2026-09-18
- 状态：已确认（设计五问 + 执行节奏均经用户拍板）
- 范围：`apps/web`（Next 16 静态导出管理台 + 节点 UI）视觉层整体改版；**不改任何运行时行为、CP API 契约、音频链路逻辑**
- 决策记录：深度重设计 / 纯浅色单主题 / 配色 C（中性骨架+青点缀）/ 布局壳 A（分组侧边栏可折叠）/ 舞台 B（全浅一体 + 官方 @agents-ui 浅色组件）/ 执行分四期
- 视觉探索存档：`.superpowers/brainstorm/97998-1789672038/`（本机，已 gitignore）
- 关联：PR #100（战役仪表盘+向导）建议先合，P1 顺带收编（见 §10）

## 0. 现状摘要（改版依据）

- 栈：Next 16.3 静态导出、React 19、Tailwind v4（CSS-first 无 config）、shadcn 管道已装（radix-nova、CVA、cn、tailwind-merge、radix-ui）；`components/ui/` 已有 vendored button/select/toggle/separator/bubble/message/marker/message-scroller（radix-nova 旧版）
- 主题：**常暗硬编码**——`app/globals.css` 两层 `:root` 打架（utilities 层 `#070707` 底 + `#1fd5f9` 青 accent + stage 渐变；base 层 oklch 被故意填成暗值喂 shadcn 组件），无主题切换
- 页面：20 页（工作台/通话/同传/名册/外呼/对象/报表/话术/快答/人设/知识/主管/员工/节点/审计/设置/登录/setup…）；导航 15 项挤单条顶栏 `StageHeader`，按权限显隐
- 大件：`CallStudio.tsx` 1235 行（三栏工作台）、`interpret-console.tsx` 1982 行（同传一体台+大字幕浮窗）、vendored agents-ui（aura 着色器 `DEFAULT_COLOR '#1FD5F9'`、`use-mood-color` 暗色调板）
- 硬雷清单（浅色化必须 sweep）：32×`bg-white/5`、12×`bg-white/10`、3×`bg-black/20`；暗底浅字（`text-emerald-300/red-300/amber-300/sky-300/red-200/80` 等）；硬编码 hex（`hover:bg-[#4adcfa]`、`text-[#01191c]`、`hover:border-[#2c2d2d] hover:bg-[#141515]`、stage 渐变 rgba）；`app/setup/page.tsx` 的 `text-(--bg)` 引用未定义变量（顺带修）
- 已知故意暗件：`interpret-console` 大字幕浮窗（黑底白字，注释言明「字幕机本色，与主题无关」）

## 1. 设计基调

1. **纯浅色单主题**：不保留暗色切换；`.dark` 块删除；所有颜色假设只验证一套。
2. **中性骨架 + 青色活信号**：骨架完全 shadcn 中性（白底卡片、zinc 灰阶、近黑主按钮）；青色只用于「活的信号」——进度/图表/在通徽标/Aura/logo 点睛/链接强调。
3. **密度不变**：运营台信息密度优先，紧凑控件（h-7 档）保留，shadcn 组件统一 `size="sm"` 档对齐。
4. **圆角/字体不动**：`--radius: 0.625rem`、卡片 rounded-lg、按钮 rounded-sm；字体栈照旧，数字等宽照旧。
5. **大字幕浮窗黑底白字原样保留**（字幕机本色，不进主题 token）。

## 2. Token 架构（globals.css 重构）

两层打架的 `:root` 收敛为一套，结构如下（oklch 具体值取 Tailwind v4 官方色板，注释给 hex 锚点）：

```
:root {
  /* —— shadcn 标准位：官方 neutral 浅色基准 —— */
  --background: 白; --foreground: 近黑(zinc-950 级);
  --card/--popover: 白; --primary: 近黑; --primary-foreground: 白;
  --secondary/--muted/--accent: 浅灰(zinc-100 级，accent 回归 hover 底色本义);
  --muted-foreground: 中灰; --destructive: red-600 级; --border/--input: zinc-200 级;
  --ring: 近黑; --radius: 0.625rem; 派生 radius 阶、sidebar/chart token 照 shadcn 官方浅色全量补齐;

  /* —— 品牌活信号：--live 家族（青，浅底重校）—— */
  --live:        cyan-600 ≈ #0891b2;   /* 图表/进度/链接/徽标主用，白底对比度 ≥4.5:1 */
  --live-strong: cyan-700 ≈ #0e7490;   /* hover/强调 */
  --live-soft:   cyan-100 ≈ #cffafe;   /* 徽标底/软高亮 */
  --live-soft-bg: cyan-50 ≈ #ecfeff;   /* 大面积微染底 */
  --live-ink:    cyan-900 ≈ #155e75;   /* 浅底上的青色文字 */

  /* —— stage 家族：重定义浅色变体 —— */
  --stage-bg: #fafafa; --stage-bg-2: cyan-50（极淡晕）; --stage-panel/--stage-panel-2: 白/浅灰;
  --stage-border: zinc-200; --stage-border-soft: zinc-100; --stage-muted: 中灰; --stage-value: --live;
}
```

- 旧 `--accent`（品牌青 #1fd5f9）语义全部迁 `--live`；shadcn `--accent` 位回归浅灰本义——同名两义的老打架就地解决。
- `stage-shell` 背景径向暗场渐变改为「`#fafafa` + 极淡青晕（cyan-50 级，克制）」。
- Aura `DEFAULT_COLOR`/`use-mood-color` 色板浅底重校：整体移向 600-800 档、chroma 拉暗端；mood 色板逐色对照白底对比度过线。

## 3. `@utility` 处置（省力杠杆）

- 全站 ~30 个自定义类（`card`/`soft`/`eyebrow`/`label`/`page-title`/`page-sub`/`muted`/`btn`/`btn-primary`/`btn-ghost`/`input`/`select`/`textarea`/`dot`/`stage-*` 家族）**保留类名、重写实现为浅色版**——45 个文件 JSX 大面积零改动，浅色自动生效。
- 只逐处 sweep 硬雷（§0 清单）：透明覆盖（`bg-white/5`、`bg-white/10`、`bg-black/20`）一律替换为语义 token（`bg-muted` 系或既有 `soft` utility），原则「同一视觉角色同一 token」，替换后全站不允许残留裸白/黑透明 arbitrary value（grep `bg-white/`、`bg-black/` 归零，大字幕浮窗的 `bg-black/85` 等黑底字幕件在豁免清单内逐个点名）；暗底浅字→浅底深字（*-300→*-700/*-600）；hex→token 引用。
- 新代码/重构页面用 `ui/` 原语；旧 utility 与新原语同 token 源并存，不冲突不双轨（utility 实现内部引用同一套变量）。

## 4. 布局壳（方案 A 落地）

- `AppShell` 重排：**左侧 Sidebar 232px + 顶部条 h-12 + 内容区 max-w 不变**。
- Sidebar 分四组：语音工作（工作台/通话/同传/主管旁听）、运营（名册/外呼/对象/报表）、资产（话术/快答/人设/知识库）、系统（设置/员工/审计/节点）。
- **导航数据与权限逻辑原样迁移**：`StageHeader` 的 NAV 数组（permission/admin/rootOnly 标志）+ `gateForPath()` 抽到 `lib/navigation.ts` 单点；Sidebar/RouteGuard 同吃一份——RBAC 显隐行为零变化。
- 折叠：整条收成 56px 图标 rail（lucide 图标 + hover tooltip）；进 CallStudio/同传页自动折叠；偏好存 `localStorage["bok.ui.sidebar"]`（本次改版唯一新增存储键）。
- 顶栏：页名/面包屑 + `StatusBadge`（CP 状态）+ 账号菜单（用户名/角色/退出）。
- 收编 `(stage)` 路由组：`dashboard-page` 现在自己包一层 AppShell，统一进新壳，删除重复包装；`/login`、`/setup` 独立浅色页（不进壳）。

## 5. 组件策略

- **shadcn registry 补件（最小必要集）**：`input / label / textarea / checkbox / switch / dialog / dropdown-menu / table / tabs / badge / card / tooltip / separator / skeleton`。button/select 已 vendored 不重拉；toast/sonner 等按需后补，不预拉。
- **图标全面转 lucide-react**（已装 1.43 未用）：页面文本字形（`→ + ⛔ ⚠ ·` 等）全部替换。
- **@agents-ui 从 registry 重拉最新**（`components.json` 已配 `https://livekit.com/ui/r/{name}.json`）：`agent-session-provider / agent-control-bar / agent-audio-visualizer-aura / agent-chat-transcript / agent-chat-indicator / start-audio-button / agent-disconnect-button` + `agent-session-view-01` block；现 vendored 旧快照被替换。
- **AI SDK Elements（https://elements.ai-sdk.dev/）= 模式参考，不进依赖**：chat 组件绑 Vercel AI SDK 运行时，本项目转写源是 LiveKit；仅借鉴 Conversation/Message/Transcription 布局形态，纯展示件个案 vendor。

## 6. 舞台页（全浅 + 官方块接入）

**铁律：运行时行为一根手指不碰。** connect() 陷阱、preConnectBuffer、setSinkId 设备层、回声守卫、垫话/watchdog 等 AGENTS.md 钉死的链路全部原样；改版只动视觉层与展示组件。

- **CallStudio**：三栏结构保留。
  - 中列舞台：`AgentSessionProvider`（session/token 源接现有 `/api/token` 与连接流程）→ `AgentAudioVisualizerAura`（`themeMode="light"`、color 青系浅底重校、size 跟舞台布局）+ `AgentControlBar`（controls 按 CallStudio 现有多媒体能力裁剪；`onDisconnect` 接现有挂断路径）。
  - 对话流列：最新 `AgentChatTranscript`（现有 transcript/消息状态映射进 `messages`；thinking 态出 `AgentChatIndicator`）。
  - 左右两列浅色卡片化；遥测数字等宽 + `--live` 高亮；对象档案列信息结构不动。
- **interpret-console**：双栏字幕/逐句延迟面板/听对方原声开关/大字幕浮窗功能结构保留；字幕流按 Elements Conversation 形态重排（气泡化、我方右对方左、浅色）；大字幕浮窗黑底白字原样。
- **Dashboard（收编 PR #100）**：六 KPI 卡 + 时长分布条 + 坐席排行 + 标记双表走 token 自动浅色；bar 轨道 `bg-white/5`→`bg-muted`；P4 给图表上 `--live`。
- **ListenPanel**：官方 `LiveKitRoom`/`useTranscriptions` 不动，chrome 浅色化。
- **战役向导/调度编辑器（PR #100）**：结构不动，token 收编浅色。

## 7. 边界与降级

- `LoadingState/EmptyState/ErrorState` 重绘：shadcn Skeleton 骨架 + lucide 空态图标；文案不变。
- static export 约束照旧：动态路由零新增，动态 import 沿用 shader-toy 先例；Aura（UnicornStudio）浅底对比度专项验收。
- 新 localStorage 仅 `bok.ui.sidebar`；`bok_token`/`bok.audio.*` 等既有键不动。
- `app/setup/page.tsx` 的 `text-(--bg)` 未定义变量顺带修正。
- 术语门禁（`tests/test_cantonese_terminology.py`）不涉及；不改任何带文案的运行时行为。

## 8. 验收门

- **门禁**：`cd apps/web && npx tsc --noEmit && npm run build` 全绿（每期）；Python 侧零改动。
- **行为回归（每期跑相关项）**：
  - 三角色（user/admin/root）× 匿名本地模式：导航显隐、页面可达性与改版前一致；
  - CallStudio 全流程手测：建单 → 连接 → 说话 → 打断 → 挂断，首连时长无回退（15.3s 陷阱不许复现）；
  - 同传启停×3、大字幕拖动/三档字号/全屏、设备切换（setSinkId）、听对方原声开关；
  - 登录 → 登出、权限页 403 守卫。
- **视觉走查（P1/P4 全量，P2/P3 按页）**：20 页浅色逐页过；正文对比度 ≥4.5:1、大字 ≥3:1；青色信号色在白底只用 ≥600 档。
- **性能哨兵**：改版不引入新运行时依赖（除 lucide/已装包的使用加深）；bundle 体积对比改版前无异常跳涨（>15% 需说明）。

## 9. 四期切分（每期独立 PR，独立可合并）

| 期 | 内容 | 完成判据 |
|---|---|---|
| **P1 地基** | globals.css 重构（§2 token 全表）+ `@utility` 浅色重写（§3）+ 硬雷 sweep（§0 清单）+ shadcn 补件（§5）+ lucide 文本字形替换 + setup 页 `--bg` 修正 | 全站 20 页立变浅色无漏底；tsc/build 绿 |
| **P2 壳** | `lib/navigation.ts` 抽取 + Sidebar/Topbar 重排 + 折叠 rail + `(stage)` 收编 + 登录/setup 页浅色重排 | 三角色显隐与改版前一致；CallStudio/同传自动折叠生效 |
| **P3 舞台** | @agents-ui registry 重拉最新 + CallStudio 舞台列/对话列换官方件 + interpret 字幕流重排 + ListenPanel 浅色 | 舞台全流程手测行为不变；Aura 浅底验收过 |
| **P4 打磨** | dashboard 图表上 `--live` + skeleton/空态全量铺 + mood 色板浅色校准收尾 + 动效微调（限 CSS/motion 已装）+ 全站走查 | §8 视觉走查全过 |

## 10. 与 PR #100 的顺序

建议 **PR #100 先合**：其仪表盘/向导全部走现有 `card/label/muted/bg-white/5` 范式，P1 的 token 重定 + sweep 直接收编，零额外协调。若改版先行合并，#100 合入后需在 P1 内补一次小 sweep（新增的 `bg-white/5`、`text-(--stage-muted)` 等，均在该两文件内）。

## 11. 参考资料

- shadcn/ui：https://ui.shadcn.com/（token 基准、registry 拉件）
- LiveKit Agents UI 文档：overview、block/agent-session-view-01、agent-control-bar、agent-audio-visualizer-aura（`themeMode`/`color` 浅色支持）、agent-chat-transcript、agent-session-provider、start-audio-button、agent-disconnect-button（docs.livekit.io/reference/components/agents-ui/…）
- LiveKit components-js shadcn registry：https://github.com/livekit/components-js/tree/main/packages/shadcn
- AI SDK Elements：https://elements.ai-sdk.dev/（Conversation/Message/Transcription 布局形态参考）
- 本仓既有档案：`docs/DEV_TOOLS.md`、`AGENT.md`（官方组件优先三档判据）
