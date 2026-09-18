# Flow Graph Phase 3 设计：命中语义四连（优先级 / 轮换 / 追问链 / 意图引擎）

- 日期：2026-09-18
- 状态：规划完成，待逐项立项执行（一次一项、一项一探针——AGENTS.md 2026-09-17 spec §12 路线约束）
- 前置：[2026-09-17-qa-canvas-design.md](2026-09-17-qa-canvas-design.md)（Phase 1 画布）、[2026-09-18-qa-flow-graph.md](2026-09-18-qa-flow-graph.md)（Phase 2 图执行引擎，已合 main d9b8836）
- 执行载体：每增量一份 plan + SDD；本 spec 只锁契约与边界

## 0. 现状基线（2026-09-18 侦察实证）

- `QaIndex.match`（qa_gate.py）：纯分数最大者胜，`score > best_score` 平分吃**插入序**（=repo `order_by(created_at)`，最老先）。无任何优先级字段。
- `qa_entries` 无 priority/weight 列；`hit_count` 纯分析位不进排序。
- **`cluster_head_id` 在 agent runtime 零消费**：变体今天是裸索引竞争者——变体可能压过 head 命中（Phase 1 只做了管理面，运行时语义欠账，正是 3.2 要收的口）。
- judge 专线在场：`_background_flow_judge`（3s 让路 → `FLOW_JUDGE_*` → :1237 9B → `build_judge_messages`/`parse_judge_output` → 换步守卫 `flow_ctrl.current == step_at`）。
- `scripts/probe_qa_hit.py` 在场：离线 A/B（真实转写 vs 现库，直连 `QaIndex.match`）——优先级增量的对照台。

## 1. 3.1 匹配优先级（本期先行动，plan: 2026-09-18-qa-priority-phase31.md）

**语义**：阈值过关的候选里，`priority` 小者先；同优先级按分数降序；再平吃 created_at（=现状）。

- `qa_entries.priority INT NOT NULL DEFAULT 10`，取值 [0,1000]，默认与 graph bindings 的 `DEFAULT_PRIORITY=10` 同约定（小者先）
- **零变化承诺**：全默认优先级时胜者与今逐字节同（含平分→最老）；kill-switch `BOK_QA_PRIORITY=0` 回纯分数档
- 运营含义：「这条必须赢过那条」变成显式数据，不再靠改措辞拼分数
- 探针：`probe_qa_hit.py --priority-duel`（合成对偶条目 A/B 胜者对照 + kill 腿）

## 2. 3.2 多答案轮换（簇语义收口）

**语义**：变体不再是裸竞争者；命中落在簇上，簇内轮换取「本通最少播放」的成员。

- 索引装配：变体（`cluster_head_id` 非空且 head 在场可用）折进 head 的候选组，**折组不改分：返回代表条目时携带胜者原分数（组分口径仅诊断日志消费）**；孤儿变体（head 删/禁）退独立条目（优雅降级）
- 轮换账本：`FlowController.qa_played: list[str]`（本通播放序，容量截断），播放成功才记（与 graph_fired 同纪律）；取组内 `qa_played` 计数最小者，平则 created_at 序
- 物化面：pregen 本就遍历全部条目——簇成员照常物化，零新增管线
- 探针：同一问法同通话连问 N 次 → 断言答案去重 ≥2

## 3. 3.3 追问链（答后跳转）

**语义**：`graph_json` 的 play_qa 绑定扩可选 `then_jump`（1-based 步号）——罐头播完当场 `jump_to`，下一轮按新步走。

- 形状：`binding.then_jump?: int`——流程逻辑全留 graph_json 单源，**不加 qa_entries 列**（否决项：链逻辑散进条目表会跟画布分家）
- 执行点：`_qa_canned_say` 播放成功返回后（graph-play 分支内）同步 jump——复用 Phase 2 的位移三件套与记账纪律（实际位移才动账本；closing 冻结；越界钳制）
- probe_flow_graph 增腿：play 轮后下一轮 `template_step == then_jump`
- validate：`then_jump` 仅 play_qa 绑定合法、范围 [1,999]

## 4. 3.4 意图引擎（judge 判据上图，独立立项）

**语义**：`GraphIntent` 扩可选 `judge: {"prompt": "..."}`——关键词未中时，背景 judge 按意图判据评估用户轮，命中则下一轮生效触发绑定。

- 复用 `_background_flow_judge` 管线形态（让路 delay + `FLOW_JUDGE_*` 9B 专线 + 换步守卫思路）；确定性关键词恒同步先行，judge 意图只补模糊轮
- 单次调用批量评估本通全部 judge 意图（多选一输出，防 N 次调用烧专线）
- 判据上画布=意图编辑器加「判据」文本域（仅 admin 语义提示：判据即 prompt 片段，写清正反例）
- kill-switch `BOK_FLOW_GRAPH_JUDGE=0`；探针腿：同话语 keyword 腿 vs judge 腿 A/B
- 最大件——必须等 3.1-3.3 落地后的真实话术反馈再立项（路线图原约束）

**状态：已实施并验收（2026-09-19，用户拍板开闸）**。实施偏离 spec 的三处定案：
①「同话语 A/B」落地为「同图同目标步、keyword 腿直中 vs judge 腿下一轮中」两条腿各跑
（同话语无法既含关键词又不含关键词）；②消费语义钉死 single-shot TTL（pending 先取即清，
无资格绑定=过期不重试）；③调度钉死 `elif user_text:`（空转写轮不进判定）。落地件：
`flow_graph.py`（`judge_prompt`/校验/`pick_graph_action(judge_hit=)`/`eligible_judge_intents`）
+ `flow.py`（`build_intent_judge_messages`/`parse_intent_judge_output`）+ `agent.py`
（六闸调度/背景批量判定/store 守卫/TTL 消费）+ 画布判据文本域 + 探针
`--intent-judge` 腿。审查修掉 F1（调度漏空轮）/F5（消费位 kill 配对）/N9（9B 调用
timeout=20+措辞禁序号+parse 剥中文标点）。证据（2026-09-19 实弹，reports/flow-graph/）：
pytest 1606 全绿；`--intent-judge` 正腿 4/4（judge_scheduled+judge_hit+judge_effective，
call-b24ab5d0/1789752590——fuzzy 话语被 ASR 劈两段，**第二段当场消费 pending 触发跳步**，
劈轮鲁棒性顺带实证）；keyword A/B 腿 4/4（1789752688，同图同目标步同步直中零回归）；
`--intent-judge --expect-off` 4/4（1789752836，`BOK_FLOW_GRAPH_JUDGE=0` 经
`ps -wwE` 实证到 worker 进程 env）。

## 5. 非目标与既挂账

- Phase 2 遗留 I3（跳步话面：4B 总览引力）——prompt 域专项，不属本四连；运营启用 graph 前实测
- `_FORWARD_ENV` 立法（45 BOK_* prod 透传面）——独立卫生项
- hit_count 参与排序——否决（分析位与控制位分离；想要权重就写 priority）

## 6. 验收公约（每增量）

1. 零回归：全默认配置行为与今逐字节同（单测钉死）+ 三语 E2E
2. kill-switch 成对回退（语义变更 + 开关各自独立）
3. 探针 A/B 真数据过线才翻默认
4. Supabase 列随增量 MCP 直连 apply（Phase 2 已立先例）
