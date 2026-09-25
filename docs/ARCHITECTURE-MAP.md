# Bok Voice 架构全景图（ARCHITECTURE MAP）

> 2026-09-25 深度调查定稿。本文 = 流程 / 节点 / 功能 / 逻辑 / A·B 线 / 模型调用 / 运行 / 节点配合的全图，
> 每个节点标注已知病灶（⚠️）。数字全部来自今晚实测（`reports/latency-soak/LANE-AB-2026-09-25.md` 附 3/4/5）。
> 运行验收基准仍以 [RUNTIME_TOPOLOGY.md](RUNTIME_TOPOLOGY.md) 为准；本文是**逻辑视图**。

---

## 1. 全局运行拓扑（进程视图）

```
bok.py serve（单点编排：启动顺序/健康/worker env 白名单 _FORWARD_ENV）
│
├─ control-plane      :8000   FastAPI + SQLite（对象/人设/话术/QA/建单/审计/token）
├─ livekit-server     :7880   RTC 信令与媒体
├─ ASR sidecar        :8787   Qwen3-ASR-1.7B (MLX)  ← ⚠️G1 每滑窗解码与 LLM 抢同一块 GPU（实测吃 35-60% 吞吐）
├─ TTS sidecar        :8788   Qwen3-TTS（预生成/canned/试听；A 线通话主打 MiniMax 云）
├─ LLM 车道           :1235   mlx-lm server（avan-ag 4B 4bit；prompt-cache 128 条/12GB）
├─ MT LLM             :1236   Hy-MT2 1.8B（B 线专用，缺失自动回退 :1235）
├─ settle/judge 专线  :1237   Huihui-9B 4bit ← ⚠️G2 与车道并存的「二号 9B」常驻（judge/settle/意图批量判定）
├─ embed              :8789   bge
├─ b-line worker      :8790   WS（旧同传通道；现役=fwd/rev 双 worker）
├─ agent worker       :8081   A 线智能体（livekit-agents job 进程）
├─ interp-fwd/rev     :8082/3 B 线双 AgentSession
├─ node-agent         :3000   薄节点守护 + 坐席 UI 静态托管
└─ monitor（dev）             健康看门 + respawn ← ⚠️G3 场景间隙 active_calls=0 时 1s TCP 探针裸奔，曾一夜误杀 worker ×7
```

**所有推理进程共享同一块 GPU / 统一内存（M4 Pro 48G）——没有隔离、没有优先级。** ← 这是全部性能病灶的根。

---

## 2. A 线：一通通话的完整逻辑

### 2.1 建单与装配（通话开始前）

```
POST /api/calls（template_id 快照 → call_sessions.template_id；created_by 盖章；语言钉死）
  → dispatch → LiveKit room + agent job(:8081)
  → 装配（一次钉死，全程不变）：
      语言三方钉死：ASR hint / LLM【用户语言】/ TTS 音色+language_boost
      车道解析：settings llm_json > BOK_LLM_TIER > 表默认 → MLX_LLM_MODEL
      FlowController.from_template（机器通道吃 published_json 冻结版；人类通道 live 草稿）
      QaIndex(account + owner_scope=created_by)  ← ⚠️G7 词库饿：103 条 vs 真实问法 200，would-hit 0%
      热词上下文（模板 hotwords + 行业词 + 对象专名，≤200 字符）
      心跳挂钩必须先于 session.start（否则首段沉默 arm 失效）
  → 开场白直念（step1 ref 首行）‖ LLM 前缀预热（真实开场白作 assistant 轮同构预热）
```

### 2.2 一轮话音的七层漏斗（核心逻辑，precedence 铁律）

