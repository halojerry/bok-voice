# QA 流程图执行引擎（Phase 2）设计

- 日期：2026-09-18
- 状态：已评审方向（Ethan 拍板「画布=workflow，图就是你跑的东西」），待 plan/SDD 执行
- 前置：[2026-09-17-qa-canvas-design.md](2026-09-17-qa-canvas-design.md)（Phase 1 画布，§12 路线图本文升格其 Phase 2）
- 代码落点：`packages/core/bok_voice_core/flow_graph.py`（纯函数）+ `apps/agent/agent_runtime/`（插线）+ `apps/control-plane`（校验/落库）+ `apps/web`（画布/编辑器）

## 1. 背景：画布与引擎说的不是一种语言

Phase 1 画布的三种边（脊柱/簇/步骤挂载）全是**记账**：`cluster_head_id`、`step_index`
只用于展示与弱过滤，`FlowController` 每轮读的是硬编码规则（`rule_verdict` /
`should_auto_advance` / 背景 judge）+ prompt。连一条线、少一条线，通话行为零变化。

而「意图判定、条件触发 QA/话术」的判定器其实都已存在，只是长在代码里：

| 能力 | 现在的位置 | 缺口 |
| --- | --- | --- |
| 确认/拒绝/含糊判定 | `flow.py` `rule_verdict` + 背景 judge | 硬编码正则+4B，运营不可配 |
| 字面触发 QA | `qa_gate.py` 0.90 字面命中 | 语料全量匹配，无业务条件绑定 |
| 步内分支 | `match_step_branch`（如果客户X→就Y） | 写死在 ref 文本里 |
| 跳步 | **无**（`advance()` 只会 +1） | 投诉转人工/客户要求找主管类场景无法跳 |

Phase 2 = 把「条件→动作」做成数据（`graph_json`），引擎每轮读图。**画布画的 = 引擎跑的。**

## 2. 目标 / 非目标

目标：

1. 模板可选携带 `graph_json`（意图节点 + 绑定边）；空串 = 未启用 = 逐轮行为零变化。
2. 运行时**确定性关键词**命中 → 两类动作：
   - `play_qa`：播指定 QA 条目的罐头音频（与 QA 快路同一条播放出口，跳过 LLM）
   - `jump_step`：跳到任意步（复用推进副作用包，本轮按新步指引继续回答）
3. 画布可视化意图节点与绑定边；调色盘「＋ 意图」加节点；编辑器落库（PUT 模板）。
4. kill-switch `BOK_FLOW_GRAPH`、turns 账本 `provider` 标记、探针 A/B。

非目标（后续期，别在本期顺手做）：

- judge 判据上图（模糊轮意图判定）——Phase 3 意图引擎，需探针 A/B 单独立项
- `say` 动作（直念自定义文本）——与「prompt 语言纯度」铁律纠缠（共享指引必须标准书面
  中文，运营直念文本绕过该防线），另立专项
- 多答案轮换、优先级学习、正则触发、意图跨通话沉淀（XKT「场景学习」类）

## 3. 契约：graph_json

存储：`conversation_templates.graph_json TEXT DEFAULT ''`（空=未启用）。模板版本化
自动覆盖（`update_template` 快照旧版 `before` 含该列）。通话用建单快照
（`call_sessions.template_id`），图随模板快照走，中途改图不影响在途通话。

```json
{
  "version": 1,
  "intents": [
    {"id": "int_1a2b3c4d", "label": "投诉", "keywords": ["投诉", "举报"],
     "steps": [], "enabled": true}
  ],
  "bindings": [
    {"id": "bnd_7e8f9a0b", "intent": "int_1a2b3c4d", "action": "jump_step",
     "step": 4, "priority": 10, "once": false, "enabled": true},
    {"id": "bnd_c1d2e3f4", "intent": "int_2b3c4d5e", "action": "play_qa",
     "qa_id": "qa-xxxx", "priority": 10, "once": true, "enabled": true}
  ]
}
```

字段语义：

| 字段 | 语义 |
| --- | --- |
| `intents[].keywords` | 触发词，**确定性子串匹配**（双侧 casefold）；无正则（运营面安全） |
| `intents[].steps` | 生效步号集合，**1-based**（与画布「第 N 步」显示一致）；`[]`=全程生效 |
| `bindings[].action` | `play_qa`（需 `qa_id`）或 `jump_step`（需 `step`，1-based） |
| `bindings[].priority` | 小者先；同轮多命中只执行一个动作，按 `(priority, id)` 排序取首个 |
| `bindings[].once` | `true` = 每通至多执行一次（`FlowController.graph_fired` 账本） |
| `enabled` | 软开关；禁用意图/绑定不删数据 |

限制与 id 规则：

- `graph_json` ≤ 65536 字节；intents ≤ 64；bindings ≤ 128；keywords ≤ 32×64 字；
  label ≤ 64 字；priority ∈ [0,1000] 默认 10；step ∈ [1,999]（运行时按实际步数钳制）
