# 浅色 UI 改版 P1（地基）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `apps/web` 全站一次性从常暗主题落到 shadcn 浅色中性 + `--live` 青色活信号 token 体系——结构与页面代码大面积零改动（@utility 保留名重写），硬雷 sweep 归零。

**Architecture:** 单文件 token 地基（`app/globals.css` 重写：双层打架 `:root` 收敛为官方浅色 oklch + 新 `--live` 家族 + stage 浅色变体），随后四个机械 sweep（透明覆盖 / 暗底浅字 / accent 语义迁移 / 图标字形），最后 shadcn 补件与全站验收。设计依据：`docs/superpowers/specs/2026-09-18-light-ui-redesign-design.md`（§2 token 全表、§3 sweep 原则、§8 验收门）。

**Tech Stack:** Tailwind v4（CSS-first `@utility`/`@theme inline`）、shadcn registry（components.json 已配 radix-nova + `@agents-ui`）、lucide-react 1.43（已装）。零新增 npm 依赖。

## Global Constraints

- **纯浅色单主题**：删除 `.dark` 块与暗色值；不新增主题切换入口。
- **零运行时行为改动**：不碰 `lib/api.ts`、连接流程、`setSinkId`、回声守卫、`localStorage` 既有键（`bok_token`/`bok.audio.*`）；P1 不新增任何 localStorage 键（侧栏偏好是 P2 的事）。
- **色彩语义**（来自 spec §1/§2，逐字执行）：主按钮=近黑（`bg-primary`）；`--live`（cyan-600 级）只做活信号（链接强调/进度/图表/在通徽标）；正文对比度 ≥4.5:1。
- **豁免清单**（唯一允许残留白/黑透明的区域）：`components/interpret-console.tsx:1560-1625` 大字幕浮窗区（`bg-black/85`、`border-white/20|30`、`text-white/70|80|90`、`bg-white/90`）——字幕机本色，逐行点名豁免。
- **Aura/mood 不在 P1 动**：`components/agents-ui/*` 的 `DEFAULT_COLOR '#1FD5F9'`、`hooks/use-mood-color.ts` 色板留给 P3/P4；P1 后它们在浅底上偏亮是已知中间态，不算 FAIL。
- **PR #100 注意**：若 #100 已合入当前分支，本计划 sweep 自然覆盖其文件；若未合，#100 合入后需对 `dashboard-page.tsx`/`campaigns/page.tsx` 重跑 Task 2-4 的 grep 门禁补 sweep。
- **门禁**（每个 Task 收尾必跑）：`cd apps/web && npx tsc --noEmit && npm run build`。
- **分支**：`feat/light-ui-p1-foundation`（从含 #100 的 main 切出）。
- 视觉走查用静态产物：`cd out && python3 -m http.server 4173` 后浏览器过页（登录页在 `/login/`）。

---

### Task 1: globals.css token 重构（全案地基）

**Files:**
- Modify: `apps/web/app/globals.css`（整文件重写，下方给出完整新内容）

**Interfaces:**
- Produces（后续所有 Task 消费）: token `--live` / `--live-strong` / `--live-soft` / `--live-soft-bg` / `--live-ink`；`@theme inline` 映射 `bg-live`、`text-live-ink`、`bg-live-soft` 等 utility；legacy 变量 `--card-border` / `--card-soft` / `--danger` 保留（浅色值）；**删除** `--accent-soft` / `--accent-ink`（Task 4 sweep 其 3 处 tsx 用法）；`text-accent` @utility 删除（Task 4 sweep 29 处用法）。
- 关键语义翻转（执行者必读）：shadcn 的 `--muted` 从此是**浅灰表面色**（zinc-100，配 `bg-muted` 用），文字灰一律 `--muted-foreground`——所以 `.muted`/`eyebrow`/`label`/`page-sub` 的实现内部改指向 `--muted-foreground`，谁直接写 `text-(--muted)` 谁就会隐形。

- [ ] **Step 1: 记录改版前基线**

```bash
cd apps/web && npx tsc --noEmit && npm run build && du -sh out
```
预期：绿。把 `du -sh out` 读数记下来（Task 7 的 bundle 哨兵基线）。

- [ ] **Step 2: 用下方完整内容重写 `apps/web/app/globals.css`**

