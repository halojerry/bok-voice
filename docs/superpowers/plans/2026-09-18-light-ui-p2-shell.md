# Light UI P2 — 布局壳：分组侧边栏 + 可折叠 + 细顶栏

Spec: `docs/superpowers/specs/2026-09-18-light-ui-redesign-design.md`（§4 布局壳 A、§9 分期）。
Base: `feat/light-ui-p2-shell` @ `cc0119e`（= P1 地基头，浅色 token + shadcn 组件库已就位）。

## Goal

把 `(app)` 组 17 页的「顶部平铺 15 链接导航（StageHeader）+ 居中 max-w-7xl 主列」换成
brainstorm 拍板的 **布局壳 A：分组侧边栏（可折叠成图标轨）+ 细顶栏**。
权限判定语义（`gateForPath`/`navVisible`/`RouteGuard`）原样搬家，一行不改判定。
`/login`、`/setup` 本相不动（P3 舞台相处理）；`(stage)` 首页经 `DashboardPage`
内嵌 `<AppShell>`（dashboard-page.tsx:111，摸底勘误：初版计划误记首页无壳），
随壳一并换新——正合「全浅一体」拍板。

## Global Constraints（对每个 task 生效）

- **色板纪律（C 方向）**：青色 `--live` 族只作活信号——导航激活项、live 状态点；
  其余一律中性。CTA「进入工作台」是入口不是活状态 → 近黑 `btn-primary`
  （shadcn `--primary`），不用青。禁新造 hex，优先既有语义 token
  （`--background/--card/--border/--muted-foreground/--accent` hover 灰）。
- **图标 = lucide-react 现有字形**，语义贴切即可，勿自造 SVG。
- **分组标签**用 `eyebrow`（mono 大写小字，P1 已定义）。
- **权限零变化**：`matchesPath`/`gateForPath`/`navVisible` 判定逻辑逐字搬家；
  `GUARD_ONLY`（/translate→interpret 键）保留；`RouteGuard` 不动。
- **静态导出兼容**：不引入服务端 API；链接用既有 `/calls` 形式（trailingSlash 由
  next 处理）；折叠态持久化用 localStorage（`bok_sidebar_collapsed`）。
- **移动端**：现有 `hidden md:flex` 顶导航在 <md 完全没有导航（既有缺陷）——
  P2 补上：<md 侧栏转 off-canvas 抽屉 + 遮罩，顶栏汉堡开关；≥md 恒驻。
- **字幕机豁免**：本相不碰 `interpret-console.tsx` 大字幕块（bg-black/85 区）。
- **门禁**：`npx tsc --noEmit && npm run build` 全绿；P1 的扫色 grep 门禁继续零命中；
  `grep -rn "StageHeader" apps/web --include='*.tsx' --include='*.ts'` 最终零命中。

## Task 1 — `lib/navigation.ts`：导航单一事实源 + 分组

**Files:** create `apps/web/lib/navigation.ts`；edit `apps/web/components/StageHeader.tsx`
（NAV 定义与门函数移出、改为 import，渲染行为零变化）。

- 类型：`NavItem = { href; label; key?: PageKey; admin?: boolean; rootOnly?: boolean; icon: LucideIcon }`。
- 分组（顺序即侧栏顺序；`PageKey`/权限 flag 沿用 session-context 现值）：
  - 工作台：`/calls` 会话、`/interpret` 同传
  - 运营：`/roster` 名册、`/campaigns` 外呼、`/objects` 对象、`/reports` 报表
  - 内容：`/templates` 话术、`/qa` 快答库、`/knowledge` 知识库(admin)、`/personas` 人设(admin)
  - 管理：`/supervisor` 主管台(admin)、`/users` 员工(admin)、`/nodes` 节点(rootOnly)、
    `/audit` 审计(admin)、`/settings` 设置(admin)
- 导出：`NAV_GROUPS`、`FLAT_NAV`（派生扁平序，工作台→运营→内容→管理——扁平顺序变化
  是产品预期；前缀互不重叠，`gateForPath` 首匹配语义不受影响）、
  `GUARD_ONLY`、`matchesPath`、`gateForPath`、`RouteGate`、`navVisible`。
- StageHeader 改从 `@/lib/navigation` import，渲染输出与现状等价（顶导航阶段仍存活，
  Task 3 才退役）。

