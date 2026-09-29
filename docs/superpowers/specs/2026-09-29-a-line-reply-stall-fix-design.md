# A 线回复哑火修复——设计文档（探针先行）

- 日期：2026-09-29
- 状态：待审
- 范围：**A 线**。B 线本波结案（见 §1.3）。
- 前置证据：本日诊断（turns 账本 + agent.log + mlx llm.log 三面对齐），证据摘录见 §2。

## 1. 背景与范围

### 1.1 症状（2026-09-29 17:07-17:30 实弹，Ethan 手打）

A 线多通通话「只有垫音」：LLM 回复不出声、QA 乱答感、流程体感不动、8 秒心跳连环轰炸。代表通话 `call-ed6aa9b8`（17:29）：三轮 LLM 回复全部哑火，用户每轮 9-11 秒听不到任何真回复，只有 filler / nudge / stall-degrade 轮播。

### 1.2 根因链（已定案部分）

| 层 | 结论 | 证据 |
|---|---|---|
| **直接死因** | mlx :1235 生成段极端慢：坏通轮 1 prefill 17:30:03.98 完成 → 请求 17:30:14.729 完成，**生成段 10.7s（28 token ≈ 2.6 tps，正常 20-24 tps）** | llm.log bok-timing wall time；对照健康通 call-d07a50b3（79 秒前）同型轮生成段 2.3s |
| LLM 引擎 | 无辜：TTFT 912ms、prefill 354 token 0.9s 均正常 | agent.log LLM_TTFT_MS / llm.log Prompt processing progress |
| agent 放大器① | 首字早发防线在 2.6 tps 下失效（攒 10 字 ~4s、攒句界 8-10s） | `_RepeatSelfGuardStream._feed` 早发逻辑 + `_first_chunk_cut` |
| agent 放大器② | 轮 1 filler 被某 guard 静默 return（哪个 guard 日志不可见） | `BOK_FILLER fired count=1` 出现在 9 秒后、user turn 2 之后 |
| agent 放大器③ | watchdog 6s force-interrupt 掐在途回复；barge-in cancel 令 guard 缓冲文本蒸发 | `MINIMAX_BIDI_PERF sentences=0 canceled=1 (interrupted)` ×3 + `interrupted reply ledgered chars=10` ×3 |
| agent 放大器④ | stall-degrade 同级连发同一句（17:30:37 / 17:30:46 两次） | turns + `[stall-ladder] level=degrade streak=3/4` |
| 慢的成因 | **未闭案**。高嫌疑：prompt cache 顶满（17:30 时 61→64 条 / 5.83→6.27GB，第七波遗留「6GB cache 与整机冻结关联」观察位第二次出现）+ A+B 线连打时段 mt-llm/laya/ASR 同卡共存 | llm.log Prompt Cache 行 |
| 复现状况 | 同栈 20 轮探针 19/19 全绿（首声 1.0-3.7s）——坏形态不随栈常驻，是当时机器状态特发 | /tmp/probe20.log（FLOW20 2026-09-29 复跑） |
| 未闭案环 | 首 10 字 tee（`_PartialCaptureStream`）捕到了，但 bidi 零 `task_continue`——LLM 流到 TTS 之间有一环在慢速场景下挂起，探针正常 tps 下复现不出 | agent.log 层序推理；需 P0.1 钉死 |

### 1.3 B 线结案

- `call-b8793951`（5.5 分钟）：fwd worker 零 ASR 输入——**me 端麦克风当时未开**（Ethan 2026-09-29 确认），无输入是正常行为，非 bug，结案。
- `call-ee1db17d`「中文翻中文变体」：fwd/rev 车道方向配死（fwd 恒 zh→en / rev 恒 en→zh），对端语言与车道不符时硬翻。**记为已知观察位，本波不动**（自动换向/语言自适应另立后续）。
- 两通 `reaped` = 挂断后回收器正常收线。

### 1.4 「流程不动」的闭环解释

