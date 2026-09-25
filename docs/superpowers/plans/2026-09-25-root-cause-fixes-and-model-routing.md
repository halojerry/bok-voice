# 根因修复与模型路由统一（2026-09-25 v2 定案 — 路由优先重排）

病灶档案见 [docs/ARCHITECTURE-MAP.md](../../ARCHITECTURE-MAP.md) §5（G1-G8）与
[reports/latency-soak/LANE-AB-2026-09-25.md](../../latency-soak/LANE-AB-2026-09-25.md)
附3/附5（引擎无罪·编排有罪 bench）。

**v2 变化（Ethan 拍板，2026-09-25）**：原 v1 把「模型路由」排在止血项之后是排错了——
**车道云端/本地皆可选 + 档位预置本身就是根修**：judge/settle 改道不再是一次性的工程决定
（原「路线 A 云端 / 路线 B 降 4B」二选一作废），而是 root 在界面上随手拨的开关。重排后：

- 原「阶段 0 止血」大半被路由开关取代（9B 撤出=改道结果，不是独立动作）；
- 路由治不了的三件（G3 monitor 误杀、G6 双账本、G7 词库饿）降为**卫生项**，同阶段顺手清掉，
  不再单列阶段；
- `LlmLaneGate`（客户端 QoS 闸）**降级为可选项**——只有「Mac 本地同时压多车道重负载」才需要；
  路由在手，重负载车道的自然答案是把它们拨走（云端/别处），不是在客户端修队。

**生产目标**：单通感知 p50 ≤ 1.2s；4-6 路并发不塌；演示档 Mac 双路即用；模型/端点/云地
切换 = root 一个界面、下一通生效、零 SSH 零重启。

---

## 1. 判定：切换治什么、不治什么（先把「本」说清）

| 病灶 | 路由可切换能否根治 | 说明 |
|---|---|---|
| G2 judge/settle 二号 9B | **能** | 9B 从「常驻事实」变「可选后端」；改道=UI 一次操作 |
| G4 内存挤压/换页 | **能**（主因面） | 9B 不驻释放 ~9G；演示/单机档常驻集收敛 = 4B+MT2+ASR+TTS。cache 预算档随预置下发 |
| G1 ASR 抢 GPU（偷 35-60%） | **能绕** | 回复/判定拨云端 → Mac 只剩 ASR+TTS，争抢面消失；全本地档仍需 CUDA 分离（§4） |
| G5 推理 FIFO 无优先级 | **能绕** | 同上——车道拨散即无队可排；全本地重负载（生产战役）才需要 SGLang priority（§4） |
| G3 monitor 误杀 | **不能** | 卫生项：真端点 + active_calls 硬否决（半天） |
| G6 双账本掩盖 | **不能** | 卫生项：报表首指标换 PERCEIVED（半天） |
| G7 QA 词库饿 | **不能** | 运营项：L-① 喂库 + 门禁化（§5） |
| G8 散装 env 车道 | **反向根治** | 新车道只准进路由表，CI 扫 env 读取面 |

结论：**placement 类病灶（G1/G2/G4/G5）的根修 = 路由**；scheduling 类只在「全本地重负载」
场景存在（生产 CUDA，§4）；观测与运营三件与路由无关，按卫生项清。

---

## 2. 阶段 0 — 模型路由统一 + 档位预置（根修主干）

### 2.1 车道清单（现状 env → 路由键，全读取面已探测，无第六条）

| 路由键 | 用途 | 现状 env（保留为缺省） | 缺省 |
|---|---|---|---|
| `a_reply` | A 线回复主 LLM | `MLX_LLM_BASE_URL` :1235 | local |
| `judge` | 流程 judge + 意图判据 | `FLOW_JUDGE_LLM_BASE_URL` :1237 + `FLOW_JUDGE_LLM_API_KEY`（云钩子已有，agent.py:4042/4182） | local |
| `mt` | B 线翻译 | `MT_LLM_BASE_URL` :1236 | local |
| `settle` | 纪要/蒸馏/润色 | `BOK_SETTLE_LLM_BASE_URL` :1237 + `BOK_SETTLE_LLM_MODEL` | local |
| `mining` | CP 挖掘/聚类/L-①②/gap | `MLX_LLM_BASE_URL`（CP 面）+ `BOK_QA_CLUSTER_MODEL` | local |

润色（`BOK_POLISH_OFFLINE`）是离线开关不是车道，随 settle/mining 走。

