# 编排地图（ORCHESTRATION-MAP）— A 线实时通话编排活文档

> **改编排必先改此图。** 任何改动 `apps/agent/agent_runtime/agent.py` /
> `providers/livekit_plugins.py` / `fillers.py` / `tts_cache.py` 编排时序的
> 提交，必须同步更新对应段落与 file:line——图先于码，图失配=后来者按错误
> 模型排障（本仓已有多次「凭旧图找错根因」的教训）。
>
> 基线：main tree HEAD `78e17e1`（2026-10-02）。行号是锚不是契约（合流/重构会
> 漂移）；**以符号名与段落标题为准，行号仅助定位**。agent.py 段的行号按
> **已提交 HEAD** 标定（本波 S1 的 P0-P3 改动未提交，合流后会整体漂移，
> 主线合流时按符号名重校）；livekit_plugins.py / fillers.py 段的行号按本波
> worktree 终态标定。
> 权威边界：本图描述 A 线（agent worker）。B 线（interpret）、CP（control-plane）
> 与 LiveKit 框架内部只在与 A 线交界处提及，不展开。
>
> 关联文档：`docs/RUNTIME_TOPOLOGY.md`（拓扑）、`docs/ARCHITECTURE-MAP.md`
> （G1-G8 病灶与修复路线）、`docs/TURN-PIPELINE.md`（轮流水线）。
> 十七波已落（本图基线）：打断即 abandon / judge 挪 4B（a_reply=:1237 直连、
> judge=:1235 代理→4B）/垫话让路 hold=0/补答去重/TTS 全云/load_threshold 0.99。

---

## 0. 总览（一屏）

```
麦克风 → VAD(silero/FireRed) → STT 流(Qwen3-ASR sidecar :8787, 滑窗 partial)
   → FINAL_TRANSCRIPT → 框架 commit(turn_detection=stt)
   → on_user_turn_completed(24 段固定序)                       [阶段 1]
        ├─ 规则先行：WA 侦测 / 流程推进 / stall / say / 图意图 / QA 快路
        └─ 落穿 LLM：ContextAwareLLM → ExprAwareLLM → MlxLlmLLM   [阶段 2]
             → _LlmFallbackStream(首 token 3s 闸) → drain/regen
   → TTS(MiniMax 云 bidi/classic 或本地 Qwen3) → 首音频           [阶段 3]
   → item_added 交付账本 → 看门狗拆弹 / 垫话停播 / 晚到补答
```

三条横向事实贯穿全图：

1. **规则先行、LLM 兜底**：一切确定性判定（WA 号码、流程推进、图意图关键词、
   QA 词面快路）都在 LLM 之前同步执行；LLM 只承接「规则未覆盖」的开放轮。
2. **严格前缀契约**：LLM 请求的 system 前缀（指令 + 尾部）字节稳定是 KV-cache
   命中前提；任何每轮变形的文本（[流程状态] 标记、道歉句、记忆块）都不准进
   请求流或必须账本化复现（见阶段 2）。
3. **出口 chokepoint**：所有脚本出声必须过 `_register_reply_lane`（agent.py:4640）
   ——账本票据 + 重复锚 + 拆看门狗 + stall 抵销单点；车道清单 27 条见 §7。

---

## 1. 阶段 0：音频 → ASR FINAL（十步）

载体：`_Qwen3ASRLiveStream._run`（livekit_plugins.py:7506）＋ sidecar
（`services/qwen3-asr-sidecar/app.py`）。VAD 骨架与官方 `stt.StreamAdapter`
同构（START/INFERENCE_DONE/END_OF_SPEECH 事件驱动）。