```css
@import 'tailwindcss';
@import "tw-animate-css";
@import "shadcn/tailwind.css";
/*
  ---break---
*/
/* 纯浅色单主题：dark 兼容变体保留给 vendored 组件的 dark: 类，全站不再有 .dark 根 */
@custom-variant dark (&:is(.dark *));

/*
  Tailwind v4 默认 border-color 兼容层（照官方模板保留）。
*/
@layer base {
  *,
  ::after,
  ::before,
  ::backdrop,
  ::file-selector-button {
    border-color: var(--color-gray-200, currentcolor);
  }
  * {
    @apply border-border outline-ring/50;
  }
  body {
    @apply bg-background text-foreground;
  }
}

/* ============================================================
   自定义 utility（浅色版）。类名与旧版一一对应，页面 JSX 零改动。
   语义变化：文字灰一律 --muted-foreground（--muted 现在是浅灰表面色）；
   品牌青迁 --live 家族（活信号专用）；主按钮近黑。
   ============================================================ */

@utility card {
  /* 面板卡片：细边 + 小圆角 + 白卡 */
  @apply rounded-lg border border-(--card-border) bg-(--card) p-4;
}

@utility soft {
  /* 软面板（列表/标签块）：极浅灰 */
  @apply rounded-lg bg-(--card-soft);
}

@utility eyebrow {
  /* LiveKit 式 mono 眉标（大写、宽字距、中性灰） */
  font-family:
    ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, 'Liberation Mono',
    monospace;
  @apply text-[10px] font-bold uppercase tracking-[0.16em] text-(--muted-foreground);
}

@utility label {
  font-family:
    ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, 'Liberation Mono',
    monospace;
  @apply text-[10px] font-bold uppercase tracking-[0.16em] text-(--muted-foreground);
}

@utility page-title {
  /* 页面大标题：Light 字重 + 收紧字距 */
  @apply text-2xl font-light tracking-tight text-(--foreground);
}

@utility page-sub {
  @apply mt-1.5 text-sm text-(--muted-foreground);
}

@utility muted {
  color: var(--muted-foreground);
}

@utility btn {
  /* 按钮：官方 rounded(4px) + h-7 + text-xs semibold */
  @apply inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-sm text-xs font-semibold transition;
}

@utility btn-primary {
  /* 主按钮 = 近黑（spec §1：中性骨架）；青色不进按钮 */
  @apply btn h-7 px-3 bg-primary text-primary-foreground hover:bg-primary/90;
}

@utility btn-ghost {
  @apply btn h-7 px-3 border border-(--card-border) bg-transparent text-(--foreground) hover:bg-accent;
}

@utility input {
  /* 输入框 / 下拉：细边 + 小圆角 + 白底；focus 用 --live（活信号） */
  @apply w-full rounded-lg border border-(--card-border) bg-(--background) px-3 py-1.5 text-sm text-(--foreground) outline-hidden placeholder:text-(--muted-foreground) focus:border-(--live);
}

@utility select {
  @apply rounded-lg border border-(--card-border) bg-(--background) px-3 py-1.5 text-sm text-(--foreground) outline-hidden focus:border-(--live);
}

@utility textarea {
  /* 多行输入(同传术语表/QA 问答对):input 同款细边,可纵向拉伸 */
  @apply w-full resize-y rounded-lg border border-(--card-border) bg-(--background) px-3 py-1.5 text-sm text-(--foreground) outline-hidden placeholder:text-(--muted-foreground) focus:border-(--live);
}

@utility dot {
  /* 状态小圆点 */
  @apply inline-block h-2 w-2 rounded-full;
}

@utility stage-shell {
  /* —— 舞台浅色变体：#fafafa 底 + 极淡青晕（spec §2）—— */
  background:
    radial-gradient(60% 55% at 50% 40%, var(--live-soft-bg), transparent 72%),
    var(--stage-bg);
  color: var(--foreground);
}

@utility stage-col {
  /* 遥测列容器：白面板；内部用细分隔线而非外框 */
  @apply flex w-full flex-col rounded-sm bg-(--card);
}

@utility stage-col-inner {
  @apply flex min-h-0 flex-col;
}

@utility stage-title {
  /* 分区标题：mono 大写，text-xs bold */
  @apply pb-2 font-mono text-xs font-bold uppercase tracking-[0.16em] text-(--foreground);
}

@utility stage-sep {
  /* 分区分隔线 */
  @apply flex flex-col border-t border-(--card-border) py-4 pr-3 first:border-t-0 first:pt-0 first:pb-2;
}

@utility stage-row {
  /* 「灰标签 → 青值」遥测行 */
  @apply flex w-full items-baseline justify-between gap-4 pb-1;
}

@utility stage-key {
  @apply font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-(--stage-muted);
}

@utility stage-value {
  @apply font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-(--stage-value);
}

@utility stage-glow {
  text-shadow: 0 0 14px rgb(8 145 178 / 0.35);
}

@utility stage-btn {
  /* 主/次舞台按钮：官方 rounded（4px）+ h-7 + text-xs semibold */
  @apply inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-sm text-xs font-semibold transition;
}

@utility stage-btn-primary {
  /* 舞台内主按钮 = --live（舞台是「活」区域，与全局近黑主按钮的区分是刻意的） */
  @apply stage-btn h-7 px-3 bg-(--live) text-white hover:bg-(--live-strong);
}

@utility stage-btn-ghost {
  @apply stage-btn h-7 px-3 border border-(--card-border) bg-(--card) text-(--foreground) hover:bg-accent;
}

@utility stage-btn-secondary {
  /* 次级舞台按钮：ghost 描边样式（interpret 页/一体台控制按钮；别名复用 stage-btn-ghost） */
  @apply stage-btn-ghost;
}

/* 注：旧 @utility text-accent 已删除——品牌青迁 --live，Task 4 全量 sweep */

@layer utilities {
  * {
    box-sizing: border-box;
  }

  html,
  body {
    margin: 0;
    padding: 0;
    background: var(--background);
    color: var(--foreground);
    font-family:
      'Neue Montreal',
      'Söhne',
      ui-sans-serif,
      system-ui,
      -apple-system,
      'Segoe UI',
      Roboto,
      'Helvetica Neue',
      Arial,
      'PingFang SC',
      'Microsoft YaHei',
      sans-serif;
    -webkit-font-smoothing: antialiased;
  }
}

/* WhatsApp 對接通知爆閃:call 行 / 橫幅 邊框+底色閃（浅色重校：青色信号） */
@keyframes wa-flash-kf {
  0%,
  100% {
    box-shadow: 0 0 0 1px rgb(8 145 178 / 0.2), 0 0 0 rgb(8 145 178 / 0);
    background-color: rgb(8 145 178 / 0.06);
  }
  50% {
    box-shadow: 0 0 0 1px var(--live), 0 0 18px rgb(8 145 178 / 0.35);
    background-color: rgb(8 145 178 / 0.14);
  }
}
.wa-flash {
  animation: wa-flash-kf 1.6s ease-in-out infinite;
}
/*
  ---break---
*/
@theme inline {
  --color-sidebar-ring: var(--sidebar-ring);
  --color-sidebar-border: var(--sidebar-border);
  --color-sidebar-accent-foreground: var(--sidebar-accent-foreground);
  --color-sidebar-accent: var(--sidebar-accent);
  --color-sidebar-primary-foreground: var(--sidebar-primary-foreground);
  --color-sidebar-primary: var(--sidebar-primary);
  --color-sidebar-foreground: var(--sidebar-foreground);
  --color-sidebar: var(--sidebar);
  --color-chart-5: var(--chart-5);
  --color-chart-4: var(--chart-4);
  --color-chart-3: var(--chart-3);
  --color-chart-2: var(--chart-2);
  --color-chart-1: var(--chart-1);
  --color-ring: var(--ring);
  --color-input: var(--input);
  --color-border: var(--border);
  --color-destructive: var(--destructive);
  --color-accent-foreground: var(--accent-foreground);
  --color-accent: var(--accent);
  --color-muted-foreground: var(--muted-foreground);
  --color-muted: var(--muted);
  --color-secondary-foreground: var(--secondary-foreground);
  --color-secondary: var(--secondary);
  --color-primary-foreground: var(--primary-foreground);
  --color-primary: var(--primary);
  --color-popover-foreground: var(--popover-foreground);
  --color-popover: var(--popover);
  --color-card-foreground: var(--card-foreground);
  --color-card: var(--card);
  --color-foreground: var(--foreground);
  --color-background: var(--background);
  /* Bok 扩展：--live 家族 + danger（bg-live / text-live-ink / bg-live-soft …） */
  --color-live: var(--live);
  --color-live-strong: var(--live-strong);
  --color-live-soft: var(--live-soft);
  --color-live-soft-bg: var(--live-soft-bg);
  --color-live-ink: var(--live-ink);
  --color-danger: var(--danger);
  --radius-sm: calc(var(--radius) * 0.6);
  --radius-md: calc(var(--radius) * 0.8);
  --radius-lg: var(--radius);
  --radius-xl: calc(var(--radius) * 1.4);
  --radius-2xl: calc(var(--radius) * 1.8);
  --radius-3xl: calc(var(--radius) * 2.2);
  --radius-4xl: calc(var(--radius) * 2.6);
}
/*
  ---break---
  shadcn token（官方 neutral 浅色基准）+ Bok 扩展（--live 活信号 / stage 浅色变体）。
  纯浅色单主题：全站只有 :root 一档，.dark 已删。
  语义注意：--muted 是浅灰表面（配 bg-muted）；文字灰用 --muted-foreground；
  --accent 回归 shadcn 本义（hover 浅灰底），品牌青在 --live。
*/
@layer base {
  :root {
    --background: oklch(1 0 0);
    --foreground: oklch(0.145 0 0);
    --card: oklch(1 0 0);
    --card-foreground: oklch(0.145 0 0);
    --popover: oklch(1 0 0);
    --popover-foreground: oklch(0.145 0 0);
    --primary: oklch(0.205 0 0);
    --primary-foreground: oklch(0.985 0 0);
    --secondary: oklch(0.97 0 0);
    --secondary-foreground: oklch(0.205 0 0);
    --muted: oklch(0.97 0 0);
    --muted-foreground: oklch(0.556 0 0);
    --accent: oklch(0.97 0 0);
    --accent-foreground: oklch(0.205 0 0);
    --destructive: oklch(0.577 0.245 27.325);
    --border: oklch(0.922 0 0);
    --input: oklch(0.922 0 0);
    --ring: oklch(0.708 0 0);
    --chart-1: oklch(0.646 0.222 41.116);
    --chart-2: oklch(0.6 0.118 184.704);
    --chart-3: oklch(0.398 0.07 227.392);
    --chart-4: oklch(0.828 0.189 84.429);
    --chart-5: oklch(0.769 0.188 70.08);
    --radius: 0.625rem;
    --sidebar: oklch(0.985 0 0);
    --sidebar-foreground: oklch(0.145 0 0);
    --sidebar-primary: oklch(0.205 0 0);
    --sidebar-primary-foreground: oklch(0.985 0 0);
    --sidebar-accent: oklch(0.97 0 0);
    --sidebar-accent-foreground: oklch(0.205 0 0);
    --sidebar-border: oklch(0.922 0 0);
    --sidebar-ring: oklch(0.708 0 0);

    /* Bok 扩展（旧名保留、值换浅）：页面/表格直接引用这些变量 */
    --card-soft: oklch(0.985 0 0);
    --card-border: oklch(0.922 0 0);
    --danger: oklch(0.577 0.245 27.325);

    /* 品牌活信号（spec §2 全表）：cyan-600 起步保白底对比度 */
    --live: oklch(0.715 0.143 215.221); /* cyan-600 ≈ #0891b2 */
    --live-strong: oklch(0.609 0.126 221.723); /* cyan-700 ≈ #0e7490 */
    --live-soft: oklch(0.956 0.045 203.388); /* cyan-100 ≈ #cffafe */
    --live-soft-bg: oklch(0.984 0.019 200.873); /* cyan-50 ≈ #ecfeff */
    --live-ink: oklch(0.398 0.07 223.393); /* cyan-900 ≈ #155e75 */

    /* 官方「舞台」token 浅色变体 */
    --stage-bg: oklch(0.985 0 0);
    --stage-bg-2: var(--live-soft-bg);
    --stage-panel: oklch(1 0 0);
    --stage-panel-2: oklch(0.97 0 0);
    --stage-border: oklch(0.922 0 0);
    --stage-border-soft: oklch(0.961 0 0);
    --stage-muted: oklch(0.556 0 0);
    --stage-value: var(--live);
    --stage-value-dim: oklch(0.715 0.143 215.221 / 45%);
  }
}
```

