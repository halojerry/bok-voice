# A 线漏斗 v2 设计（Funnel v2）

- 日期：2026-09-18
- 状态：已实施并验收（P0+P1 全量落地；P2 脊柱收编待后续周期）。验收证据：三套 E2E 全绿（trilingual-canto PASS/barge-in PASS/edge 8/8）、offscript-soak 50 轮哑 1（历史基线哑 9、watchdog 15→0）、latency-soak PASS（哑 0 零哨兵）、实弹全链路取证 judge route=register_followup conf=0.70→CP 工单落单（fu:8e226d9b16e0）→stall-ladder degrade 接管（agent.log call-a8a1c354）。
- 证据锚点：call-af30d9de（2026-09-17 13:43 赔偿外呼）、call-91a6b8c9（2026-09-17 08:32 入线查单）；诊断过程见当日 agent.log 与 turns 账本
- 关联：`docs/S2S_ROADMAP.md`（不换架构）、AGENTS.md「官方组件优先三档判据」

## 1. 背景与证据

现有体系已是事实上的多层漏斗（规则直念 / QA fastpath / 4B LLM + 兜底），但对两通真实通话的诊断表明失败不发生在「层」上，发生在三个具体缺口：

1. **卡步无升级器**（call-af30d9de）：step3 平台问句被重复 4 遍。`judge(bg)=unclear` 连续 7 轮，flow 账本没有「同 step 连续 unclear」概念；`REPEAT_SELF_SUPPRESSED` 只剥逐字重复句，改写句照发；客户「你还没答出电话呀？」→「搞算了，唔使搞啦」挂断。
2. **工具层缺位**（call-91a6b8c9）：客户 13 轮独白只换来 1 条被截断的 LLM 回复（「收到，單號三七七」——系统无查单能力，收完单号零后续动作）+ 5 条 filler + watchdog ack。风暴静听误伤合法独白部分已于 2026-09-17 修复（cap + starve-ack）。
3. **模糊轮无路由出口**：rule_verdict 与 judge 都只会说 unclear，没有「该换策略/该登记/该转人工」的结构化建议，导致修复只能往规则层加正则（规则黑洞）。

上游 ASR garble（「当我们满屋欠人钱」级）不在本设计范围——治本在 ASR 端（云端 Qwen3-ASR-Flash 试点独立推进）。

## 2. 目标 / 非目标

**目标**
- 卡步有出口（stall 升级阶梯）
- 查单/跟进/投诉类请求有真动作（登记工单 + 诚实降级话术）
- 模糊轮有结构化路由决策（judge 出 confidence + suggested_action）
- 门控次序显式化（TurnRouter 脊柱收编）

**非目标**
- 不新增任何模型依赖（无 embedding router、无领域分类器——证据不支持，见下）
- 不动 ASR 端、不动 B 线、不动 S2S 路线
- v1 不让 4B 自主发起 function calling（触发权归 router；LLM-native tools 待云端 ASR 改善输入质量或换更强 LLM 后再评估）
- 不加规则层正则（规则层冻结令：新失效场景优先喂 judge action / 升级器 / 工具层）

否决 embedding router 的依据：碎裂粤语转写喂 embedding 同样失效（GIGO），且是唯一会新增关键路径/模型依赖的候选。两通坏通话的死因它一个都治不了。

## 3. 组件设计

### 3.1 Stall 升级器（P0）

**账本**：`FlowController`（flow.py:841 dataclass，与 `said_steps` 同层）新增 `step_streak: dict[int, int]`。rule_verdict 与 background judge 双路计数，**按 (step, turn) 去重——同一轮两路都判 unclear 只计 1**（judge 迟到返回时若该轮已计过则跳过）；判定 UNCLEAR 且步未变 → +1；进 ADVANCE/CONFIRM/REFUSE/FAREWELL/DEFER 或步切换 → 清零。纯函数 `_stall_ladder(streak: int) -> str | None` 离线可测。

**阶梯**（触发当轮 agent 以 `_say_script` 直念，跳过 LLM，零 TTFT）：