| # | 动作 | 锚点 | 守卫/前提 |
|---|------|------|-----------|
| 0-1 | 音频帧进流：`_forward_input` 把输入帧 push 进 VAD 流 | livekit_plugins.py:7509-7516 | VAD provider 由 `_build_vad_provider` 装配（agent.py:2875）；`BOK_VAD_PROVIDER=silero|firered` |
| 0-2 | `START_OF_SPEECH`：**pre-roll 并流**（VAD prefix padding + min_speech 确认窗的 frames 并入 `_pending`）＋开 sidecar 会话 | 7521-7544 | 快语速首字结构性缺失的修复点；join-hold 续段不重复 start |
| 0-3 | `INFERENCE_DONE`：每窗 PCM 攒 `_pending` + 滚动尾部 `_smart_pcm`（≤8s，供 smart-turn） | 7545-7552 | `started` 且非 `_finishing` |
| 0-4 | `_maybe_partial`：≥300ms 节流、≥0.6s 音频才 POST sidecar `/api/chunk`，回滑窗全文 | 7897-7930 | 成功才清 `_pending`（D4 止血）；失败保留随下窗 |
| 0-5 | partial 三路分发：INTERIM_TRANSCRIPT（前端字幕）／稳定前缀 PREFLIGHT_TRANSCRIPT（抢跑 prefill）／`_publish_turn_partial`（E2 fallback 证据位） | `_maybe_partial` 后段（~7960-8060） | 稳定前缀 = 连续两窗一致；E2 泄漏判据靠它 |
| 0-6 | **句级提交**：partial 强句标点（。！？!?）+ 门（≥6 字/无数字·字母连写/跨窗稳定/1.5s 限速）→ 发 `FINAL_TRANSCRIPT(句)+END_OF_SPEECH` | `sentence_commit_enabled` 7167；提交点 8092 | `turn_detection=stt` 默认开；speech-end→first-audio 唯一 ≤1s 路径 |
| 0-7 | `END_OF_SPEECH`：closing-say 窗整段抑制 → smart-turn 语义闸（默认关；cantonese 车道恒关）→ join-hold（数字/系词续段不 finish）／VAD 微停顿提交 | 7554-7660；`_pause_trigger_enabled` 7181；`_join_hold_s` 7242 | smart-turn fail-open；join-hold 超时 `_hold_flush`（7764） |
| 0-8 | `/api/finish`：补尾段、整句高精度 FINAL；sidecar 侧 finish 锁等待 `QWEN3_ASR_FINISH_LOCK_WAIT`（默认 1.0s） | `_finish_session` 8257；sidecar app.py:1109 | 锁忙 >1s=增量捷径丢失→整段重解（EOU 抖动源，已放宽 0.3→1.0） |
| 0-9 | 回声过滤：`_echo_filter` 四闸口（停嘴/join-flush/interim/句级）剥热词回声；词表 echo_seen 衰落 | 7484-7504 | `QWEN3_HOTWORD_ECHO_GUARD=1` |
| 0-10 | 框架 commit → `on_user_turn_completed`；钩子内净化/润色/置信度（见阶段 1 第 7-9 段） | agent.py:5688 | `turn_detection` 由 `_turn_detection_from_env`（2376）解析 |

---

## 2. 阶段 1：turn 钩子（`on_user_turn_completed`）24 段固定序

载体：`agent.py:5688`（`PausableAgent.on_user_turn_completed`，HEAD 78e17e1 锚）。
**串行**执行；任何一段 `raise StopResponse` 即终止本轮（框架忽略该 user 消息的
回复），后续段不再执行。带 `★` 的段有 StopResponse 出口——**每个出口必须置位
`_reply_done_event`**（judge 让路信号；lint 钉死见 `tests/test_reply_chokepoint_lint.py`）。