### 2.2 数据面
- `global_settings.model_routing_json`（`deps.py build_engine()` `_ensure_column` 唯一迁移
  落点，`sms_json` 先例；Supabase 引导件同步重跑）。
- 每车道：`{"provider":"local"|"openai", "base_url":"", "model":"", "api_key":"",
  "extra":{"enable_thinking":false}}`。`local` = 现状 env 行为逐字节不变；`openai` =
  OpenAI 兼容云端（base_url/model/api_key 必填，保存校验 400）。
- `enable_thinking` 车道旗：LANE-AB 实证 Qwen3.5 家族思考全开（5.85s 全 `<think>`）。本地
  mlx=`--chat-template-args`；云端=`extra_body`。切云端 MT/Qwen 系必带。
- **档位预置（preset）= 命名快照**：root 可把当前五车道整表存成命名档（如「演示档」「生产档」），
  一键套用=整表覆盖。内置两份出厂档：
  - **演示档（Mac）**：全 local；judge/settle→4B 共享 :1235（9B 不驻）；cache 6GB；
  - **生产档（CUDA/云）**：五车道指 CUDA SGLang 端点或云端（届时按 §4 验收数据定）。
  预置只存路由值不含密钥（密钥留原位，套档不清 key）。

### 2.3 消费面（热切换）
- agent 每通装配已调 `cp.get_settings()`（agent.py:2481 / interpret.py:805）——路由挂同一
  fetch，**改道下一通生效，零重启**。本地引擎内换 model 仍需该 server 重启（外部事实，UI 注明）。
- CP 进程内消费者（qa_cluster/gap_mining/gap_proposals/summarize）直读 repo 同列。
- 唯一解析函数 `packages/core/bok_voice_core/model_routes.py::resolve_route(lane, env)` 纯函数
  ——agent/CP/CLI 三面共用（`intent_rules.py` 共享契约先例）。解析顺序：
  **路由 provider=openai > 既有 env 云钩子（judge） > 本地缺省**。
- kill-switch `BOK_MODEL_ROUTING=0` 全表忽略、字节同旧（进 `_FORWARD_ENV` dev/prod 双面）。

### 2.4 权限面（红线，需求原话：admin 与话务员不允许，只有开发人员和 root）
- `GET/PUT /api/model-routing` + `POST /api/model-routing/test` + 预置读写——**root + 机器通道
  专属**，同 `nodes`/`licenses` 先例：**不进管理键目录**（admin 授不了、下发面板看不见）、
  admin 403、user 403。
- 系统无「开发人员」角色——开发人员 = 部署面（机器通道 / bok env / DB），天然持有；UI 唯一
  入口 = root。不铸第四角色。
- web `/settings`「模型路由」卡仅 root 渲染（`me().role === "root"`）。

### 2.5 简化面
- 五车道卡 ×（本地/云端 toggle、base_url、model、api_key 掩码、enable_thinking）+ 档位
  预置条（套用/另存/删除）。
- 每车道「测连」：`POST /api/model-routing/test {lane}` → 以当前配置发 max_tokens=1 探活
  （`bok._probe_llm` 同款，10s informational），回延迟+模型回显。
- 默认全 local = 零配置即用；字段空 = env 缺省。

### 2.6 安全
- api_key 掩码读回（`sms.secret` 先例：PUT 空传保留；明文回源仅 root+机器通道，同
  `GET /api/settings?internal=1`）。
- 审计 `model_routing.update` / `model_routing.preset_apply`（车道级 detail，零密钥材料）。
- `a_reply` 载客户 PII——云端档 UI 明示数据出境提示。默认恒 local；上云=root 显式拍板+留痕。

### 2.7 随手清掉的卫生项（路由治不了，同阶段半天量）
- **G3**：`cmd_monitor` 探活改真 `GET :port/worker`（prod 探针函数抽单点共用）；
  **active_calls>0 任何失败不杀**（硬 veto，取代现 12 轮/60s 抬门槛）；`test_health_surface.py` 钉住。
- **G6**：soak/驾驶舱 headline = PERCEIVED p50/p95，墙钟标「含垫话」。
- 9B 后端化：`tools/bok.py` :1237 9B 不再随栈常驻（`BOK_DEV_9B=1` 显式才拉，注释引用附3）。

### 2.8 测试（tests/test_model_routing.py）
权限矩阵（admin 403/user 403/root 200/机器 200）；掩码与空传保留；openai 缺字段 400；
`resolve_route` 三层解析+kill-switch 字节等价；热切换（fake server 断言下一通打新端点）；
enable_thinking 两面形状；预置套用不清密钥。

---