| streak | 动作 | 材料 |
|---|---|---|
| ==3 | 降级问法 | 优先：模板步可选 `degrade_hint` 字段（web 话术编辑器可选输入，P1 后补 UI）；缺省：通用封闭问句引导脚本（三语各一条，静态资产） |
| ==5 | 绕过留号 | 直念「不如留个 WhatsApp，我哋专人帮你跟进」→ 客户报号后走既有 WA 捕获 machinery（`_wa_numberish`/digit-accumulate 全步生效已具备），capture 成功 → 既有复述确认 + 收线阶梯 |
| ==8 | 主动收线 | 直念收线语（复用 `_farewell_line` 家族），结束通话 |

- say 直念后当轮不再生成 LLM 回复（与 defer-ack 同门槛同路径）；升级行写审计/打点 `[stall-ladder] step=N level=degrade|bypass|close`。
- 步切换/捕获成功/客户实质回应 → 清零重计。
- kill-switch：`BOK_STALL_LADDER=0` 全关；阈值 env 可调（`BOK_STALL_LADDER_N` 系列，默认 3/5/8）。

**依据**：af30d9de 若有此阶梯，第 3 轮 unclear 即换封闭问法，不会滑向客户放弃。

### 3.2 Judge 路由决策字段（P0）

`flow.build_judge_messages` 的 prompt 契约从单一 verdict 扩展为：

```
verdict: advance|confirm|unclear|objection|question|repeat|defer|refuse|farewell   （不变）
route: keep|degrade_question|capture_contact|register_followup|transfer_human
confidence: 0.0-1.0
```

- `parse_judge_output` 防御式解析：route/confidence 缺失或非法 → 回落 `route=keep`，行为与现状逐字节一致。
- 消费方：3.1 升级器（`route=degrade_question` 可提前触发降级，不受计数门槛硬卡）；3.3 工具层（`route=register_followup` 触发建单）；`transfer_human` 走既有主管转接语义。
- judge 仍后台 fire-and-forget（`_background_flow_judge` agent.py:2952、`FLOW_JUDGE_DELAY=3`），**不进关键路径**；路由动作在其返回后执行，确认语用 `session.say()` 直念（WA 复述确认同模式）。
- kill-switch：`BOK_ROUTE_JUDGE=0` 回旧 prompt（只解析 verdict）。

### 3.3 工具层最小版：登记工单（P1）

**触发**：router 层命中「查单/跟进/投诉」意图——来源 = judge `route=register_followup`（主）或规则显式命中（辅，如「我要投诉」短句族，白名单 ≤5 条，不再增长）。

**动作**：agent 异步调 CP 新端点 `POST /api/calls/{id}/followups`（机器通道鉴权）→ 新表 `call_followups`（id/call_id/object_id/account_id/kind/note/status=open/created_at；deps `build_engine()` 幂等补表，方言可移植过 `test_db_portability`）→ 审计 `followup.create`。

**话术**：`_say_script` 诚实回复（三语罐头）：「我帮你登记咗跟进，专人 24 小时内回复你」+ 已知事实复述（单号/诉求）。**不装查**：无订单数据源，v1 只有「登记+人工跟进」一档；将来接入真实订单 API 时插入同一 action 槽位换真状态。

**可见面**：v1 落库+审计即可（主管报表页读取留 P2/后续）；不阻塞 agent 侧价值。`kind ∈ {track_order, complaint, followup}`。

- 并发防叠：同通话同 kind 已有 open 工单 → 幂等返回不重发确认语。
- kill-switch：`BOK_TOOLS_FOLLOWUP=0` 关（降级为纯话术直念、不调 CP）。

### 3.4 TurnRouter 脊柱收编（P2，结构重构）

现有门控散在 agent.py / flow.py / livekit_plugins 三处，次序靠惯例。收编为单一显式漏斗模块 `agent_runtime/router.py`，每级返回统一决策对象：

```
RouteDecision(action: str, verdict: str, confidence: float, reason: str)
```