- [ ] **Step 3: 构建门禁 + 烟囱走查**

```bash
cd apps/web && npx tsc --noEmit && npm run build
```
预期：绿（此步会有大量页面仍带暗底浅字/白透明——预期内，Task 2-4 处理）。
```bash
cd out && python3 -m http.server 4173
```
浏览器抽查三页：`/`（工作台）、`/calls/`、`/interpret/`——底色应为白/浅灰，文字可读，无隐形文字（若发现 `text-(--muted)` 之类内联引用直接隐形的，记下 file:line 归入 Task 4 sweep）。大字幕浮窗开一次确认仍黑底。

- [ ] **Step 4: Commit**

```bash
cd /Users/halo/Documents/bok/voice-assistant
git add apps/web/app/globals.css
git commit -m "feat(web): P1 地基——globals.css 重构为浅色 token 体系

双层打架 :root 收敛为 shadcn neutral 浅色；--live 家族接替品牌青语义；
--muted 语义翻转（表面色），文字灰全量转 --muted-foreground；
.dark 删除；@utility 保留名重写；大字幕浮窗黑底豁免保留。"
```

---

### Task 2: 透明覆盖 sweep（bg-white / bg-black / border-white）

**Files（grep 实测分布，15 文件 43 处 bg-white/ + 4 处 bg-black/ + 5 处 border-white/）：**
- Modify: `app/(app)/calls|campaigns|knowledge|nodes|objects|personas|qa|reports|settings|supervisor|templates|users/page.tsx`、`components/CallStudio.tsx`、`components/dashboard-page.tsx`、`components/interpret-console.tsx`、`components/listen-panel.tsx`、`app/(app)/translate/page.tsx`
- 豁免不改：`components/interpret-console.tsx:1565,1583,1590,1602,1607`（大字幕浮窗区，含其中 `border-white/30`、`text-white/70|80`）

