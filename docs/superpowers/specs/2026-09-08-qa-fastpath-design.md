# Q→A 检索快路 设计（PR-3）

日期：2026-09-08。状态：已批准。依赖 PR-1（TtsAudioCache 存应答音频）。配套：`2026-09-08-tts-cache-design.md`。

## 目标

用户说某句话 → 语义/词法命中预置问答对 → 跳过 LLM 直接播预生成回答。命中轮延迟 ~1.5-2s → ~50ms。数据源与「话术蒸馏/留存」同源：从 turns 表挖高频「用户轮→紧随 agent 回复」配对 + 运营精选 FAQ。

## 定位与安全（行为变更，四道闸）

这是 LLM 前置的检索闸门，插入点：`on_user_turn_completed` 流程推进块之后、paused 检查之前。命中 → 压掉本轮 LLM、播罐头回答；未命中 → 照旧走 LLM。**核心风险=上下文盲配**（同一句「好的」在平台确认步与 WhatsApp 步该接的话完全不同），四道闸兜底：

1. **条目作用域**：`scope=global`（步不变社交轮）或 `scope=step`（仅限第 N 步）；不在作用域不命中。
2. **关键信号强制旁路**：用户轮含 WhatsApp 信号（`detect_whatsapp_signal`）、数字串（`_valid_digit_runs`）、拒绝词（`_REFUSE_RE`）、当前 WA 步未 captured、closing 态 → 一律交回 LLM+flow。全部复用 flow.py 现成纯函数，不另造判定。
3. **让位规则**：推进轮（auto_advance 已触发）、背景 judge 在途、verdict ∈ {REFUSE, OBJECTION} 让位；QUESTION 仅限 FAQ 条目命中时放行（judge 照常后台跑，只动状态不发话）。
4. **开关与阈值**：`BOK_QA_FASTPATH=0` 一键全关；`BOK_QA_MATCH_THRESHOLD` 默认 0.90，宁缺毋滥。

## 数据层

- `qa_entries` ORM（packages/business-db models.py，create_all 自动建表，无迁移）：`id`、`question_text`、`answer_text`、`lang`（zh/cantonese/en）、`scope`（global|step）、`step_index`、`voice_id`、`enabled`、`hit_count`、`source`（mined|curated）、`account_id`、`template_id`、`created_at`。
- repository 新增：QA CRUD、`incr_qa_hit`、跨通话配对查询（`join(call_sessions).order_by(call_id, created_at)`，user 轮→紧随 assistant 轮；turns 无 seq，按 created_at+role 交替启发式，同通内轮次天然串行，风险极低）。

## CP 端点（仿 script-insights 平铺模式挂 main.py）

- `GET/POST/PATCH/DELETE /api/qa-entries`：CRUD + 启停。
- `GET /api/reports/qa-pairs?min_calls=5`：归一化聚类计数报告（文本归一化与运行时匹配器共享同一函数）。
- apply：置 `enabled=1`；音频物化统一走 agent 侧 `tts-pregen`（为 enabled 缺音频条目合成 answer 入 TtsAudioCache），CP 不碰合成。

## 工具

- `tools/bok.py tts-mine`：出报告；`--apply` 入库（source=mined，voice_id 取当前 voice-map）。

## 运行时匹配器（新文件 `apps/agent/agent_runtime/qa_gate.py`，纯函数）

- 会话启动拉取 enabled 条目（CP 客户端，失败优雅空表），HybridLexicalEmbedding（CJK 逐字+bigram 哈希，零外部依赖）预建向量；查询时只 embed 用户话语一次；打分 0.6×余弦+0.4×子串比（照抄 InMemoryVectorStore 公式）。
- 数字串文本在闸②已被旁路，匹配器无需处理数字变体。
- v2 预留：MlxEmbedding 选型工厂（`KB_EMBEDDING_MODEL`，类已实现未接线）。

## 命中动作（全部有现成先例）

1. 取消停着的抢跑快照（`session.interrupt()` 通路；兜底=框架 rebase 自愈，幻影账本条目下轮自动重锚定）。
2. 手动补 user 消息入 ctx（paused 分支姿势，agent.py:1891-1898）——否则记忆/落库收不到这句。
3. `context_state.set_last_reply(罐头文本)` 预锚回声守卫（补 playout 滞后窗）。
4. `session.say(答案文本, audio=缓存帧, add_to_chat_ctx=True)`——文本进上下文保持 KV 严格前缀；应答音频未预生成 → 视为未命中走 LLM（闸门绝不触发云合成）。
5. `cp.add_turn` 落库两轮（user + assistant，assistant 的 provider 字段标 `qa-fastpath` 供审计）；`incr_qa_hit`。
6. `raise StopResponse()`（在所有 except-pass try 之外，WA 累积先例）。

## 打点与验收

- `QA_FASTPATH hit=1 entry=… score=…` / `bypass reason=…` 逐轮打点。
- 测试：`test_qa_gate.py`（闸门矩阵/阈值/归一化/配对挖掘 InMemoryBusinessRepository）、CP 端点测试（TestClient+sqlite 先例）、agent 接线测试（假 session 断言 StopResponse+落库+锚定）。
- 回归：全量 pytest + 三语 E2E（WhatsApp 捕获回归——闸门绝不可吃报号轮）+ `scripts/e2e_barge_in.py`。

## 非目标（本期）

web 快答库管理 UI（v2，本期 CP 端点+CLI 够用）；MlxEmbedding 语义向量与步骤作用域条目放量（v2，实测误配率达标后开）；垫话与快路的联动编排（各自独立门控已够）。