| 层 | 内容（既有逻辑搬家，不改行为） |
|---|---|
| L0 输入门 | 回声守卫 / 犹豫残片门 / garble 门 |
| L1 规则层 | REFUSE/FAREWELL/DEFER/REPEAT/WhatsApp detect/数字累积/say 直念门 |
| L2 检索层 | QA fastpath |
| L3 推进层 | rule_verdict + should_auto_advance + 分支注入 + stall 升级器 |
| L4 裁决层 | 9B judge（含 route 字段） |
| L5 生成层 | 4B LLM + `_LlmFallbackStream` 兜底 + watchdog |

- **纯搬家 + 换契约**：1162 条 pytest 全绿是门禁；storm backoff、垫话、fallback 壳等调度类组件**不搬家**（它们是兜底不是路由）。
- agent.py 消费 RouteDecision 取代散点调用；`BOK_TURN_ROUTER=0` 保留旧调用路径一个版本后删除。
- 每级独立可测：各级函数接受 `(ctx, turn)` 返回决策，不直接 I/O。

## 4. 延迟不变量（设计约束）

1. **零关键路径新增**：L0-L3 全是 regex/检索/状态机（µs-ms 级）；L4 是已在跑的后台进程；进关键路径的模型调用只有既有 4B，与今天完全一致。
2. 漏斗每抢走一个轮次 = 负延迟贡献（直念 0ms TTFT / QA fastpath ~50ms vs LLM 2-3.8s）。
3. 工具调用（CP 建单）异步执行，不挡首音频；确认语排主回复后出声。
4. 现有延迟武器（预热/前缀缓存/尾部瘦身/prefill 抢跑/垫话/2.0s 兜底闸/4s watchdog）一律不动——漏斗管「谁说话」，链路管「多快说」。
5. 验收含 `probe_latency_soak.py` 零回归断言。

## 5. Kill-switches 汇总

下表为**终态默认**；合入初版一律置 0，探针验收过后才逐个翻 1。

| env | 控制范围 | 默认（终态） |
|---|---|---|
| `BOK_STALL_LADDER` | 3.1 升级器全闸 | 1 |
| `BOK_STALL_LADDER_*`（阈值组） | 3/5/8 阈值 | 3/5/8 |
| `BOK_ROUTE_JUDGE` | 3.2 judge route 字段 | 1 |
| `BOK_TOOLS_FOLLOWUP` | 3.3 CP 建单动作 | 1（关=纯话术） |
| `BOK_TURN_ROUTER` | 3.4 脊柱收编 | 1 |

## 6. 测试与验收

**单测**（先于实现，TDD）：
- `_stall_ladder` 纯函数：阈值边界/清零语义/双路计数
- `parse_judge_output`：字段缺失/非法/旧格式回落
- RouteDecision 契约与各级函数无 I/O
- followups 端点：RBAC（机器通道/角色闸）/幂等防叠/方言可移植

**行为验收**：
- `probe_offscript_soak.py` A/B（主判据：同 step 重复问句次数下降、哑轮数、give-up 后无收线数=0、followup 触发率）
- 三语 E2E（trilingual/barge-in/edge_cases）全绿
- `probe_latency_soak.py`：p50/p95 与预算超标计数零回归
- `probe_reply_quality.py` 三断言无回归

**发布节奏**：各组件 kill-switch 默认关合入 → 探针过 → 默认开（沿用仓库惯例）。

## 7. 分期与风险

| 期 | 内容 | 风险 |
|---|---|---|
| P0 | 3.1 升级器 + 3.2 judge route | 低：纯增量，回退开关齐 |
| P1 | 3.3 工单端点+动作 | 低-中：新端点过 RBAC/门禁；「投诉」白名单误触发靠 judge confidence 兜底 |
| P2 | 3.4 脊柱收编 | 中：大面积搬家——放最后，让 P0/P1 先在旧结构里验证正确，避免「重构+改行为」搅在一起无法归因；门禁=全量 pytest+E2E |

**已知残留（本设计不治，记录归属）**：ASR garble（云端 Flash 试点）；转写碎裂轮规则激活条件不被激活（AGENTS.md 已记，治本同前）。

## 8. 未决

- 模板 `degrade_hint` 的 web 编辑器入口（P1 顺手做，无字段时走通用脚本，不阻塞 P0）
- 主管报表页展示 followups（P2/后续）
- 真实订单数据源接入（外部依赖，未有时 v1 话术即终态）