```
客户话音 → LiveKit track
  → Silero VAD（min_silence 0.35s）
  → Qwen3ASRLiveSTT → sidecar :8787 滑窗 partial（~700ms；
        AI thinking/speaking 时抬档 3000ms 抑制 ← G1 的唯一缓解，已默认开）
  → 句级提交（事件源：标点 / vad-pause≥10字 / [B线] clause-len）→ join-hold 跨段拼接
  → FINAL → agent.on_user_turn_completed
      │
      ├─ 0 垫话臂（并行）：500ms 内回复首音频未到 → out-of-band 垫话音轨（每通≤3、连轮冷却、链发≤1）
      │     ⚠️G6 垫话+兜底把「有声音」和「答案到了」拆开——墙钟首声 vs PERCEIVED 两本账
      ├─ 1 内置 REFUSE 收线（verdict 车道）
      ├─ 2 DEFER 短应承（社交拖延轮 → 三语直念，不推进）
      ├─ 3 say 直念锁（轮开始时当前步待念 → 先念，流程推进让位）
      ├─ 4 分支动作（话术 ref 分支行 【收线/转人工/跳第N步/留本步】；双护栏防热词幻觉挂断）
      ├─ 5 话术图 graph（关键词命中 → play_qa / jump / notify_human；判据 judge 补位走 :1237 ⚠️G2）
      ├─ 6 QA 快路（0.90 字面阈值 + 四道闸 + PCM 已物化 → ~50ms 直播罐头）
      └─ 7 自由 LLM（ContextAwareLLM：
             静态前缀整场 KV 冻结 + 尾部跨轮追加账本重放（严格前缀契约）
             + PrefillSpeculator 说话中预热 ← ⚠️G5 FIFO 队列：预热请求可排在真实回复前
             + 出口剥框架标记/尾锚/复读守卫）
  → MiniMax 云 TTS（bidi 流）/ tts-cache 命中直念 → playout 队列（min_gap 0.3s）
  → turns 上报（line/speaker/gen/template_step/perceived_ms；_spawn_report 挂断 flush）
```

### 2.3 每轮并行的背景任务（同 GPU 抢食）

| 任务 | 打哪 | 触发 | 备注 |
|---|---|---|---|
| 背景 flow judge（模糊轮推进判定） | :1237 9B | 每个判不出的轮 | FLOW_JUDGE_DELAY 让路 + idle cap 6s |
| 意图判据批量 judge | :1237 9B | 关键词未中且图有判据 | timeout=20（9B prefill 慢） |
| PrefillSpeculator 预热 | :1235 车道 | 说话中每 600ms×≤2 | max_tokens=1，但 prefill 同尺寸 ⚠️G5 |
| 心跳/收线/垫话 | 零 LLM | 12s 静默 | 脚本直念 |
| settle 纪要（挂断后） | :1237 9B | 挂断 | D7 窗口自适应等待 |
| WA 上报 / turns 上报 | CP :8000 | 事件 | |

### 2.4 收线与结算

REFUSE→enter_closing→farewell 直念；12s 自动收线；`/end` 带 intent_code；
`_settle_core`：意图规则评估（_facts 账本 × intent_rules）→ disposition 覆盖 → 9B 纪要 →
短信钩子（可选）→ 审计。

---

## 3. B 线：实时同传（manual 自驱管线）

```
POST /api/calls(kind=interpret, glossary, voices_json)
  → 双 dispatch：fwd(worker:8082)=我→对方   rev(worker:8083)=对方→我（默认纯字幕）
每方向（P2 manual 架构，框架不再自动回复）：
  话音 → VAD(0.35) → ASR 句级 FINAL（事件源三枚：标点/子句 8 字/长度档 10 字 CTK 稳定切）
     → 单消费 FIFO 队列 → _mt_once 直调 :1236（15s wait_for；无 judge 无漏斗——B 线零话术层）
     → session.say() 串行排队播放（MT 与上一句播报流水线重叠）
     → 背压 _PlaybackBacklog：估时 >6s 从最旧 force interrupt（队头与最新永不弃）
  glossary 双路注入：ASR 热词（不吃 A 线行业词）+ MT prompt 术语槽（会话级常量）
  语气词标记 2.8（(laughs) 等 19 个，只活合成层）；MT 出口剥反引号；
  译文延迟物理下限 ≈1.2-1.5s（子句稳定点+VAD 0.35+解码+MT+TTS 首包）
```

---

## 4. 模型调用面（谁打谁）

| 调用方 | 端口 | 模型 | 量级 |
|---|---|---|---|
| A 线回复 / 预热 / 抢跑 | :1235 | 车道（现 4B） | 每轮 1-3 个请求 |
| flow judge / 意图 judge / settle | :1237 | Huihui-9B | 每模糊轮 1 + 挂断 1 |
| B 线 MT（两方向） | :1236 | Hy-MT2 | 每子句 1 |
| 全部 ASR（A+B 线） | :8787 | Qwen3-ASR | 每 700ms/3000ms 一窗 |
| A 线 TTS | MiniMax 云 | speech-2.8 | 每句流式 |
| 预生成/canned/试听 | :8788 | Qwen3-TTS | 离线 |

---

## 5. 病灶总表（问题自暴露，全部今晚实测）