**Gates:** `npx tsc --noEmit && npm run build`；`grep -n "const NAV" components/StageHeader.tsx`
零命中；`/calls/new` 守卫仍命中 `calls` 键（读 gateForPath 派生确认）。

## Task 2 — Sidebar + Topbar 组件

**Files:** create `apps/web/components/layout/sidebar.tsx`、`apps/web/components/layout/topbar.tsx`。

- **Sidebar**：固定左栏 `w-60`，折叠态 `w-[3.5rem]` 图标轨；折叠状态存
  localStorage `bok_sidebar_collapsed`（init 时读，防御 SSR/static export——初渲染后再读）。
  - 分组：eyebrow 标签（折叠时隐藏标签、组间 `border-t` 分隔）；组内 item =
    icon + label，`text-sm`。
  - 激活：`isActive` 沿用 `matchesPath`；样式 `bg-(--live-soft) text-(--live-ink)`，
    折叠态加左缘 2px `--live` 竖条；hover = `bg-accent`（shadcn hover 灰）。
  - 权限过滤复用 `navVisible`（分组空则整组隐藏；root 项匿名/root 语义不变）。
  - 折叠时 icon 用 `ui/tooltip` 提示（仅折叠态渲染 Tooltip）。
  - 底部：折叠开关（PanelLeftClose/PanelLeftOpen）。
- **Topbar**：`h-14 sticky top-0 z-30 bg-background/85 backdrop-blur border-b border-border`；
  左 = 移动端汉堡（<md）+ 当前页标题（由 pathname 经 FLAT_NAV/GUARD_ONLY 反查 label）；
  右 = status slot（现 StatusBadge）+ 账号区（本地模式徽标/登录链接/用户名+退出，
  从 StageHeader 搬）+ 「进入工作台」`btn-primary` CTA。
- 移动端抽屉：<md 侧栏 `fixed` off-canvas + 半透明遮罩，汉堡开关；≥md 恒驻不遮内容。
- 纯组件本 task 不接线（AppShell 仍用 StageHeader），tsc/build 过即可；
  组件文件内 export 的 props 契约写清（sidebar 无外部状态依赖折叠态自管）。

**Gates:** `npx tsc --noEmit && npm run build`（未接线组件也要过类型与编译）。

## Task 3 — 壳装配 + StageHeader 退役

**Files:** edit `apps/web/components/app-shell.tsx`；delete `apps/web/components/StageHeader.tsx`；
`(app)/layout.tsx` 若需微调。

- `AppShell` 内：`stage-shell` 外底改 `bg-background min-h-screen`；结构改
  flex 双列——Sidebar + 右列（Topbar + `RouteGuard` 包裹的 main）。
  main 容器：`mx-auto w-full max-w-6xl px-6 pb-12 pt-6 lg:px-8`。
- `TooltipProvider` 挂双列容器最外（radix Provider 单例，全站一处）。
- StatusBadge 从 app-shell 移为 Topbar status slot 的默认实现（版本点逻辑原样）。
- `gateForPath` import 源改 `@/lib/navigation`；`NoPermission`/`SessionLoading`/
  `SessionReady`/`RouteGuard` 一字不改。
- 删除 `StageHeader.tsx`；全仓 grep `StageHeader` 零命中（含注释）。
- `/login`、`/setup`、`(stage)` 首页不经此壳，渲染零变化。

**Gates:** tsc + build；grep StageHeader 零命中；P1 扫色门禁零命中。

## Task 4 — 全站验收 + 走查

- build 产物断言：`out/` 下 17 个 (app) 页 + `/` + `/login` + `/setup` HTML 全存在。
- 视觉走查：每组至少一页（桌面全宽态 + 折叠图标轨态 + <md 抽屉态）、
  `/calls/new`（工作台满幅页在侧栏壳内的观感）、无权限页（NoPermission 在壳内）。
- 权限走查：user 角色会话下分组渲染 = 权限键 ∩ 有效集；admin 项不出现。
- ledger 更新 + 终审材料。

## 范围外（后续相）

- P3：`(stage)` 首页与 CallStudio 换官方 @agents-ui（registry 重拉，浅色 themeMode）。
- P4：图表 `--live` 化、骨架屏、campaigns dialing/in_call 撞色等收尾。