| # | 段 | 锚点(HEAD) | 动作/守卫 |
|---|----|------|-----------|
| 1 | W-GATE 清零 + commit 戳 | 5692 / 5696-5699 | `_reply_done_event.clear()`；`_turn_timing["commit"]/["commit_ms"]` |
| 2 | 抢跑恢复 + 投机预算 | 5703-5707 | `_preemptive_gate` 暂停解除（实验档）；`_prefill_spec.new_turn()` |
| 3 | 心跳/静默归零 + 垫话撤表 | 5710-5713 | nudge count 归零、`_disarm_silence`、`_filler.cancel()` |
| 4 | 看门狗武装 + lane 槽清理 | 5717 / 5721 | `_arm_response_watchdog()`；`_pending_lane/_current_lane` 清（防 provider 泄漏） |
| 5 | 抢跑失效 helper | 5732 | `[流程状态]` 标记只进 turn_ctx（框架侧），**绝不进请求流**（前缀契约） |
| 6 | 复问放行闸 | 5768 | 本轮用户话 ≈ 上一轮客户话（≥0.8）→ `set_allow_repeat(True)` |
| 7 | ASR 润色预计算 | 5781 | `polish_turn`（确定性吸附→CSC opt-in）→ `set_polished`；**原文单轨**：下游全吃 raw |
| 8 | 回声自听守卫 ★ | 5801 | AI speaking 中且本轮≈上一条回复 → 整轮丢弃（StopResponse） |
| 9 | 热词幻听/泄漏清洗 E2 + snippet E1 | 5823 | `_vocab_echo_guard` 剥尾保头；E1 词表替换；净文写回下游唯一读面 |
| 10 | 空轮短接 ★ | 5891 | 净化后空串 → `EMPTY_TURN_DROPPED` + StopResponse |
| 11 | 暂停冻结 ★ | 5908-5929 | `self.paused` → 落库 gen=paused + StopResponse（恢复零逻辑） |
| 12 | 风暴静听/饿死兜底 ★ | 5930-6043 | 连环打断风暴退避（starve-ack/storm-listen）；`_starve` 连续 2 轮零输出→短承接 |
| 13 | M-23 首位回声剥离 | 6036 | 剥 AI 复述混进客户报号首位的回声前缀 |
| 14 | WA 号码累积/直捕 | 6049-6108 | 号码主导碎片暂存（5s flush）；直捕 canned 确认（wa-confirm）★ |
| 15 | 数字/单号累积 | 6109-6247 | 半截单号句暂存；疑问/算式/金额豁免 |
| 16 | 流程推进（flow） | 6248-6532 | `should_auto_advance`/`decide_advance` + verdict；WA confirm/reask；say 锁铁律（待念直念步让位） |
| 17 | 分支收线 REFUSE ★ | 6533 | 最高优先级直念车道；置告别窗旗（STT 整段静默丢弃） |
| 18 | stall 阶梯 ★ | 6556 | degrade→bypass→close 直念（同级不连发；实答抵销 streak） |
| 19 | DEFER ack ★ | 6608 | `last_verdict==DEFER` → 短应承直念（零 TTFT） |
| 20 | say 直念步 ★ | 6643 | 进入步 ref `say:1` → `_say_script` 直念（缓存线零 TTFT） |
| 21 | 图意图 + 分支罐头 | 6780-7144 | 关键词/判据/laya → jump/notify/play_qa；分支计划（stale 丢弃规则） |
| 22 | QA 快路 ★ | 7145-7376 | 词面 0.90（+语义补位）命中且音频已物化 → 罐头直答；未中落 LLM |
| 23 | paused 防御 + garbled-reask ★ | 7377-7501 | 置信度带/文本判据 → 罐头重问（cap=2 后静默）；异常落穿 LLM |
| 24 | 垫话 arm + 落穿 LLM | 7502 | `_filler.arm()`（回复 ~700ms 未到才播）；本轮回复交给阶段 2 |

**出口铁律**：任何新增 `raise StopResponse` 出口（或有意静默分支）必须
①`_reply_done_event.set()` ②有意静默时 `_cancel_response_watchdog()`。
漏①=等待中的 judge 挂满 15s 硬帽（`_JUDGE_REPLY_WAIT_S`）才放行；
lint 双向钉站点清单（新增/删除均红）。

---

## 3. 阶段 2：LLM 装配与守卫链

### 3.1 装配（agent.py 组装序）