Ethan 当前模板（`3676d8e395bf`）为 3 步、**无 say 直念步**——step2/step3 内容全靠 LLM 应答交付。LLM 哑火 ⇒ 内容永远出不来 ⇒ 体感流程死了。健康通（judge unclear keep）里 LLM 正常应答、下一轮 rule=confirm 照推——流程本身是走的。修掉哑火，流程自然恢复；另加 P2.1「unclear 连续推进」满足「意图不明确时一步一步往下走」的产品预期。

## 2. 方案总览（四波，探针先行）

```
P0 探针波（零生产代码改动，除 P0.3 纯观测打点）
   ├─ P0.1 慢速注入复现 + py-spy 定案未闭案环
   ├─ P0.2 GPU 争用源定量（含 prompt cache 顶满姿态）
   └─ P0.3 filler 拦截观测补齐（+4 打点）
        ↓ 探针产出 Ethan 过目后动 P1
P1 A 线止血
   ├─ P1.1 LLM 生成停滞检测 + 分级处置（filler 补位 → 断流重发 → 兜底直念）
   ├─ P1.2 guard cancel 缓冲蒸发修复（账本完整性）
   ├─ P1.3 stall ladder 同级门修复（ladder_fired 账本替数值魔术）
   └─ P1.4 prompt cache 收紧（条件项：仅当 P0.2 证明成因）
P2 产品行为（与 P1 并行，文件所有权互斥）
   ├─ P2.1 unclear 连续 N 轮推进
   └─ P2.2 心跳 nudge 软化（文案 + 间隔 8→12s + pregen 重录 + 耳测）
        ↓
全量验收：pytest 全绿 + FLOW20 + soak 双语 + P0.1 探针修后复跑
```

## 3. P0 探针波设计

### P0.1 慢速注入复现探针 `scripts/probe_llm_stall.py`

**目的**：可控复现「2.6 tps 下 agent 全链行为」，py-spy dump 钉死「tee 有 10 字、bidi 零 task_continue」的未闭案环。不依赖复现 GPU 状态。

**组成**：
1. **限速 SSE 代理**（探针内嵌，asyncio）：监听 :12399，转发 `:1235/v1/chat/completions`。流式响应逐 SSE event 注入 delay，目标速率 `--tps 2.6`（按 chunk 均摊：`delay = chars_in_chunk / (tps * chars_per_token)`，中文 ~1 token/字）。非流式/其他路径原样透传。健康检查直通。
2. **通话驱动**：复用 `probe_flow_20rounds.py` 的建单/预合成/推音频基建，裁成 3 轮（身份 → 模糊问「吃什么包裹啊？」 → 再模糊问），`--rounds 3`。
3. **自动抓栈**：探针检测到坏标记（新窗口日志出现 `sentences=0 canceled=1 (interrupted)` 或「commit 后 6s 无 `TTS_FIRST_AUDIO_MS`」）→ 定位 worker job pid（ps 匹配 `agent_runtime` job 子进程）→ `py-spy dump --pid` 落盘。

**接线**：探针前置要求 `MLX_LLM_BASE_URL=http://127.0.0.1:12399/v1 python tools/bok.py serve`（env 指向代理；探针 doctor 检查该 env 已生效否则退出码 1 提示）。

**产出**：`reports/llm-stall-replay/`——py-spy 栈 dump + 时间线归因（卡在哪一行、哪一层壳）。**这份产出决定 P1.1/P1.2 的实现细节，必须先于 P1 落地。**

**修后复用**：P1 落地后同探针复跑，验收「慢速注入下无声>6s 轮占比」。

### P0.2 GPU 争用源定量（扩展 `scripts/gpu_contention_probe.py`）

**目的**：钉死 17:30 那 10.7 秒是谁抢的。先读现有脚本形态再扩展（本设计只定输出契约，不改其既有测量面）。

**四姿态 × 测 :1235 生成段 tps**（每姿态 ≥5 rep，固定 prompt 长度 ~3k token、生成 30 token）：