| # | 病灶 | 证据 | 根因层 |
|---|---|---|---|
| G1 | ASR 与 LLM 同 GPU 无隔离 | 隔离 63/39 tps → ASR 解码中 24-42/25 tps | 架构：单 GPU 无 QoS |
| G2 | judge/settle 二号 9B 常驻 | :1237 与 :1235 并存；swap 20.9/22.5G、闲置权重页全换出 | 编排：常驻面超预算 |
| G3 | monitor 场景间隙误杀 | offscript 窗口 `parent process shutdown` ×7 | 编排：探针无功能感知 |
| G4 | 桌面/缓存挤兑统一内存 | 12GB prompt-cache 预算 + 桌面 ~10G；PERCEIVED 三通方差 1984/2752/4687 | 卫生 |
| G5 | 推理 FIFO 无优先级 | speculator 预热 prefill 同尺寸，可排真回复前；server 内并发虽不串行但无 QoS | 编排 |
| G6 | 掩盖层拆两本账 | 墙钟首声 p50 1.6s vs PERCEIVED p50 5.0s（9B 夜）；「下午平手」= 垫话盖耳 | 观测 |
| G7 | QA 词库饿 | would-hit 0%（103/200） | 数据运营 |
| G8 | 漏斗七层状态复杂度 | 一轮过 7 层闸 × 十余本账本（said_steps/_facts/graph_fired/qa_played/...） | 工程复杂度 |

**好消息（同样实测）**：引擎无罪——mlx-lm server 隔离即 LM Studio 级（4B 63 / 9B 39 tps）；
跨模型并发 decode 几乎零损耗；KV 前缀契约 80-95% 命中。**问题全在编排层，不动模型可解大半。**

---

## 6. 开源框架对照（2026-09-25 深度调研定稿）

> 调研范围：Pipecat / TEN / Vocode / LiveKit Agents / SimulStreaming / Hibiki / serving 层（vLLM·TRT-LLM·llama.cpp·MLX 生态）。

### 6.1 管线形态对照

| | 管线模型 | 节点协调 | 背景任务隔离 | 同 GPU 争抢 | 并发会话 | 打断/partial | 活跃度 |
|---|---|---|---|---|---|---|---|
| Pipecat | 帧流 + ParallelPipeline | 帧推送**双车道**（控制帧插队） | job coordination，无 GPU 级 | 无 | 单进程 asyncio | InterruptionFrame 广播 + 各节点自取消 + **只提交已播文本** | 高（Daily） |
| TEN | App/Graph/Extension 声明式图 | 消息端口 | 无 | 无 | 多实例容器 | extension 消息 | 中高（Agora 系） |
| Vocode | Conversation 抽象 | 回调 | 无 | 无 | — | 基础 | **已停更（2024-11）** |
| LiveKit Agents（我们） | Session 事件 + 节点覆写（生成器流） | FlushSentinel 句界提前推 TTS | recipe 级独立小模型 | **推给 serving 层** | **worker/job 进程池 + prewarm** | min_duration/false-timeout/adaptive | 最高 |
| serving 层 | — | — | **vLLM priority / TRT chunked context / llama.cpp -np** | **本层主题** | batch/slot | — | 高 |

### 6.2 对我们八个病灶的对照结论

- **G1（ASR 抢 GPU）/ G5（FIFO 无优先级）——开源编排框架集体沉默，正解在 serving 层**：
  vLLM `--scheduling-policy fcfs|priority`（低值高优，主线回复高优、judge/speculator 低优同卡共存）、
  TensorRT-LLM batch scheduler `GUARANTEED_NO_EVICT` + chunked context（长 prefill 分块与 decode 交错，
  speculator 预热不再堵真回复）、llama.cpp `-np` slot 并行。**CUDA 窗口直接采用**；MLX 侧无现成品
  （mlx-lm server 无优先级、弱并发；社区有 libmlxforge/oMLX 未成熟）——我们现有姿势
  （进程隔离 + partial 3000ms 抑制 + speculator busy-gate）就是 MLX 生态当下最优近似，方向没错，只是要收得更紧。
- **G2（二号 9B）**：LiveKit 官方 content-filter recipe 也是「独立小模型 + 独立端口」——我们的 :1237 专线
  形态对，错在**常驻 9B 太重**；recipe 的启示=判定用小模型（judge 降 4B）或云端。
- **G3（monitor 误杀）**：LiveKit worker/job 模型每 job 独立子进程 + prewarm 权重预载——我们的 job 拓扑
  一致；monitor 应读 `/worker` 真端点 + 功能探针（已在 parking lot）。
- **G6/G8（漏斗复杂度）**：三个头部框架只有 TEN 用 DAG，LiveKit/Pipecat 后期都演化回「session + 可覆写钩子」，
  Pipecat 的图能力使用率低——**FlowController 单引擎路线与主流一致，不需要重写**。可吸收两件：
  ①Pipecat **SystemFrame 双车道**（打断/控制帧高优插队）= 我们 late-final guard / repeat-guard 的框架化形态；
  ②**只提交已播文本**（Pipecat/LiveKit 共识）——被截断的回复不进对话史。