| 步 | 动作 | 锚点 |
|----|------|------|
| a | 车道解析：`resolve_route("a_reply", …)` → provider/base_url/model/thinking | 3938；契约 `packages/core/bok_voice_core/model_routes.py` |
| b | 内芯构造：`MlxLlmLLM(**route_llm_kwargs(...))`（本地）/ DeepSeekLLM（显式云档） | 3957/3965/3982；livekit_plugins.py:389 |
| c | 抢跑：`PrefillSpeculator` 挂 `on_request_messages` + STT `stable_prefix_listener` | 3996-4010；prefill_speculator.py:131 |
| d | 包装链：`ContextAwareLLM(ExprAwareLLM(raw))` | 4011-4014；livekit_plugins.py:2851/3170 |
| e | 兜底注入打 **raw 内芯**：`set_fallback_text`（`_wire_llm_fallback` 1317）+ 晚到补答 `set_late_answer_cb`（4065）+ 垫话盖耳闸 `set_fallback_gate`（4386） | 包装层无这些方法（getattr 恒 None 的教训） |

### 3.2 chat 出口守卫链（内→外）

| 层 | 职责 | 锚点 |
|----|------|------|
| 请求侧 | 尾部渲染：`render_instruction_prefix`（2603，稳定段）+ `render_context_tail`（2713，增量段）；剥离 `[流程状态]` 标记；polished 副本替换 user 文本（原文单轨） | livekit_plugins.py:2876-3020 |
| `_LlmFallbackStream` | 首 token 截止（3s）+ 兜底直念 + drain/regen；W-ABORT req_id | 843；见 §4 |
| `_StripTailAnchorStream` | 剥尾部锚拟声复刻 + 数字顿号配对 | 1664；`_flush_at_end` 1748 |
| `_RepeatSelfGuardStream` | 出口复读防线：自我（last_reply）+ 跨轮账本；早发片段拼合纵深；号码守卫 | 1837；`_feed` 1949/`_flush_at_end` 2077 |
| `_PartialCaptureStream` | 已生成文本 tee（打断补账用） | 3108 |
| 消费侧 | `_on_conversation_item`（4749）→ `_consume_reply_ticket`（4720）→ `_report_assistant_turn`（4865） | 交付/账本/`_reply_done_event.set()` |

**前缀契约（KV-cache 铁律）**：稳定段（`_stable_stale_in_window` 窗口纪律、
`BOK_TAIL_SLIM`、`BOK_TAIL_MEMORY_EVERY`）与增量段分离；重试重建走
`tail_emit_stable_for_rebuild` 账本纯函数复现（逐字节复现，防 identical_skipped
假断裂）。改尾部渲染必须读 `ContextState` 账本三件套（`_applied_tails`/
`_applied_stable_keys`/`_memory_in_tails`）。

---

## 4. 阶段 3：首 token → 首音频

| 步 | 动作 | 锚点 | 时序假设 |
|----|------|------|----------|
| 3-1 | LLM 首 chunk 计时；超时（默认 3.0s）出兜底句但**不弃流**（drain 接手） | livekit_plugins.py:1096-1150 | W-ABORT 后弃流不再「无奈」：drain 是策略（原流慢但可能仍活） |
| 3-2 | drain 次级截止（默认 8s；饥荒档 15s）→ 真弃流 `_aclose_inner` + regen | `_late_answer_deadline_s` 827；`_drain_late_answer` 991 | regen 单次；`BOK_LLM_REGEN`；饥荒禁 regen（1083） |
| 3-3 | 晚到补答投递：`_late_answer_say`（4023）先 `_cancel_response_watchdog` + 去重（`_late_answer_dedup_verdict` 1290） | 4023-4130 | 补答走正常 speech 队列（客户可打断） |
| 3-4 | TTS 装配：MiniMax 主档 → FallbackAdapter(hd→turbo) → CachedTTS → `_FirstAudioTTS` 薄透传 | 3845-3932；tts_cache.py | 首音频监听口（`add_first_audio_listener`）在包装层透传 |
| 3-5 | 首音频三回调：垫话停播（`on_reply_first_audio`）／看门狗拆弹／墙钟 `BOK_TURN_TIMING` | agent.py:4440-4447（timing def 4427） | 回调经 provider 透传层，本地 Qwen3/volcano 链无此口→看门狗不武装 |
| 3-6 | 垫话：arm（500ms 定时器）→ 首条 out-of-band 播放 → 播完观察者 → reshot（pending 时间窗） | fillers.py:661（arm）/1151（`_fire`）/917（`_reshot_wait`） | 见 §8 常数表；hold=0 政策（首音频即停垫话） |
| 3-7 | 看门狗：6s 无首音频 → 在途顺延（synth ext 2s）→ force-interrupt + ack | agent.py:3513-3630 | 垫话开播顺延（filler ext ≥2s，按 hold 撑高）；第二发再顺延一次 |
| 3-8 | 交付：item_added → 票据消费 → 账本（gen/provider）+ 锚精修 + `_reply_done_event.set()` | 4749/4951/4865 | 打断路径（`_on_speech_created` 7721）单独 set + abandon 弃流 |