| 姿态 | 加载方式 |
|---|---|
| baseline | 无额外负载 |
| mt 满载 | :1236 持续翻译批（B 线工作负载形态） |
| laya 满载 | :8791 持续 decide 请求 |
| ASR 满载 | :8787 持续转写音频流 |
| cache 顶满 | :1235 prewarm 多通对话把 prompt cache 撑到 ≥6GB 后测生成 |

**同步采样**：`powermetrics --samplers gpu_power`（1s 间隔）。

**产出**：`reports/gpu-contention-2026-09-29/`——姿态 × tps 表 + GPU 利用率曲线 + 归因结论。**P1.4 的 go/no-go 依据**。

### P0.3 filler 拦截观测补齐（纯打点，唯一提前动生产文件的项）

`fillers.py` `_fire()` 每个 return 分支前补打点：

```python
print(f"BOK_FILLER skipped reason=<disabled|no-player|guards|max-per-call|cooldown|state|handle|no-entry>", flush=True)
```

- 不改任何行为，只补可见性。P1.1 的强制补位路径依赖此可见性（确认 filler 真的开火了）。
- 测试：现有 filler 测试族补断言（各 return 路径打点在/行为不变）。

## 4. P1 A 线止血设计

### P1.1 LLM 生成停滞检测 + 分级处置

**检测点**：`MlxLlmLLM` 流层（`livekit_plugins.py`）。chunk 产出时间序列，TTFT 后滑动窗 `W` 秒；窗内 token 数 `< BOK_LLM_STALL_TPS × W` → 停滞事件（每流至多各窗口触发一次）。

**分级处置**（处置只覆盖「TTFT 已过、流未死、零/慢出声」的窗口——与既有 `LLM_FIRST_TOKEN_TIMEOUT_S` 首字垫话层互补不重叠）：

| 窗口 | 动作 | 车道 |
|---|---|---|
| 第一窗口停滞 | **强制 filler 补位**：新增 `FillerController.force_fire(reason="llm-stall")`——绕过冷却与 agent_state 门，守每通上限与 player 在场；走 background track，**不占 speech 调度队列**（框架调度器串行，reply 挂着时只有 out-of-band 车道能出声——这正是 filler 的存在意义） | filler |
| 第二窗口仍停滞 | **断流重发**：MlxLlmLLM 层 cancel 内部 HTTP 流、同请求体重发（KV 前缀命中只重付生成）；已产 chunk 计数保留续接，不重播。**每流至多重发 `BOK_LLM_STALL_RESEND`（默认 1）次** | LLM 内芯自愈，上层无感 |
| 重发后仍停滞 | **放弃流，兜底直念**：已生成部分文本（含 guard 缓冲，P1.2 修复后可取）交 `_say_script` 直念，lane=`llm-stall-partial`；零文本 → 当前步 goal 首句渲染直念，lane=`llm-stall-fallback`。同时 cancel 在途 reply speech 防双声 | 直念 |

**env（全部入 `_FORWARD_ENV`）**：
- `BOK_LLM_STALL_TPS`（默认 `"5"`，`"0"`=整闸关）
- `BOK_LLM_STALL_WINDOW_S`（默认 `"3"`）
- `BOK_LLM_STALL_RESEND`（默认 `"1"`）

**观测**：`LLM_STALL window=<1|2|3> action=<filler|resend|partial|fallback> chars=N tps=<实测>`。

**测试**：`tests/test_llm_stall_recover.py`——FakeLLM 注入可控 token 间隔流；断言三窗口顺序、kill-switch、重发不双播（已产 chunk 只续接）、兜底 lane 账本行。

### P1.2 guard cancel 缓冲蒸发修复

**现状**：`_RepeatSelfGuardStream._run()` 被 cancel 时（barge-in / watchdog force-interrupt），`_buf` 缓冲文本随协程蒸发——三轮 `chars=10` 只有 tee 捕到的部分入账，其余证据丢失。