**Interfaces:**
- Consumes: Task 1 的 `bg-muted`、`bg-accent`、`soft`、`--live-soft`。
- Produces: 全站 grep `bg-white/|bg-black/|border-white/` 仅剩豁免 5 行。

**映射表（逐条机械替换）：**

| 旧 | 新 | 语义 |
|---|---|---|
| `bg-white/5` | `bg-muted/60` | 常驻软底（行 hover 底、bar 轨道、软面板） |
| `bg-white/10` | `bg-muted` | 强一档软底（选中/强调块） |
| `bg-white/20` | `bg-muted` |（仅 1 处，knowledge 页） |
| `bg-white/90` | 豁免区外不许出现；出现则 `bg-card` | |
| `bg-black/20`（CallStudio:231 波形轨道、listen-panel:18 转写面板、translate:265 字幕块） | `bg-muted` | 页内面板，非字幕机 |
| `hover:bg-white/5` | `hover:bg-muted/60` | |
| `hover:bg-white/10` | `hover:bg-accent` | 交互 hover 用 shadcn accent 本义 |

- [ ] **Step 1: 列出全部命中并逐条替换**

```bash
cd apps/web && grep -rn "bg-white/\|bg-black/\|border-white/" app components --include="*.tsx"
```
按映射表逐文件替换；替换前先肉眼确认该处不是豁免区 5 行。