- id：`int_`/`bnd_` + 8 位十六进制（`^(?:int|bnd)_[0-9a-f]{8}$`），web 端生成
- `qa_id` **保存时不校验存在性**（QA 条目可见性按 owner 收窄、且可后删）；运行时
  `QaIndex.by_id` 解析不到 → 放行 LLM + 日志（防悬空引用炸轮）

校验双轨（`packages/core/bok_voice_core/flow_graph.py`，CP 与 agent 共用）：

- `validate_flow_graph(raw) -> list[str]`：**严格**，CP 保存用；错误列表非空 → 400
- `parse_flow_graph(raw) -> FlowGraphDoc`：**宽容**，运行时装配用；坏 JSON/坏版本/单项坏
  → 逐项跳过，整体坏 → 空图（永不抛错，绝不炸通话）

## 4. 运行时语义

### 4.1 插点与每轮判定顺序

`agent.py` `on_user_turn_completed` 现有序（回声守卫 → 风暴/starve → WA/数字累积 →
WA 侦测 → 事实沉淀 → flow 规则块 → DEFER → say 直念步 → QA 快路 → LLM）中，
**graph 块插在 say 直念步快路之后、QA 快路之前**：

```
…（不变：守卫/累积/侦测/规则块/DEFER）
say 直念步快路        ← say-lock 铁律不被图抢（合规直念内容优先）
▶ graph 块（新）：
    BOK_FLOW_GRAPH=1 且图非空 → pick_graph_action(user_text, step_1based=current+1, fired)
    ├─ jump_step：target≠current 才动（防环；no-op 不消耗 once）
    │    jump_to() + _invalidate_stale_preemptive("流程跳转 → 第 N 步")
    │    + context_state.set_flow_current() → 本轮继续回答（新步指引）
    │    assistant 轮 provider="graph-jump"
    ├─ play_qa：QaIndex.by_id 在场且音频已物化 → _qa_canned_say() → StopResponse
    │    assistant 轮 gen="qa_fastpath" / provider="graph-play"
    │    缺条目/缺音频 → 放行 LLM（日志 FLOW_GRAPH play_miss，不消耗 once）
    └─ 无命中 → 原路径零变化
QA 快路（不变；jump 轮因 current≠_flow_step_before 天然被 advanced 闸旁路）
LLM（不变）
```

precedence 总结：**REFUSE 收线 > DEFER > say 直念 > graph > QA 快路 > 自由 LLM**。
理由：守卫与收线是安全语义；DEFER 是「我先查一下」的既定应承；say 直念步是合规内容
（通知/赔偿承诺），被图抢=合规内容被吞（call-73f13391 教训）；graph 绑定是运营显式配置，
比 QA 快路的语料级匹配更具体。

### 4.2 jump 的副作用包（与正常推进逐字节同构）

`FlowController` 新增 `jump_to(idx)`（镜像 `advance()`：钳制 `[0, len(steps)]`、
`closing` 时 no-op、置 `_just_advanced=True`）。agent 侧调用三件套与规则推进同款：

```python
flow_ctrl.jump_to(target)
_invalidate_stale_preemptive(f"流程跳转 → 第 {flow_ctrl.current + 1} 步")
context_state.set_flow_current(flow_ctrl.current_step_text())
```

下游自动继承：`【新一步】`注入、该步底稿首轮重渲染、尾部 revision 重渲染、目标步是
say 步则下一轮走直念快路、抢跑快照作废。

### 4.3 账本与防环

- `FlowController.graph_fired: set[str]`：**动作成功执行才记账**（jump 实际位移 /
  play_qa 实际播出）；once 判定在 `pick_graph_action` 过滤
- 防环双保险：jump 只在 `target != current` 时动（跳当前步=no-op 不记账）；
  `once=true` 供运营对强跳转加硬上限
- 日志：`FLOW_GRAPH jump|play|play_miss|jump_noop binding=<id> …`
- turns 账本：jump 轮 assistant 行 `provider="graph-jump"`；play 轮复用 QA 快路的
  `gen="qa_fastpath"`（列 VARCHAR(16)，不新增 gen 值）+ `provider="graph-play"`

### 4.4 kill-switch 与装配

- `BOK_FLOW_GRAPH`（默认 `"1"`）：整闸，读法与全仓同款 `os.environ.get(...) == "1"`
- 装配：`FlowController.from_template` 解析 `template["graph_json"]`（宽容路径），
  图对象随 per-call `flow_ctrl` 存活，模板快照语义天然获得
- 空 schema 兼容：无 `graph_json` 列前旧库 → `_ensure_column` 幂等补列默认空串 → 零变化

## 5. 数据与迁移

- `packages/business-db/bok_voice_business_db/models.py`：`ConversationTemplate.graph_json`
  （Text, default ""），带注释
- 迁移唯一落点：`apps/control-plane/control_plane/deps.py` `build_engine()` 幂等段
  `_ensure_column(conn, "conversation_templates", "graph_json", "graph_json TEXT DEFAULT ''")`