---

## 5. 等待链审计表（谁等谁、上限、饿死风险）

| 等待 | 主体 → 对象 | 上限/默认 | 前提与风险 |
|------|------------|-----------|------------|
| judge 回复让位 | 后台 judge → `_reply_done_event` | `_JUDGE_REPLY_WAIT_S=15s`（agent.py:1495） | 事件在每个交付/静默出口置位；超时照开火（判定不被饿死）。**契约待办（S1）**：帽 15→4s |
| judge 错峰窗 | judge → 链路空闲窗 | floor 1s / cap 3s（`_judge_yield_env` 1457；`_wait_link_idle` 1522） | 2026-10-02 复标：judge 与 reply 分端点，互斥消失只剩 GPU 错峰；capped 可跳过（`BOK_JUDGE_CAPPED_SKIP`，仅 thinking 时） |
| 首 token 截止 | 回复流 → 第一块 chunk | 3.0s（`LLM_FIRST_TOKEN_TIMEOUT_S`） | 饥荒档拉长到 15s（EMA≥4s 判定） |
| drain 次级截止 | drain → 原流产出 | 8s（饥荒 15s） | 无产出才真弃流+regen |
| 传输 read-gap | httpx → 字节流 | 22s（`LLM_REQUEST_TIMEOUT_S`） | 粗后盾；冷 prefill p95 18.9s，8s 会误杀慢流 |
| 看门狗 | 轮提交 → 首音频 | 6s（`BOK_RESPONSE_WATCHDOG_S`） | 在途合成顺延 +2s；垫话开播按 hold 撑高 |
| 结算 gather | `_on_close` → `_report_tasks` | 10s 起，按慢任务 deadline 分档 +5s（`_settle_wait_s` 2530） | 晚到补答/judge 落账不丢 |
| 收线延迟 | farewell → end_call | 14s（`_schedule_call_end` 5215） | shutdown 回调兜底补发（`_end_on_shutdown` 5254） |
| WA/数字累积 flush | 碎片暂存 → 下段/超时 | 5s（`BOK_WA_ACCUM_TIMEOUT_S`） | 超时 flush 合并成一轮 |
| bidi 收尾 | 取消/收摊 → 服务端 ack | cancel 3s / 首音频 6s（livekit_plugins.py:5249/5256） | 卡死自愈 `_stall_watch`（BOK_MINIMAX_BIDI_STALL_MAX_HEALS=2） |
| ASR finish 锁 | finish → 在飞 partial | 1.0s（sidecar app.py:1109） | 锁忙=整段重解（EOU 抖动）；0.3→1.0 是换稳定 |
| 句级提交限速 | 提交 → 上一次提交 | 1.5s（`_Qwen3ASRLiveStream`） | 防连发；门含数字/字母连写豁免 |

---

## 6. fire-and-forget 任务全谱表（含各自时序假设）