- [ ] **Step 2: 门禁 grep 归零（豁免除外）**

```bash
cd apps/web && grep -rn "bg-white/\|bg-black/\|border-white/" app components --include="*.tsx" | grep -v "interpret-console.tsx:15[6-9][0-9]\|interpret-console.tsx:16[0-2][0-9]"
```
预期：零输出。

- [ ] **Step 3: 构建门禁**

```bash
cd apps/web && npx tsc --noEmit && npm run build
```
预期：绿。

- [ ] **Step 4: Commit**

```bash
cd /Users/halo/Documents/bok/voice-assistant
git add apps/web
git commit -m "refactor(web): P1 sweep——透明覆盖换语义 token（47 处）

bg-white/5→bg-muted/60、bg-white/10→bg-muted、hover→bg-accent、
页内 bg-black/20 面板→bg-muted；大字幕浮窗 5 行豁免（字幕机本色）。"
```

---

### Task 3: 暗底浅字与语义色 sweep（text-*-200/300/400 与 alpha badge 对）

**Files（grep 实测分布）：** `app/(app)/supervisor|campaigns|calls|users|nodes|templates|settings|objects|audit|reports|knowledge|roster|qa|personas/page.tsx`、`components/{app-shell,CallStudio,interpret-console,listen-panel,dashboard-page,StageHeader}.tsx`、`app/setup/page.tsx`、`app/login/page.tsx`

**Interfaces:**
- Consumes: Task 1 浅色底（这些字色只在浅底上才需要换档）。
- Produces: 全站 grep `text-(red|emerald|amber|sky|green|rose|orange|yellow)-[23]00` 归零。
- **豁免（与 Task 2 同一豁免区）**：`components/interpret-console.tsx` 大字幕浮窗块（以现场 grep `bg-black/85` 定位 overlay 行区间为准，rebase 后约 1608-1654）内的 `text-sky-300`/`text-emerald-300` 位于**黑底**上，浅色字正确，**不改**；grep 门禁按现场行区间排除，勿信陈旧行号。

**映射表：**