**修法**：
1. `_RepeatSelfGuardStream` 暴露 `pending_buffer` 属性；`_run` 的 `except asyncio.CancelledError` 分支打 `REPEAT_GUARD_CANCEL_DROP chars=N` 后 re-raise（行为零变化）。
2. agent.py `_on_speech_created._watch`（interrupted 补账点）读 `pending_buffer` 拼进 partial——turns 账本从此完整（已播部分 + 未播缓冲）。

**不改播出行为**（speech 已死播不了）；补播职责归 P1.1 处置 3。

**测试**：`tests/test_repeat_guard_cancel.py`——流中途 cancel，断言账本含缓冲文本、打点在、正常流零变化。

### P1.3 stall ladder 同级门修复

**现状根因**：同级不连发靠「发射后把 streak 顶到下一级门槛-1」（degrade 发射后 streak=4）——但 `stall_ladder_level(4)` 仍返回 `"degrade"`（4≥3），下一轮 streak 仍在 4 时**同级再发**（call-ed6aa9b8 17:30:37/17:30:46 两发同句实证）。

**修法**（数值魔术 → 显式账本）：
- `FlowController` 加 `ladder_fired: set[str]`（`advance()`/`jump_to()` 清空）。
- agent.py 发射条件加 `and _lvl not in flow_ctrl.ladder_fired`；发射后 `add`。
- 退役「顶 streak」写法。`bypass→close` 直升逻辑保留（close 级由 `enter_closing()` 天然只跑一次，不入同级门）。

**测试**：`tests/test_stall_ladder.py` 补回归——streak=3 发 degrade 后，streak=4 轮**不得**再发 degrade（跳过），streak=5 发 bypass；步切换后同级别可再发。

### P1.4 prompt cache 收紧（条件项）

**仅当 P0.2 证明 cache 顶满姿态下生成段显著退化**：`BOK_LLM_PROMPT_CACHE_BYTES`（既有三级档 env，2026-09-25 模型路由波）单通档 6GB→5GB，重跑 P0.2 验证。不成立则不动，记录归因结论即可。

## 5. P2 产品行为设计（与 P1 并行，文件互斥）

### P2.1 unclear 连续推进

**语义**：意图不明确（judge `route=keep`）连续 N 轮 → 按 rule=auto 同构管线推进一步，新步内容照常 LLM 应答。「流程别停」。

**实现**：
- `FlowController` 加 `unclear_streak`（per-step）：judge `route=keep` 消费点 bump；实答轮（`relieve_stall_streak` 同挂点）-1 抵销；`advance()`/`jump_to()` 清零。
- agent.py judge 块 `route=keep` 分支：`unclear_streak >= N` 且**非最后一步**且**非 closing**且**非 done** → 推进三件套（`advance()` + `set_flow_current()` + `_invalidate_stale_preemptive("unclear 连续 → 推进")`，与 rule=auto 同构）。
- 打点 `[flow] unclear-advance step=N streak=N`；审计 `flow.unclear_advance`（detail 带 streak/step）。

**env（入 `_FORWARD_ENV`）**：`BOK_UNCLEAR_ADVANCE`（默认 `"1"`，`"0"`=关）、`BOK_UNCLEAR_ADVANCE_N`（默认 `"3"`）。

**边界**：最后一步不推（到底后交 stall ladder 接管）；closing/done 恒不推；WA 收号步未捕获不推（复用 `wa_confirm_advance_allowed` 门）。

**测试**：`tests/test_unclear_advance.py`——连续 keep 计数/第 N 轮推进/审计/实答抵销/最后步不推/kill-switch。

### P2.2 心跳 nudge 软化

**文案**（`agent.py` `_nudge_line` 三语，软陪伴型、去「喂」开头——**待 Ethan 耳测拍板**）：

| 轮 | cantonese | zh | en |
|---|---|---|---|
| 1 | {name}，唔急，我等你，你聽到就應我一聲。 | {name}，不急，我等您，您听到就应我一声。 | {name}, no rush — I'm here whenever you're ready. |
| 2 | {name}，我仲喺度，你有咩想問隨時講。 | {name}，我还在，您有什么想问随时说。 | {name}, I'm still here — ask me anything. |
| 3 | {name}，唔好意思，你可能喺度谂紧，我等你。 | {name}，不好意思，您可能在想事情，我等您。 | {name}, sorry — take your time, I'll wait. |

