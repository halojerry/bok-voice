# Plan: 3.4 意图引擎（judge 判据上图）— Phase 3 收官件

Spec: `docs/superpowers/specs/2026-09-18-flow-graph-phase3.md` §4。
路线图原约束「等 3.1-3.3 实战反馈再立项」——用户 2026-09-19 拍板继续执行，门已开。

## 0. 语义定案（实施前钉死，实施中不变）

- `GraphIntent` 扩可选 `judge: {"prompt": "..."}`（JSON 形态）；模型层落 `FlowIntent.judge_prompt: str`（空串=无判据，缺省零变化）。
- **keywords 仍是必填非空**（validate 规则不动）——judge 是模糊轮补充，不是关键词替代。确定性关键词恒同步先行；judge 只在关键词未中（`pick_graph_action` 返回 None）时补位。
- **背景管线形态复用 `_background_flow_judge`**：让路 delay（同 `FLOW_JUDGE_DELAY`）+ `FLOW_JUDGE_*` :1237 9B 专线 + 让位守卫（store 时仍在本步 + 非 paused）。
- **单次批量调用**：一次 LLM call 评估本通全部「在 scope 且有可触发绑定」的 judge 意图，多选一输出（一个 intent id 或 NONE）。绝不做 N 次。
- **命中下一轮生效**：judge 完成 → `_gjudge_pending["intent"] = id`；下一次图引擎求值（下一轮 `on_user_turn_completed` 的图块）读取即**清**（single-shot TTL）——无论绑定是否真触发（无资格绑定=过期作废，打日志）。消费时 `pick_graph_action` 对该 id 重新过 enabled/step-scope/绑定资格（judge 结果不豁免任何守卫）。
- kill-switch `BOK_FLOW_GRAPH_JUDGE=0`（缺省 "1"——无 judge 数据=零调用=零变化，功能随数据 opt-in；探针真数据过线是合并前置）。
- 零 DB 变更：judge 走 `graph_json` 文本列（模板字段），CP `validate_flow_graph` 单点加严。无 Supabase migration。

## 1. 接口冻结（T1/T2 并行契约）

JSON：`intents[].judge = {"prompt": string}`，prompt 1..400 字（`JUDGE_PROMPT_MAX_CHARS=400`）。
validate（严格，CP 保存拒绝）：`judge` 在场必须是 dict 且 `judge.prompt` 是 1..400 字字符串；其余错误列表契约不变。
parse（宽容，agent 消费）：坏形状→丢字段（`judge_prompt=""`，Phase 2/3.3 行为），绝不拒整项。
`pick_graph_action(doc, user_text, *, step_1based, fired, judge_hit: str | None = None)`——`judge_hit` 与关键词命中同权入 `hit_ids`（仍受 enabled/步 scope/绑定资格全守卫）。
agent flow.py 新纯函数：`build_intent_judge_messages(...)` + `parse_intent_judge_output(text, valid_ids) -> str`（输出契约=单个 id 或 NONE；剥空白/反引号后精确匹配，认不出=""）。
`_llm_judge(base_url, model, messages, *, max_tokens=8)`——加具名参数缺省 8 零变化；意图 judge 调用传 32（id 可能长于 8 token）。
env：`BOK_FLOW_GRAPH_JUDGE` 入 `tools/bok.py` `_BOK_PASSTHROUGH_KEYS`（dev+prod 双面）。
日志族：`FLOW_GRAPH judge_scheduled intents=N step=S` / `judge_hit intent=X` / `judge_miss` / `judge_pending_fired binding=B step=S` / `judge_pending_expired intent=X` / `judge_skipped reason=...`。

## 2. Tasks

### T1 core+runtime（intent-judge-phase34 主工作树）
1. `packages/core/bok_voice_core/flow_graph.py`：`FlowIntent.judge_prompt` + `_parse_intent` 宽容解析 + `validate_flow_graph` 严格规则 + `pick_graph_action` `judge_hit` 参数。
2. `apps/agent/agent_runtime/flow.py`：`build_intent_judge_messages`（标准书面中文模板，判据片段+用户轮+当前步，单选输出契约）+ `parse_intent_judge_output`。
3. `apps/agent/agent_runtime/agent.py`：图块 `_gbinding is None` 分支挂 `_maybe_schedule_intent_judge`（资格预筛：enabled+judge_prompt 非空+步 scope+存在可触发绑定+单飞+pending 空+env 开）；`_background_intent_judge`（delay→批量调用→守卫→store pending）；消费点在图块 pick 前 pop+清。`_llm_judge` 加 max_tokens 参数。
4. `tools/bok.py` passthrough key。
5. 测试：`tests/test_flow_graph_judge.py`（模型/校验/宽容/judge_hit 语义）+ runtime 纯函数与接线测试 + `tests/test_bok_worker_env.py` 参数表加键。全默认档零变化断言（无 judge 数据=行为逐字节同）。
6. `python -m compileall -q` + 全套 pytest（主树 .venv312，conftest 路径哨兵）。