| 旧（文字用） | 新 | 备注 |
|---|---|---|
| `text-red-300` / `text-red-400` / `text-rose-400` | `text-red-600` | 错误/危险文字 |
| `text-amber-300` | `text-amber-700` | amber-600 白底对比度不足 |
| `text-sky-300` | `text-sky-700` | |
| `text-emerald-300` / `text-emerald-400`（setup:59 状态文字） | `text-emerald-700` / `text-emerald-600` | 文字用 -600 起步 |
| `text-red-200/80` 等带透明度变体 | `text-red-600` | grep 时留意 `/80` 后缀 |
| `bg-emerald-400`（状态点） | `bg-emerald-500` | 底色点 -400 可留，顺手统一 |
| `bg-sky-400` / `bg-amber-400`（CallStudio:30-32 图例点） | 不改 | 底色用途，白底可读 |
| `*-400/15` badge 对（nodes:15 emerald、users:17-18 amber/sky、templates:418 sky） | `bg-emerald-100`/`bg-amber-100`/`bg-sky-100` + 对应文字按上表 | 浅色 badge = 实色浅底 + 深字 |

- [ ] **Step 1: 列出命中并按映射表替换**

```bash
cd apps/web && grep -rEn "text-(red|emerald|amber|sky|green|rose|orange|yellow)-[234]00" app components --include="*.tsx" && grep -rEon "(emerald|amber|sky|red)-400/[0-9]+" app components --include="*.tsx"
```
逐条替换；badge 对（`bg-*-400/15 … text-*-300` 同一元素）成对改，防止单改一半出现浅底浅字。

- [ ] **Step 2: 门禁 grep 归零（字幕机豁免段除外）**

```bash
cd apps/web && grep -rEn "text-(red|emerald|amber|sky|green|rose|orange|yellow)-[23]00" app components --include="*.tsx" | grep -v "interpret-console.tsx:16[0-5][0-9]"
```
预期：零输出（豁免区间以现场 overlay 块为准，上式覆盖 1600-1659；若有出区间命中且确实在黑底浮窗内，按块豁免并在报告点名）。

- [ ] **Step 3: 构建门禁**

```bash
cd apps/web && npx tsc --noEmit && npm run build
```
预期：绿。

- [ ] **Step 4: Commit**

```bash
cd /Users/halo/Documents/bok/voice-assistant
git add apps/web
git commit -m "refactor(web): P1 sweep——暗底浅字转浅底深字（约 30 处）

text-*-300/400 → -600/-700；alpha badge 对 → 实色浅底+深字。"
```

---

### Task 4: accent 语义迁移 sweep（text-accent / --accent 内联 / 残留 hex）

**Files（grep 实测分布）：**
- `text-accent` 29 处：calls:175,205,229,242 / settings:133,696,878 / objects:273,326,336 / qa:280 / personas:497,634 / audit:111 / templates:376,460,508,519,590 / campaigns:480 / supervisor:229,257,318,379 / login:91 / dashboard-page:88,105 / CallStudio:1117,1220
- `bg-(--accent)/15`：calls:205、supervisor:318；`border-(--accent)`：qa:280、templates:376
- `bg-(--accent)` 实底按钮：`app/setup/page.tsx:65`（同处修 `text-(--bg)` 坏变量）
- `accent-soft/accent-ink` 残留 3 处：执行时以 grep 为准
- hex：`app/login/page.tsx:43`、`components/StageHeader.tsx:100`（logo `text-[#01191c]`）、`components/CallStudio.tsx:190`（canvas `var(--accent, #22d3ee)`）

**Interfaces:**
- Consumes: Task 1 的 `--live` 家族与 `text-live-ink`。
- Produces: 全站 tsx 零 `text-accent`、零 `--accent`/`--accent-soft`/`--accent-ink` 引用、零 `#01191c`/`#4adcfa`/`#2c2d2d`/`#141515`。

**映射表：**