**间隔**：`SILENCE_NUDGE_SECONDS` 默认 `"8"` → `"12"`。

**缓存**：新文案=新缓存 key，旧吓人条目自然失效不删（不命中即无害）。新增 `tts-pregen --nudge`——按账号 roster 对象名批量预合成三句变体（首通即命中，零冷合成）。

**耳测**：新旧对照样本落 `~/Desktop/nudge_ab/`（三语 × 新旧 × 2 音色），Ethan 拍板后定稿。

**测试**：`tests/test_nudge_lines.py` pin 新文案、间隔默认值、轮换序。

## 6. 测试与验收

| 项 | 标准 |
|---|---|
| 全量 pytest | `./scripts/test.sh` 全绿（预计 +25~35 条新测试） |
| FLOW20 | 20/20 零坏标记（含 `--narrowband` 臂） |
| soak 双语 | canto/zh PERCEIVED p50 ≤3000ms 无回归 |
| P0.1 修后复跑 | 慢速注入（--tps 2.6）下「无声>6s 轮」占比 <20%（修前实测 ~100%），`LLM_STALL` 行按窗口开火 |
| P0.2 产出 | 姿态×tps 归因表落 `reports/gpu-contention-2026-09-29/`，P1.4 go/no-go 有据 |
| P2.1 | 连续 unclear 场景推进可达最后一步；审计行在 |
| P2.2 | 新文案耳测通过（Ethan）；`ASR` 后新缓存命中 `TTS_CACHE hit=1` |
| 常态零回归 | 正常负载（无注入）下 agent.log 无新增异常标记族 |

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| P1.1 断流重发期间旧流未真断 → 双流双 chunk | cancel 旧 httpx 流并 await 收尾后才重发；重发一次为限；测试钉「已产 chunk 只续接」 |
| P1.1 force filler 与既有冷却/风暴防线冲突 | force 只绕冷却与 state 门，每通上限与 player 仍在守；P0.3 打点可观测 |
| P2.1 推进错步 | N=3 门槛 + 非最后步 + closing/done/WA 门；`BOK_UNCLEAR_ADVANCE=0` 一键回退 |
| P2.2 新文案 MiniMax 韵律仍冲 | 耳测 A/B 拍板后才合入；文案集中在 `_nudge_line` 单点，改回零成本 |
| kill-switch 总表 | `BOK_LLM_STALL_TPS`/`BOK_LLM_STALL_WINDOW_S`/`BOK_LLM_STALL_RESEND`/`BOK_UNCLEAR_ADVANCE`/`BOK_UNCLEAR_ADVANCE_N` 全入 `_FORWARD_ENV`（prod 封闭 env 面可达——BOK_FLOW_GRAPH 同款教训） |

## 8. 执行序与文件所有权

1. **P0.1 + P0.2**（新探针脚本 + reports，零生产代码）→ 产出呈 Ethan 过目。
2. **P0.3**（fillers.py 纯打点）可与 1 并行。
3. **P1.1**（livekit_plugins.py 为主）∥ **P1.2**（livekit_plugins.py guard + agent.py watcher——与 P1.1 同文件需串行或同一执行者）∥ **P1.3**（flow.py + agent.py 阶梯段）——文件冲突关系：P1.1/P1.2 同文件串行；P1.3 独立。
4. **P2.1**（flow.py + agent.py judge 段）∥ **P2.2**（agent.py `_nudge_line` + pregen 脚本）——P2.1 与 P1.3 都动 agent.py/flow.py，按波串行。
5. 全量验收。

## 9. 明确不做（本波）

- B 线方向自适应/自动换向（观察位记录，另立）。
- `PERCEIVED_MS` 修表专项（A3，既有排队项）。
- 尾部瘦身/前缀断裂（A1/A2，实时主菜既有排队项）。
- LLM 出 chat_ctx 的 out-of-band 道歉交付重构（既有排队项）。