- SQL 方言可移植（`tests/test_db_portability.py` 门禁）：TEXT+DEFAULT'' 全方言安全
- Supabase 侧：改表后重跑 `scripts/dump_postgres_ddl.py` 再应用（CI 同款流程）

## 6. CP API

**不加新端点**——复用 `POST /api/templates` 与 `PUT /api/templates/{id}`：

- `TemplateRequest` / `UpdateTemplateRequest` 增 `graph_json: str = ""`
- 保存时非空即 `validate_flow_graph`，错误列表非空 → `400 {"error": "invalid_graph_json",
  "detail": [...前5条]}`；空串=清空图（合法）
- PUT 沿用 `exclude_unset` 语义（只写显式出现的键）；版本快照自动涵盖
- 审计：`template.create/update` detail 增 `graph_saved` 布尔
- 权限零新增：模板编辑权沿用 B3（user 只改自己的；共享=admin/root）

## 7. Web 画布

- `lib/qa-canvas.ts`：`parseGraphDoc`（宽容）+ `deriveGraph` 增第三类节点与第二类自有边：
  - 意图节点：id `intent:<id>`、type `intent`、x=`INTENT_X=-280`（脊柱左侧、全程通用
    泳道 -560 之右），y 锚定首个生效步（全程锚第 1 步），同锚点按 `INTENT_STACK_Y=140`
    纵向堆叠；禁用意图照渲染（视图置灰）
  - 绑定边：id `bind:<id>`、kind `binding`、虚线琥珀色、label=意图 label（退首关键词）；
    `play_qa`→QA 条目节点（id=条目 id），`jump_step`→`step:<n-1>`；**悬空引用不画边**
  - 位置持久化沿用 `LOCAL_POS_KEY`（intent 节点 id 稳定，同一张 localStorage 表）
- `components/qa-canvas-view.tsx`：
  - `nodeTypes.intent`：标签+关键词数徽标+禁用置灰；选中态高亮
  - 调色盘：左下浮动「＋ 意图」按钮 → `screenToFlowPosition` 视口中心落节点 → 打开编辑器
  - 点选意图节点/绑定边 → 打开编辑器；Delete 删意图连带其绑定（与簇/步骤边删除语义互不干扰：
    spine 不可删不变；binding 边删除=从图移除该绑定）
- `app/(app)/qa/page.tsx`：意图编辑器模态（标签/关键词逗号分隔/生效步骤 chips/绑定列表
  （动作下拉+QA 条目选择器或步号下拉+优先级+once+enabled））；保存=组装全量 doc 序列化
  → `updateTemplate(id, {graph_json})`（PUT exclude_unset 单字段直改）；乐观更新+失败回滚；
  `canEditTemplate = me.role !== "user" || template.owner_user_id === me.user_id`（B3 口径），
  无编辑权隐藏调色盘、编辑器只读

## 8. 观测与探针

- agent.log：`FLOW_GRAPH` 前缀四打点（jump/play/play_miss/jump_noop）
- 探针 `scripts/probe_flow_graph.py`（骨架照 `probe_offscript_soak.py`：真栈+TTS 推流）：
  建带图模板（投诉→jump 第 4 步；退款→play_qa）→ 推触发语句/非触发语句 → 断言：
  ①触发轮 `FLOW_GRAPH jump` 日志 + 后续 assistant 轮 `template_step=4, provider=graph-jump`
  ②非触发轮零 `FLOW_GRAPH` 行 ③`BOK_FLOW_GRAPH=0` 重跑全零（kill-switch 档）。
  play_qa 断言为信息位（依赖罐头物化，miss 有明确降级路径）
- 回归：无图模板三语 E2E + barge-in + edge_cases 全绿 = 「零变化」承诺的验收线

## 9. 任务指引

实现计划见 [../plans/2026-09-18-qa-flow-graph-phase2.md](../plans/2026-09-18-qa-flow-graph-phase2.md)
（9 任务 TDD，SDD 执行）。

## 10. 风险与回退

| 风险 | 缓解 |
| --- | --- |
| 插线破坏既有管线 | 插点在快路族末尾、纯增量；无图模板走原路径逐字节不变；三套 E2E 回归 |
| jump 与抢跑/尾部渲染打架 | 复用规则推进同款三件套（`_invalidate_stale_preemptive`+`set_flow_current`），语义与 2026-09-06 修订后的推进完全一致 |
| 运营配出环/死跳 | target==current no-op；`closing` 冻结；运行时钳制步号；once 硬上限 |
| 悬空 qa_id | 保存不校验（owner 可见性），运行时 miss 放行 LLM + 日志 |
| 关键词误触发 | 确定性子串+账本可追溯（provider 打点）；误配即改图，零代码发布 |
| graph_json 超大拖慢装配 | 64KB 上限 + 保存时校验；装配每通只 parse 一次 |