### T2 web（并行工作树 intent-judge-t2，基 ce8dd15）
1. `apps/web/lib/qa-canvas.ts`：`GraphIntent.judge?: { prompt: string }` + 草稿字段 + **唯一保存路径投影**（意图确认整图 PUT 的构造点折入 judge）。
2. `apps/web/app/(app)/qa/page.tsx`：意图编辑器加「判据」多行文本域；hint=「判据即 prompt 片段：写清什么算命中、什么不算（正反例）。留空=仅关键词确定性命中。」（admin 语义提示）。空判据不落 judge 键（字节级同旧 JSON）。
3. `npx tsc --noEmit && npm run build` 绿。

### T3 探针+验收+文档（T1/T2 合回后，主工作树）
1. `scripts/probe_flow_graph.py` 加 `--intent-judge` 腿：judge-only 意图（关键词不在话语中）→ 模糊话语轮 → 断言 `judge_hit` + 下一轮 `judge_pending_fired`（同轮 via 日志 ∨ after-round template_step 双证同 3.3 口径）；A/B：keyword 腿同话语直中 vs judge 腿下一轮中，报两腿时序。
2. 真栈验收（serve + 双腿 PASS + `BOK_FLOW_GRAPH_JUDGE=0` 反腿 PASS）。殭尸 worker 检查 + 8000 端口 Docker 让位照旧。
3. 文档 truthing：probe docstring、AGENTS.md 话术图条目补 3.4 句、spec §4 状态翻牌（已实施+证据）。
4. PR + CI 全绿 + 合 main。

## 3. 验收（spec §6 公约）
1. 零回归：无 judge 数据全默认=行为与 ce8dd15 逐字节同（单测钉死）。
2. kill-switch 成对：`BOK_FLOW_GRAPH_JUDGE=0` 关语义层（调度+消费都不发生）。
3. 探针 A/B 真数据过线才可翻默认合并。
4. 无 DB 迁移（本期不适用公约 #4）。

## 勘误
1. **（T1 review F1）调度 else 与图块平级**：空转写轮（user_text 为空令图块整体跳过）
   仍会漏进调度——白烧一次 9B 之外，挂上的 pending 在下一轮无话语支撑地触发绑定。
   修=调度钉死 `elif user_text:`；接线测试 test_intent_judge_wiring.py 源级锚钉死。
2. **（T1 review F5）消费位缺 kill 配对**：=0 时 pending 必须照清（TTL 不悬挂）但不再
   喂 judge_hit——中程翻闸无半开态。
3. **（T1 review N9）`_llm_judge` timeout=5 硬码对 9B 判据集是静默永久 miss**：非流式
   总预算 5s、9B prefill ~0.6k tok/s，中型候选集必超时。修=timeout 具名参数（缺省 5.0
   零变化），judge 调用传 20；同批修 prompt「编号」措辞（诱导回序号=parse 恒 miss）
   与 parse 不剥句尾中文标点。
4. **（T3 实弹）探针 fuzzy 窗用错解析器**：judge_scheduled 落 fuzzy 窗（字节偏移实证
   15107513 ∈ window0），但窗解析只用图四词正则 `parse_graph_events`=结构性捞不到、
   judge_scheduled 恒 FAIL 假阴。修=fuzzy 窗叠加 `parse_judge_events`。
5. **（T3 实弹）erc.fetch_turns 裸 httpx 在 auth-on 栈 401**：`{"detail":…}` dict 令
   `graph_turn_rows` 的 `t.get` AttributeError 炸腿。修=探针内 `fetch_turns_authed`
   （带 `_CP_HEADERS`、非列表响应当零行；未设 token 逐字节同 erc 裸跑）。e2e-auth
   未并分支（fix/e2e-trilingual-auth）同病，各修各的。
6. **（T3 环境）worktree 起真栈需三链接**：`runtime`（sidecar 打包 python/livekit）、
   `services/realtime-translation/node_modules`（bline）符号链接主树——bok.py 的
   sidecar_python/write_bline_config 都按 ROOT 相对路径解析，worktree 缺未跟踪目录即
   serve exit 2/1。非代码问题，验收跑法记档。
7. **（T3 实弹）fuzzy 话语被 ASR 劈两段不破坏腿**：首段调度+判定、次段消费 pending
   触发跳步（turn 行 template_step=4）——judge 消费语义对劈轮鲁棒，顺带实证。
8. **（T2 review I1/N1-N4）**：web 接线守卫的 JUDGE_PROMPT_MAX_CHARS 裸 token 断言被
   import 行喂饱（恒绿）→ 钉比较本体+报错文案双锚；judge: 唯一投影 count 锚；两处
   注释 truthing（parseGraphDoc 不消毒坏形状/常量手工同步）；表头文案补判据语义。