| 任务 | 创建点 | 语义 | 时序假设/风险 |
|------|--------|------|----------------|
| `_watchdog_fire` | agent.py:3645（arm）/3675（顺延） | 6s 无首音频→ack+force-interrupt | 持 `_watchdog["task"]` 强引用；disarmed 旗拆弹 |
| `_arm_wa_accum_flush` | 3435 | 号码碎片 5s 超时 flush | 新碎片重置定时器；flush 走 `_register_reply_lane("wa-flush")` |
| `_arm_digit_accum_flush` | 3488 | 单号碎片超时 flush | 同上（digit-flush） |
| `_fire`（垫话） | fillers.py:674 | 500ms 后垫话开火 | 真回复首音频/新轮 cancel 作废；guards 复核 |
| `_chain_wait`（播完观察者） | fillers.py:875 | 首条播完→reshot/链发 | `wait_for_playout` 精确唤醒；cancel 连 gap 段一起取消 |
| `_backfill_safe`（垫话物化） | fillers.py:1332（spawn）/1336（def） | miss 后异步云合成入缓存 | 下一通起命中；失败零影响 |
| `_supervisor_watch` | agent.py:7868 | 主管接管/打铃状态轮询 | 随通话生命周期；关闭即退 |
| `_nudge` 定时器 | 7663 | 12s 静默心跳→两轮无应→no_response 收线 | 注册必须先于 session.start（首段静默依赖） |
| `_spawn_report`（REPORT 池） | 4849；消费 `_report_tasks` | 摘要/结算/晚到补答等落账 | `_close` gather 有界等待（§5）；强引用池防 GC |
| `_SETTLE_TASKS`（收线池） | 5191/5250 | delayed end_call / close flush | 不被 gather 干等；shutdown 兜底 |
| `_duration_fuse` | 3230 | 通话时长熔断（900s） | 独立 fuse，超时收线 |
| `_drain_late_answer` | livekit_plugins.py:1135/1142 | 首 token 超时后原流续读 | 首 chunk 任务绝不 cancel（tee_peer 对 cancellation 不免疫） |
| `_regen_late_answer` | 1083 | drain 无产出→同参二发 | 单次；饥荒禁；`_abandoned` 熔断 |
| `_ping_loop`/`_prewarm_run`（bidi） | 5006/5141 | WS 保活/预热复用 | 长连接生命周期；失效重连 |
| `_hold_flush`（ASR join-hold） | livekit_plugins.py:7597/7640 | 续段超时→正常停嘴路径 | 凭 `_session_epoch` 识别「已续讲」不 reset |
| `_minimax_pool_replenish` | 3988/4039 | classic 连接池取用即补 | TTL 240s；陈旧弃置就地回退 |
| `_prefill_spec._fire` | prefill_speculator.py:131 | 说话窗抢跑 prefill（max_tokens=1） | 前缀快照一致才发；revision 门防 divergent |

---

## 7. 车道表（`_REPLY_LANES` 27 条，agent.py:1256）

所有车道出声前过 `_register_reply_lane`（agent.py:4640）：推 TurnTicket（FIFO，
`_consume_reply_ticket` 4720 配对消费）/ 预写重复锚 / 拆看门狗 / stall 抵销
（`relieve` 缺省 = gen ∈ {llm, qa_fastpath}）。`notify=True`=无自有出声（provider
顺延到下一个 LLM item）；`history=False`=纯 ack 不进 chat_ctx、手工补账
（`_ledger_ack_line` 4705）。

| 车道 | 触发 | 注册点 | 备注 |
|------|------|--------|------|
| opening | 会话开场白 | 7930 | 首条直念 |
| wa-flush | WA 碎片累积超时 flush | 3430 | 合并轮 provider=wa-merged |
| digit-flush | 单号碎片累积 flush | 3483 | 同上（digit-merged） |
| watchdog-ack | 看门狗 6s 无首音频 | 3622 | history=False |
| late-answer | drain/regen 晚到真答案 | 4060 | gen=llm；去重后投递 |
| starve-ack | 连续 2 轮零输出短承接 | 5952/6010 | history=False |
| storm-ack | 打断风暴静听期短承接 | 7773（watcher） | history=False |
| branch-refuse | 分支【收线】直念 | 6353 | 最高优先级 |
| branch-notify | 分支打铃（无出声） | 6386 | notify=True |
| branch-jump | 分支跳步（无出声） | 6410 | notify=True |
| stall-degrade/bypass/close | stall 阶梯三级 | 6587（`lane=f"stall-{_lvl}"`） | 同级不连发 |
| defer-ack | verdict==DEFER 短应承 | 6612 | |
| flow-say | 待念直念步（say:1） | 6656 | 缓存线零 TTFT |
| qa-fastpath | QA 词面/语义命中 | 6746（`lane=provider`） | gen=qa_fastpath；relieve=False（防双扣） |
| graph-play | 图意图 play_qa | 6983（`_qa_canned_say(provider="graph-play")`） | 罐头播放 |
| graph-jump | 图意图跳步（无出声） | 6938 | notify=True |
| graph-notify | 图意图打铃（无出声） | 6954 | notify=True |
| branch-canned | 分支罐头计划命中 | 7110 | |
| farewell | 收线告别 | 7639 | 置告别窗旗 |
| nudge | 静默心跳 | 7654 | history=False, anchor=False |
| followup-ack | 工具追问短应接 | 5471 | history=False |
| pause-ack | 暂停确认（off-band 音轨） | 7569 | notify 语义（无 speech item） |
| fallback-ack | LLM 兜底句（流内发出） | `_report_assistant_turn` 4878 归位 | 退出「实答」判定面（不抵销 stall） |
| garbled-reask | 烂转写罐头重问 | 7486 | gen=script, history=False；cap=2 |
| wa-confirm | WA 直捕罐头确认 | 6214 | gen=script, anchor=False |

