# Light UI P4 — 打磨：图表/空态/mood/撞色/抽屉 a11y/全站走查

Spec: `docs/superpowers/specs/2026-09-18-light-ui-redesign-design.md` §7/§8/§9（P4 行）。
Base: `feat/light-ui-p4-polish` @ `91ad5c6`（= P3 舞台头）。

## Goal

改版收官：dashboard 图表 `--live` 化、skeleton/空态全量、mood 色板浅色校准、
campaigns 状态撞色收编、抽屉 dialog-a11y、动效微调；全站视觉走查过 §8 门。

## Global Constraints

- 色板纪律同前：语义 token + 青只作活信号；**状态色例外**——语义状态色
  （成功绿/警告琥珀/危险红/信息蓝）保留 Tailwind 标准档（400/500/600 按底色对比选），
  但**同页同义必须同色**：campaigns 的 dialing（bg-sky-400）与相邻 in_call 撞色
  ——dialing 改 `bg-blue-500`、in_call 保持 `bg-emerald-500` 类语义分色（P2 遗留项）。
- 空态/骨架文案不变（spec §7：文案不动，只换视觉）。
- 铁律延续：运行时行为零变化；大字幕黑块豁免。
- 新依赖零新增（motion/chroma-js/lucide 已装）。
- 门禁每 task：`cd apps/web && npx tsc --noEmit && npm run build`。

## Task 1 — dashboard 图表 --live 化 + 状态撞色收编

- `components/dashboard-page.tsx`：六 KPI 卡数字/图标、时长分布条填充、坐席排行
  条、标记双表徽标——数据墨色换 `--live` 系（数值高亮 `text-(--live-ink)`、
  bar 填充 `bg-(--live)`、轨道 `bg-muted`）；**状态徽标语义色不青化**（绿/琥珀/红
  保留）。
- campaigns 页状态徽标：dialing `bg-sky-400`→`bg-blue-500`，与 in_call
  （emerald）区分；全页 badge 档位对齐 P1 范式（`*-100` 底 + `*-700` 字）。
- 全站散色扫尾：`grep` 残留 `--stage-*`（豁免区外）、随机 hex、同义异色，
  逐处收编语义/标准档。

**Gates:** tsc/build；`grep -n "bg-sky-400" apps/web/components/*campaign*` 零命中；
散色清单进报告。

## Task 2 — skeleton/空态全量 + mood 色板浅色校准

- `components/app-shell.tsx` 的 `LoadingState/EmptyState/ErrorState`：Skeleton
  骨架（shadcn `ui/skeleton`）+ lucide 空态图标（Inbox/AlertCircle 类）+ 文案原样；
  全站使用点自动收编（组件级改动）。
- 高频列表页（calls/roster/objects/templates/qa）加载态如手写 spinner 则换 Skeleton；
  数据结构不动。
- mood 色板浅色校准：`hooks/use-mood-color.ts` 官方 11 色映射在白底的对比度
  校准（暗色调深的档位微调 hex→oklch 或标准档，**不改映射逻辑**，只调色值表）；
  Aura 上色仍经此钩子。

**Gates:** tsc/build；空态文案 grep 抽查不变；mood 色值 diff 表进报告。

## Task 3 — 抽屉 dialog-a11y + 动效微调 + 全站终验

- sidebar 移动抽屉打开态：`role="dialog"` + `aria-modal="true"` + 焦点圈定
  （Tab 循环在抽屉内，关闭还焦汉堡按钮）；Esc/滚动锁/遮罩渐隐保持。
- 动效微调（限 CSS/已装 motion）：侧栏折叠宽度过渡（`transition-[width]` 200ms）、
  抽屉滑出时序校准；不引新依赖。
- **全站终验**：21 路由 build 产物齐全；浏览器走查全量（含 P1 豁免区确认、
  对比度抽查、collapsed/mobile/舞台路由各态）；ledger 收尾 + §8 行为回归
  （浏览器结构层可验部分；真机通话/同传 E2E 列 Ethan 验收清单，不假绿）。

**Gates:** tsc/build；走查表全过；`progress.md` 完稿。

## 范围外

真机全流程手测（Ethan）；CP 侧池泄漏修复（另案）；`.superpowers` 账本不入库。