| 旧 | 新 |
|---|---|
| `text-accent` | 链接/CTA 强调 → `text-(--live)`；非链接小字段（如 `audit:111` 的 action 指纹列）→ `text-(--live-ink)`（cyan-900，小字对比度更稳） |
| `bg-(--accent)/15` | `bg-(--live-soft)` |
| `border-(--accent)`（qa/templates tab 选中态） | `border-(--live)` |
| `bg-(--accent) … text-(--bg)`（setup:65 下载按钮） | `bg-(--live) … text-white` |
| `var(--accent, #22d3ee)`（CallStudio:190 波形 stroke） | `var(--live, #0891b2)` |
| `bg-accent`（dashboard:215 时长条形图**填充**，Task 1 后浅灰隐形） | `bg-live`（图表填充=活信号；轨道保持 `bg-muted/60` 形成对比） |
| `bg-(--accent)`（interpret-console:1577 src 字幕气泡底） | `bg-(--live-soft)`；同元素 `text-(--accent-ink)` → `text-(--live-ink)`（顺带消化 Task 1 deferred：该行引用已删变量） |
| campaigns SEGMENTS 待拨段 `bg-muted`（Task 2 映射碰撞：与空轨道不可分辨） | `bg-foreground/10`（深一档中性，与 `bg-muted/60` 轨道拉开） |
| `text-[#01191c]`（logo 方块字，login:43/StageHeader:100） | `text-white`（方块底色 `bg-(--stage-value)` 已是 cyan-600） |

- [ ] **Step 1: 列出命中并按映射表替换**

```bash
cd apps/web && grep -rn "text-accent\|accent-soft\|accent-ink" app components --include="*.tsx" && grep -rn "(--accent" app components --include="*.tsx" | grep -v "card-border"
```
逐条替换。

- [ ] **Step 2: 门禁 grep 归零**

```bash
cd apps/web && grep -rn "text-accent\|accent-soft\|accent-ink\|#01191c\|#4adcfa\|#2c2d2d\|#141515" app components --include="*.tsx"; grep -rn "(--accent)" app components --include="*.tsx"
```
预期：两组均零输出。

- [ ] **Step 3: 构建门禁**

```bash
cd apps/web && npx tsc --noEmit && npm run build
```
预期：绿。

- [ ] **Step 4: Commit**

```bash
cd /Users/halo/Documents/bok/voice-assistant
git add apps/web
git commit -m "refactor(web): P1 sweep——accent 品牌青语义迁 --live（32 处）

text-accent→text-(--live)、badge 底→live-soft、tab 选中→border-live；
setup 下载按钮顺手修 text-(--bg) 坏变量；logo 反白字 token 化。"
```

---

### Task 5: shadcn 补件（12 件，P2/P3 消费）

**Files:**
- Create: `apps/web/components/ui/{input,label,textarea,checkbox,switch,dialog,dropdown-menu,table,tabs,badge,card,tooltip,skeleton}.tsx`（shadcn CLI 生成）
- Modify: `apps/web/package.json`（CLI 可能补 radix 子包依赖）

**Interfaces:**
- Consumes: Task 1 浅色 token（新组件自动吃 `--primary/--accent/...` 浅色值）。
- Produces: `components/ui/` 下的标准 shadcn 原语，导出名与官方一致（`Input`、`Dialog`, `DropdownMenu`, `Table`, `Tabs`, `Badge`, `Card`, `Tooltip`, `Skeleton`…），P2 壳与 P3 舞台直接 import。
- **不重拉**：`button`/`select`/`toggle`/`separator`（已 vendored，防覆盖）；不预拉 toast/sonner（YAGNI）。

- [ ] **Step 1: registry 拉件**

```bash
cd apps/web && npx shadcn@latest add input label textarea checkbox switch dialog dropdown-menu table tabs badge card tooltip skeleton
```
若 CLI 询问覆盖/依赖，全部确认安装、**不覆盖**既有文件（列表里没有既有文件名，出现提示即停下人工确认）。

- [ ] **Step 2: 构建门禁 + 冒烟 import**

```bash
cd apps/web && npx tsc --noEmit && npm run build
```
预期：绿。抽查 `components/ui/badge.tsx` 存在且 import 自 `radix-ui`/`cn` 同源（不引入第二套 cn）。

- [ ] **Step 3: Commit**

```bash
cd /Users/halo/Documents/bok/voice-assistant
git add apps/web
git commit -m "feat(web): P1 补件——shadcn 原语 12 件入 components/ui

input/label/textarea/checkbox/switch/dialog/dropdown-menu/table/tabs/
badge/card/tooltip/skeleton；不覆盖既有 button/select/toggle/separator。"
```

---

### Task 6: lucide 图标替换（仅独立图标位，散文不动）

**Files:**
- Modify: `components/app-shell.tsx`（LoadingState/EmptyState/ErrorState 的 `⛔ ⚠` 字形）、`components/StageHeader.tsx`、各页 glyph 按钮
- 边界规则（防过度替换）：**只换「独立图标位」**——元素内仅含该字形、或按钮内除文字外独立的图标字符；句内散文箭头（如 templates 页「如果客户… → 就…」说明文字、表格内「查看 →」的语义性链接尾箭头）**一律不动**。