## 3. 阶段 1 — 演示档验收（Mac 双路并发，答案：够用）

bench 依据（LANE-AB 附5）：4B 隔离 63 tps；**双 4B 并发各 53-62 tps**（GPU 时间片近免费）；
单路 in-call 13-20 tps；B 线 2452ms；整机天花板 = prompt-cache 4-6 路 + TTS sidecar 全局锁。
双路在线宽内一半的位置。

演示档配方（=出厂预置「演示档」）：全 local 4B；judge/settle→:1235 共享 resident（9B 不驻，
内存让给缓存）；罐头/直念步/垫话盖住尾延迟；桌面清空（演示纪律既有）。**双路并发演示成立**。

**验收**：`load_audio_concurrency.py LOAD_ROADS=2`（Mac）——两路 p50 感知 ≤ 2.5s、零 worker
异常、零 swap 爬升；`e2e_barge_in` + `e2e_edge_cases` 不回归。

---

## 4. 阶段 2 — 生产引擎（仅当生产选「全本地重负载」才需要）

路由在手，生产有三条摆法，按 §3 验收数据现场拨：
1. **全云**：五车道拨云——Mac/CUDA 都只剩 ASR+TTS，G1/G2/G4/G5 全消失（数据出境由 root 拍板）。
2. **混合**：a_reply→CUDA SGLang，judge/mining→云，MT→CUDA——常见生产态。
3. **全本地 CUDA**：SGLang 单引擎 request-level priority（a_reply 高优、judge/settle/mining
   低优）+ `--schedule-policy lpm` + RadixAttention（N 路战役共享 system 前缀 KV）——G2/G5
   结构性消失；ASR 分离（分卡/CUDA MPS，递进 bench 先行移植定夺）；MT 带 enable_thinking=false。

Huihui-9B offscript 对照 = 4B/9B 切档裁决点。vLLM（APC+priority）对照腿。SGLang MLX backend
（v0.5.10+）只作 Mac 试验腿，不进生产路径。

**验收**（CUDA 全探针组）：`probe_latency_soak`/`probe_offscript_soak`/`e2e_interpret`/
`e2e_barge_in`/`load_audio_concurrency` 4-6 路：p50 感知 ≤ 1.2s、优先级 A/B（背景洪水下回复
延迟差 ≤ 10%）、零 worker 异常。

---

## 5. 阶段 3 — QA 喂库 + 防复发

- **G7**：L-① gap 采纳 + `tts-mine --cluster` + `tts-pregen --qa` 进运营日常；`probe_qa_hit`
  would-hit 比率进 soak 常设段+驾驶舱铃铛。验收：真实问法 would-hit 0% → ≥ 30%。
- **G8 立规**：新增 LLM 车道只准进路由表；`tests/test_model_env_surface.py` 扫 agent+CP 全部
  LLM 端点 env 读取面，白名单=五车道缺省 env + `_FORWARD_ENV`，新增即 CI 红
  （`test_forward_env.py` 同款纪律）。
- **可选项（原阶段 2，降级）**：`LlmLaneGate` 客户端 QoS（reply 独占、背景让路）+ CP 挖掘
  静默窗（active_calls>0 不挖）——仅当出现「Mac 本地同时压多车道重负载」的长期形态再做；
  路由能拨走就不修队。
- 4B prompt 域补课（配合轮照本重问 3 连问）排独立课程清单，不混入本计划。

## 6. 验收矩阵

| 阶段 | 探针/测试 | 判据 |
|---|---|---|
| 0 | tests/test_model_routing.py + 手动测连 + 卫生项 | 权限矩阵全过；热切换下一通生效；kill-switch 字节同旧；monitor 0 误杀；报表首行 PERCEIVED |
| 1 | load_audio_concurrency=2（Mac）+ barge_in/edge | 双路 p50 感知 ≤ 2.5s；零异常零 swap 爬升 |
| 2 | CUDA 全探针组 | p50 感知 ≤ 1.2s；4-6 路稳；优先级 A/B 显效 |
| 3 | probe_qa_hit | would-hit ≥ 30% |

## 7. 风险与回退

- 车道 env 缺省恒在：路由只覆盖不替换；任何车道回 local = 一键。
- `BOK_MODEL_ROUTING=0` 全局回旧（dev/prod 双面）。
- judge 云钩子（env）保留部署面逃生口（解析顺序见 §2.3）。
- 预置只存路由值不含密钥；套档误操作=再套回，密钥无损。
- 云端 lanes 全 opt-in + 审计 + UI 出境提示；默认全 local，密敏红线不松动。