新增车道三步：① 入 `_REPLY_LANES` ② 出声前 `_register_reply_lane` ③ 有静默出口
则置 `_reply_done_event`（lint 会红）。AST lint：
`tests/test_reply_chokepoint_lint.py`（`set_last_reply`/`_cancel_response_watchdog`/
`repeat_requested`/`_reply_done_event.set` 四类白名单）。

---

## 8. 常数表（值 → 补偿对象 → 前提状态）

| 常数 | 默认 | 补偿/约束对象 | 前提状态（失配后果） |
|------|------|---------------|----------------------|
| `LLM_FIRST_TOKEN_TIMEOUT_S` | 3.0 | 首 token 慢（健康 p95 0.2-0.6s） | 垫话盖 0.5-2.3s；健康档下 3s 兜底句是第二层盖耳（设计内双声） |
| `LLM_LATE_ANSWER_DEADLINE_S` | 8 | drain 原流续读窗 | 4B 出满答案远快于此；饥荒档 max(8,15) |
| `LLM_REQUEST_TIMEOUT_S` | 22 | 传输 read-gap 粗后盾 | 冷 prefill p95 18.9s；<18.9 会把慢误杀成死 |
| `LLM_REQUEST_RETRIES` | 0 | 官方 3×10s 静默 | 兜底壳取代重试做恢复 |
| `BOK_RESPONSE_WATCHDOG_S` | 6 | commit→首声 p95 5.66s | 4s 档 237 火灾/212 ack（慢而未死被收割） |
| `BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S` | 2 | TTS 流已开未出声 | 一次性顺延；kill-switch=0 二判据同灭 |
| `BOK_RESPONSE_WATCHDOG_FILLER_EXT_S` | 2（按 hold 撑高 +1.5） | 垫话盖耳期 | 垫话 out-of-band 框架不可见 |
| `_JUDGE_REPLY_WAIT_S` | 15 | judge 让位帽 | 回复 p95 <6s；**S1 契约待办 15→4** |
| `FLOW_JUDGE_DELAY`/`IDLE_CAP` | 1 / 3 | judge 错峰 floor/cap | 分端点后只剩 GPU 错峰；饥荒 overlay 调回 3/6 |
| `RESHOT_MIN_ELAPSED_S` | 2.2 | reshot 载荷轮判据 | 垫话 0.5 起播+~1.5 播完+gap；正常轮 elapsed≈2.0 |
| `RESHOT_PENDING_GRACE_S` | 2.0 | reshot pending 时间窗 | <2s=首音频将至让路；>2s=真卡顿放行补发 |
| `HOLD_LOG_MIN_LEGACY_S` | 0.5 | hold=0 观测行降频 | 省下等待 <500ms 不值得记 |
| `BOK_FILLER_DELAY_MS`/`MAX`/`COOLDOWN_S` | 500 / 6 / 10 | 垫话懒触发/上限/防连发 | 真实轮间隔 >10s=慢轮全覆盖 |
| `BOK_TTS_FIRST_CHUNK_CHARS` | 6 | 首段早发门槛 | 数字/拉丁 run 不劈；句界 N+6 容差让位 |
| `BOK_REPEAT_HEAD_MAX_HOLD` | 22 | D1 有界持有 | 无句界长首句防零句死；换头主体由拼合纵深剥 |
| `BOK_REPEAT_CROSS_TURN_SIM` | 0.85 | 跨轮整段滚动阈值 | 单句比对 0.9；reask 放行豁免 |
| `BOK_TAIL_STABLE_SPAN` / `BOK_TAIL_MEMORY_EVERY` / `BOK_MEMORY_CHARS` | 0（=max(2,HISTORY-2)）/ 3 / 180 | 尾部节食（uncached 字节） | 前缀缓存严格前缀；改渲染必读账本三件套 |
| `BOK_FILLER_MATCH_THRESHOLD` | 0.42 | 垫话罐头匹配 | 垫话有双层兜底，宁 hit 勿 miss |
| `QWEN3_ASR_FINISH_LOCK_WAIT` | 1.0 | ASR finish 锁等待 | <1.0=增量捷径丢失整段重解（EOU 抖） |
| `BOK_VAD_PROVIDER` + `vad.min_silence` | silero / 0.28 | EOU 静音确认窗 | 0.28 生产档（设置面 vad 段，非 asr 段）；smart-turn 粤语恒关 |
| `BOK_MAX_CALL_DURATION_S` | 900 | 通话时长熔断 | `_duration_fuse` |
| `BOK_WORKER_LOAD_THRESHOLD` | 0.99 | worker 派单负载闸 | 十七波根修（整机 CPU 误拒派） |