**Interfaces:**
- Consumes: `lucide-react` 1.43（已装）。
- Produces: 图标统一 `<Icon className="h-3.5 w-3.5" />` 档位（h-7 按钮内 h-3.5，空态 h-5）。

- [ ] **Step 1: grep 出独立字形位并按映射替换**

```bash
cd apps/web && grep -rn "⛔\|⚠\|✕\|＋" app components --include="*.tsx" | head -40
```

映射：`⛔`→`<Ban />`、`⚠`/`⚠️`→`<TriangleAlert />`、`✕`→`<X />`、`＋`/`+ `（新建类按钮）→`<Plus />`。替换形如：

```tsx
// 旧
<button className="btn-ghost text-xs" onClick={del}>⛔ 删除</button>
// 新
import { Ban } from "lucide-react";
<button className="btn-ghost text-xs" onClick={del}><Ban className="h-3.5 w-3.5" /> 删除</button>
```

`→`（ArrowRight）只换「整元素只有箭头」的 CTA 尾（如 calls:229 `进入 →`、dashboard:105 `查看 →`→ 图标+保留文字「进入/查看」，箭头字符删除）；句内说明文字的箭头不动。

- [ ] **Step 2: 构建门禁**

```bash
cd apps/web && npx tsc --noEmit && npm run build
```
预期：绿。

- [ ] **Step 3: Commit**

```bash
cd /Users/halo/Documents/bok/voice-assistant
git add apps/web
git commit -m "feat(web): P1 图标——独立字形位换 lucide（散文/句内箭头不动）"
```

---

### Task 7: P1 全站验收

**Files:**
- 无新改动（发现问题回对应 Task 修）

**Interfaces:**
- Consumes: Task 1-6 全部产物。
- Produces: P1 完成判定（spec §9 P1 行：全站 20 页立变浅色无漏底）。

- [ ] **Step 1: 门禁 grep 总闸（全绿才算过）**

```bash
cd apps/web
echo "--- 裸透明（字幕机浮窗块外应为 0；浮窗区间以现场 bg-black/85 overlay 为准，约 1608-1654）---"
grep -rn "bg-white/\|bg-black/\|border-white/" app components --include="*.tsx" | grep -vc "interpret-console.tsx:16[0-5][0-9]"
echo "--- 暗底浅字（字幕机浮窗块外应为 0）---"
grep -rEn "text-(red|emerald|amber|sky|green|rose|orange|yellow)-[23]00" app components --include="*.tsx" | grep -v "interpret-console.tsx:16[0-5][0-9]" | wc -l
echo "--- accent 残留（应为 0）---"
grep -rn "text-accent\|accent-soft\|accent-ink" app components --include="*.tsx" | wc -l
echo "--- globals 内 hex 残留（应为 0）---"
grep -n "#070707\|#0d0e0e\|#1fd5f9\|#4adcfa\|#01191c\|#2c2d2d\|#141515\|#012a32\|#ff6b6b" app/globals.css | wc -l
```
预期：四组输出均为 `0`（第一组 `grep -vc` 计数为 0）。

- [ ] **Step 2: 构建 + bundle 哨兵**

```bash
cd apps/web && npx tsc --noEmit && npm run build && du -sh out
```
对比 Task 1 Step 1 基线：涨幅 >15% 需排查说明（预期变化来自 shadcn 12 件 + lucide 图标，应在个位数百分比）。

- [ ] **Step 3: 20 页视觉走查（`cd out && python3 -m http.server 4173`）**

逐页过：`/ /calls/ /calls/new/ /roster/ /campaigns/ /objects/ /qa/ /templates/ /interpret/ /translate/ /reports/ /supervisor/ /users/ /nodes/ /knowledge/ /personas/ /audit/ /settings/ /login/ /setup/`。
每页三问：①有没有漏底暗块（大字幕浮窗除外）②文字是否全部可读（重点：`.muted`、表格行、badge）③主按钮是否近黑、青色是否只出现在信号位。
发现漏底 → 回 Task 2-4 补 sweep + 重跑 Step 1。

- [ ] **Step 4: Commit（如有修补）+ 汇报**

```bash
git add apps/web && git commit -m "fix(web): P1 验收走查修补"
```
向用户汇报：走查结果 + bundle 读数对比 + 已知中间态清单（Aura 亮青、mood 色板——P3/P4 处理）。