- **B 线**：manual turn 模式是官方承认的译员语义逃生口（我们 P2 已是正道）；SimulStreaming AlignAtt
  「稳定前缀才交稿」= 我们 clause/len commit 同思想；**Hibiki（Kyutai）真开源且有权重 CC-BY + MLX int4/int8 变体，
  但语向不含中粤**——维持级联管线判定不变（S2S_ROADMAP 结论再次坐实）。
- **可借鉴小件**：LiveKit `user_transcription_timeout` 哑轮兜底（对齐我们的 heartbeat/watchdog）、
  FlushSentinel（可替代 `_RelaySynthesizeStream` 的一部分自定义）。

### 6.3 总判定

**编排层不值得换框架**：LiveKit Agents 的 session+覆写节点模型与 FlowController 形态同构且是最高活跃度官方栈；
Vocode 已死、TEN 绑 Agora、Pipecat 面向云端 API（其帧流思想可局部吸收）。**G1/G5 的根治在 serving 层
（CUDA=vLLM priority scheduling；Mac=维持进程隔离 + 把抑制档收得更紧）**——与我们自己的递进 bench 结论完全互证。

来源：docs.pipecat.ai（pipeline/interruptions）、docs.livekit.io（nodes/job/adaptive-interruption/recipes）、
github.com/ufal/SimulStreaming、github.com/kyutai-labs/hibiki、docs.vllm.ai（scheduler config）、
nvidia.github.io（TRT-LLM runtime）、github.com/ggml-org/llama.cpp#4666、github.com/hvasconcelos/libmlxforge。

---

### 6.4 SGLang 专记（2026-09-25 核实）

- **RadixAttention**：radix 树跨请求/跨会话 KV 复用——同一话术模板的静态前缀（语言规则/总览/对象档案）
  在 N 路并发通话间**一份 KV**，新通话从分叉点起算；配 `--schedule-policy lpm` 调度器按前缀命中排队。
  我们热路径已靠「严格前缀重放 + mlx LRU」拿到 cached=80-95%（实测），radix 的**新增**价值在多路并发
  共享与分叉复用（战役形态），以及把这件事从「我们手写的契约」变成「引擎内建」。
- **request-level priority**：整数优先级，高值先调度——回复=高优、judge/speculator=低优，G5 的对症药。
- **平台**：CUDA 一等公民；**Mac 由 v0.5.10 起有原生 MLX/Metal backend（PR #20342，docs「Apple Silicon
  with Metal」）**——年轻 backend，须实弹验证（隔离 tps 对比 mlx-lm 63/39 基线、radix/priority 在 MLX
  路径是否完整）。OpenAI 兼容 API=换车道只改 `MLX_LLM_BASE_URL`+model id，试验成本极低。
- 来源：github sgl-project/sglang #5603（scheduler priority）、#17846（MLX backend）、v0.5.10 release、
  LMSYS RadixAttention/zero-overhead scheduler 博客、SGLang paper (arXiv 2312.07104)。

## 7. 修复路线（按病灶 → 动作）

完整定案计划（阶段 0-5、模型路由统一配置、验收矩阵、风险回退）见
[docs/superpowers/plans/2026-09-25-root-cause-fixes-and-model-routing.md](superpowers/plans/2026-09-25-root-cause-fixes-and-model-routing.md)。

| 病灶 | Mac 短期 | CUDA 窗口（结构性解） |
|---|---|---|
| G1 ASR 抢 GPU | listening 期再抬档（代价：字幕/抢跑钝）；ASR 降档试点；**SGLang MLX backend 试点（v0.5.10 原生 Mac 支持）看调度器能否降低争抢面** | ASR 与 LLM 分卡；SGLang/vLLM serving 层统一调度 |
| G2 二号 9B | judge 走云端钩子 `FLOW_JUDGE_LLM_API_KEY` 或降 4B；settle 挂断后延迟批处理 | 全 9B 化 + vLLM continuous batching |
| G3 monitor | 健康探针改功能探针（LLM 忙碌感知） | 同 |
| G4 内存 | prompt-cache-bytes 下调；演示档清桌面 | 大显存 |
| G5 FIFO | speculator busy-gate 收紧（真请求在队时禁发） | **SGLang 主候选：request-level priority（回复高优、judge/speculator 低优同卡）+ `--schedule-policy lpm` 前缀感知调度 + continuous batching**；vLLM priority/APC 为对照腿 |
| G6 观测 | 探针主判据一律 PERCEIVED | — |
| G7 词库 | L-① 挖掘采纳 + 同义聚类（机制现成） | — |