---

## 9. 观测行速查（排障入口）

| 症状 | 先看 |
|------|------|
| 慢轮 | `BOK_TURN_TIMING commit_to_audio=` / `PERCEIVED_MS` / `LLM_TTFT_MS` / `MINIMAX_TTS_BIDI_PERF` |
| 复读/乱回 | `REPEAT_SELF_SUPPRESSED` / `REPEAT_CROSS_TURN_SUPPRESSED` / `REPEAT_CROSS_TURN_EMPTY` |
| 卡死/哑轮 | `LLM_FIRST_TOKEN_TIMEOUT` / `LLM_FALLBACK_TEXT` / `LLM_LATE_ANSWER source=drain|regen` / `REPEAT_GUARD_CANCEL_DROP` |
| 垫话行为 | `BOK_FILLER fired|reshot fired` / `FILLER_YIELD stopped|hold=0|reshot` / `BOK_FILLER reshot skip reason=` |
| judge 让路 | `FLOW_JUDGE deferred reply_ms=` / `[judge] yield idle|capped|disabled` / `judge_pending_expired` |
| ASR 面 | `QWEN3_ASR_TEXT`（含 conf）/ `QWEN3_ASR_SENTENCE_COMMIT` / `QWEN3_ASR_REDECODE_DROP` / `QWEN3_HOTWORD_ECHO_STRIP|DROP` |
| 打断面 | `interrupted reply ledgered` / `interrupted stream abandoned` / `[storm] engage|listening` |
| 流程面 | `FLOW_JUDGE` / `FLOW_GRAPH` / `CANNED` / `GARBLED_REASK lane=1` |

---

## 10. 变更纪律（活文档维护）

1. 动 §2 段序 / §7 车道 → 同步改本图 + 跑 `tests/test_reply_chokepoint_lint.py`。
2. 动 §3 请求渲染 → 跑前缀契约相关测试（`test_tail_diet` / `test_prompt_surgery2`
   / `test_llm_metrics_forward`）。
3. 动 §4 首 token/drain → 跑 `test_llm_fallback` / `test_tts_first_chunk` /
   `test_bidi_head_flush`。
4. 动 §1 ASR → 跑 `test_asr_confidence` / `test_asr_polish_wiring` / E2E 探针。
5. 新 env 旋钮 → 必须入 `tools/bok.py` `_FORWARD_ENV`（prod 封闭 env 死键教训），
   并补进 §8。
