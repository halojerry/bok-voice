# 延迟预算与超时政策（LATENCY_BUDGETS）

全栈「等几耐、等唔到点算」的单一事实源。**任何超时/等待默认值的变更必须同步本表**，
并跑对应探针复测（见文末「测量工具」）。定档日期 2026-09-17（探底=agent-2/agent-3 盘点 +
probe_latency_soak 基线）。

## 1. 北极星与口径

- **PERCEIVED_MS（北极星）**：客户讲完 → AI 出声 = `eou + llm_ttft + tts_ttfb` 三段
  （官方 metrics 语义）。日志 `PERCEIVED_MS total=… (eou= llm= tts=)`，逐轮落
  `turns.perceived_ms`。旁路轮（脚本直念/QA 快路/垫话）冇全三段，不计。
- **墙钟首声**：探针口径「推完客户音频 → 听到 AI 首声」，含 VAD 端点等待+网络+播放路径，
  恒 ≥ PERCEIVED_MS。两口径同轮对照先可以定位「慢喺链内定链外」。
- **预算线**（暖轮）：`PERCEIVED_MS ≤ 3000ms`；探针墙钟首声 `≤ 2500ms`。
  超标哨兵：`PERCEIVED_BUDGET_EXCEEDED`（`BOK_PERCEIVED_BUDGET_MS`，默认 3000，0=关）。

## 2. 客户心理线（真实电话客服口径，定档依据）

| 客户等待 | 体感 | 政策要求 |
|---|---|---|
| ≤0.5s | 即时 | 垫话兜底线（`BOK_FILLER_DELAY_MS=500`） |
| ≤1.5s | 流畅 | 罐头/QA 快路应达 |
| ≤2.5-3s | 可接受 | 暖轮 PERCEIVED 预算线 |
| 3-5s | 迟钝感 | 必须有垫话/链发垫住 |
| 5-7s | 疑似断线 | 必须有「在帮你查」类补位（LLM 兜底直念 / bidi 看门狗） |
| 8s+ | 大量客户放弃 | 心跳/收线线（见 §3） |
| 10s+ | 挂机高发 | 硬上限：任何静默路径不得跨过 |

## 3. 全栈超时/等待清单

### 3.1 感知链（ASR → LLM → TTS）

| 机制 | env | 默认 | 触发行为 |
|---|---|---|---|
| **LLM 首 token 截止** | `LLM_FIRST_TOKEN_TIMEOUT_S` | **2.0s**（客服口径拍板「还是太松」后由 2.5 收紧；0=关） | 第一块 ChatChunk 计时，超时 → 立即三语兜底直念 + **原流后台续读（drain）晚到补答**（`LLM_FIRST_TOKEN_TIMEOUT`/`LLM_LATE_ANSWER source=drain` 哨兵）。TTFT 预算必须在 ChatChunk 层计——SSE keepalive 会喂 httpx 字节，传输层 read 超时计唔准 TTFT。**计时用 `asyncio.wait({task})` 唔准 `wait_for(__anext__())`**——超时 cancel 会杀 livekit `tee_peer`，原流后续 chunk 结构性丢失（2026-09-17 实测教训） |
| **晚到答案 drain** | `LLM_LATE_ANSWER_DEADLINE_S` | 8.0s（0=kill-switch 回「立即 aclose+regen」旧行为） | 兜底句说出后**唔弃流**：原请求后台继续读到截止，首个真 chunk 即晚到答案（`source=drain`，省二发请求、零新僵尸解码——mlx 服务端无断连中止，弃流重生会令二发排在僵尸后面拖高后续轮 TTFT，2026-09-17 offscript 实测 [watchdog] 后紧跟 TTFT 3872ms）；截止仍无产出/内芯异常 → 才 aclose+factory regen 最后手段（`source=regen`）。部分已生成文本优先 salvage 交付 |
| **弃流重生（最后手段）** | `BOK_LLM_REGEN` | 开 | drain 失败后同参重发一次，成功经 speech 队列补答（`provider=late-answer`，客户插话可打断）；二发失败即止唔追 |
| LLM 主回复重试 | `LLM_REQUEST_RETRIES` | **0** | 插件级重试链归零（官方默认 3×10s=最坏 46s 静默）；恢复交给兜底壳+重生——更快且有声。重试等 8s=这通电话已废（2026-09-17 用户拍板） |
| LLM 传输层 read-gap | `LLM_REQUEST_TIMEOUT_S` | 8s | 字节流死流的粗后盾（首包后流中卡死兜底；TTFT 闸在上一行） |
| LLM 兜底直念 | `BOK_LLM_FALLBACK` | 开 | 上述两闸触发 → 按通话语言单句「在帮你查」直念（`LLM_FALLBACK_TEXT` 哨兵）；=0 整闸关回旧行为 |
| **响应看门狗** | `BOK_RESPONSE_WATCHDOG_S` | 4s（0=关） | 轮提交后 N 秒零 assistant 音频（任何原因：生成从未启动/TTS 死火/钩子静默失败/垫话配额耗尽）→ force-interrupt 清队列 + 三语兜底直念 + 账本 `provider=watchdog-ack`。钩子顶端武装，有意静默分支（回声/暂停/风暴/暂存）与首音频回调拆弹；非 CachedTTS 通路不武装 |
| 看门狗垫话顺延 | `BOK_RESPONSE_WATCHDOG_FILLER_EXT_S` | 2.0s（0=关） | 垫话开播（out-of-band，不触发首声回调）→ 看门狗截止一次性顺延「剩余+2s」——垫话已盖耳，唔算全哑（2026-09-17 RC3：垫话+兜底+晚到答案三连道歉根因之一）。每轮只顺延一次，拆弹后唔再生效 |
| LLM judge 快路 | — | 5s×1 | 失败返空（不推进） |
| LLM 前缀预热 | — | read=30s | fire-and-forget，失败吞 |
| TTS classic 首包看门狗 | `MINIMAX_FIRST_AUDIO_TIMEOUT_S` | 4s | `MINIMAX_TTS_STALL` 断连重连+重发已发文本（一流一次） |
| TTS bidi 首包看门狗 | `MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S` | 6s | `MINIMAX_TTS_BIDI_STALL` 弃连重连+合并重发 |
| bidi 看门狗自愈上限 | `MINIMAX_BIDI_STALL_MAX_HEALS` | 2 | 2026-09-17 由 1 放宽：二次僵死再救一次，超限落回 recv fail-fast（防重连风暴） |
| bidi ping 探活 | `MINIMAX_BIDI_PING_S`/`_PING_MAX_MISS` | 60s/2 | 连失 2 → `MINIMAX_TTS_BIDI_DEAD` 强断+重预热 |
| ASR 跨段拼接 hold | `QWEN3_ASR_JOIN_HOLD_MS` | 800ms | 超时 flush 照常出（该轮多等 ≤hold） |
| ASR partial 滑窗 | `QWEN3_ASR_PARTIAL_MS` | 700ms | 说话中节拍 |
| ASR partial 抑制档 | `BOK_ASR_PARTIAL_SLOW_MS` | 3000ms | thinking/speaking 期（GPU 竞态） |
| ASR finish HTTP | — | 30s | 流式失败 `QWEN3_ASR_FINISH_ERROR` 丢转写（该轮白说，P2 待兜底） |

### 3.2 轮次与打断

| 机制 | env | 默认 | 触发行为 |
|---|---|---|---|
| 真插话让位 | `INTERRUPT_MIN_DURATION` | 0.6s | ≥0.6s 客户语音=真打断 |
| 误打断自愈 | `FALSE_INTERRUPTION_TIMEOUT` | 1.0s | 打断后 1s 无转写=噪声，AI 从暂停处续讲 |
| **连环打断风暴退避** | `BOK_INTERRUPT_STORM_BACKOFF`（窗口 `…_WINDOW_S`=20 / 阈值 `…_THRESHOLD`=3 / 静听 `…_QUIET_S`=8） | 开 | 20s 内打断 ≥3 → 直念让路语一次 + 静听（零回复），客户停嘴安静 8s 恢复；打断轮补记 `gen=interrupted`（`BOK_INTERRUPT_LEDGER=0` 关补账） |
| 饿死兜底 | `BOK_STARVE_ACK` | 连续 2 轮零回复 | 第 3 轮短承接直念 |
| WA 号码碎片累积 | `BOK_WA_ACCUMULATE`（超时 `BOK_WA_ACCUM_TIMEOUT_S`=5s） | 开 | 收号码步半截句暂存攒齐 |
| **通用单号累积** | `BOK_DIGIT_ACCUMULATE` | 开 | 非 WA 步单号句拆段同姿势；flush ≥4 位落 call fact+确认，<4 位请重报 |

### 3.3 沉默与收线

| 机制 | env | 默认 | 触发行为 |
|---|---|---|---|
| 沉默心跳 | `SILENCE_NUDGE_SECONDS` | 8s | AI 讲完转 listening 后客户无声 → 「仲喺度嗎」（脚本直念） |
| 心跳护栏 | — | ≤2×delay(16s) | 客户刚说完且 ≤16s 不开火（答案在途窗）。已知代价：LLM/TTS 全卡死时最坏 16s 无补位——由 §3.1 兜底直念/看门狗把「答案卡死」收敛到 ≤18s 内出声，护栏保持不动 |
| 心跳次数 | `SILENCE_NUDGE_MAX` | 2 | 两次后 farewell + 自动收线 |
| 自动收线 | — | farewell 后 12s | `disposition=no_response` |
| 拒绝/道别收线 | — | 14s / 8s | declined / scheduled·polite_close |
| **通话时长保险丝** | dial 块 `max_call_duration_s` > `BOK_MAX_CALL_DURATION_S`（默认 900s，0=关） | 900s | 2026-09-17 扩 real 档：dial 块缺键时本地计时器兜底收线（此前 real 档无上限） |
| 振铃超时 | settings `sip.ringing_timeout_s` | 30s | no_answer |
| CP reaper | — | RINGING 600s | 空房 → ENDED/abandoned |

### 3.4 基础设施

| 机制 | env | 默认 |
|---|---|---|
| VAD 端点 | `ENDPOINT_MIN_DELAY`/`ENDPOINT_MAX_DELAY`/min_silence | 0.25/0.6/0.45（kill-switch 配对见 AGENTS.md「VAD/endpointing 基线」） |
| B 线译文积压 | `BOK_INTERP_MAX_BACKLOG_S` | 6s（队头/最新不弃） |
| CP 请求 | — | httpx 15s 无重试 |
| 结算 flush | — | 12s 上限 |
| mlx_lm server | — | 无请求 deadline（架构现状；被作废请求解码到完才放锁，PrefillSpeculator busy 门缓解） |
| Qwen3 本地 TTS | — | HTTP 120s×3、整段无音频 beep（**无首包看门狗**，P2 待补） |

## 4. 定档取舍记录（评估结论，2026-09-17）

- **LLM 主回复（原 timeout=10s×retry=3，最坏 ~46s 静默零兜底）→ 首 token 截止 2.0s + 重试归零 + 兜底直念 + 弃流重生**：
  客服口径两轮拍板（2026-09-17）——「等 8s 再重试，这通电话已经废了」→「2.5 还是太松」。
  最坏可闻时间 ≈ 2.0s（弃流）+TTS ≈ 2.6s，垫话盖 0.5-2.3s 无缝衔接；被切掉的真答案
  由后台单次重生补答（晚到好过冇）。响应看门狗 4s 兜「生成从未启动/TTS 死火」类
  无音频路径（run-5 轮9 33s 死寂实证：垫话 6 次配额耗尽后零兜底）。
- **心跳 8s 保持**：3.5-4s 时代实测「一直心跳」压死客户思考窗（agent.py 注释档案）；
  真正的洞是「答案在途护栏最坏 16s」——由 B1 兜底直念补上，不动护栏（改小会复现
  心跳顶替真答案）。
- **bidi 看门狗 6s 保持、自愈 1→2**：6s 触发=客户已听 6s 静默，已在疑断线区；但
  服务端攒句首包天然慢，压到 4s 会误杀正常慢轮。二次自愈是把「第二次僵死干等 30s」
  的尾砍掉。
- **回声守卫 <6 字盲区（P2 备忘）**：AI speaking 中的 <6 字回声残片可成轮自打断
  （`_ECHO_MIN_CHARS=6`）。**唔好**靠降阈值修——「拼多多/京东」类 2-3 字真答案会被
  误杀；现有误打断自愈(1s)+热词回声闸已遮大半，出现实证案例再议 vocab-aware 路径。
- **ASR 流式 finish 无重试（P2 备忘）**：`QWEN3_ASR_FINISH_ERROR` 直接丢转写，
  客户该轮白说。待补：finish 失败一次性重试 + 垫话位补「您再讲一次」。

## 5. B 线延迟预算按姿势分档（2026-10-09 interp-lite 波定档）

同一条 `probe_interpret_latency`（逐句「说完→出声」口径，`BOK_PROBE_LAG_BUDGET_MS`）
按 **MT 车道姿势**分两档——预算是 MT 腿的函数，不是常数：

| 姿势 | MT 腿 | 预算 | 定档依据（实弹） |
|---|---|---|---|
| 本地 MT2（:1236） | p50 ~260ms（15 字句） | **3500ms**（缺省不变） | 2026-10-05 真栈 avg 2223 |
| 全云 DeepSeek（mt 车道 openai） | 首 token p50 ~600ms、尾 2-6s | **4200ms**（`BOK_PROBE_LAG_BUDGET_MS=4200`） | 2026-10-09 三臂：interp-lite 3572/3786 vs 旧线 3835（同姿势 A/B 持平）；3500 档下两线同超=姿势成本非线损 |

注意（口径边界）：边说边译模式（B 线句档/子句提交开启）下该探针的 onset 锚在
「src_end 之后」而译声常已在播——量边说边译用 `probe_interp_continuous`（硬定义
onset）与 `probe_interp_fluency`（天窗/段时长/提交单元），不用本表预算判死活。
perceived 含整段播完时长，与「单段 ≥4s」目标内部矛盾（W6 §5 口径修正），只报告不设门。

## 5a. 业界级联 SLO 对照（2026-10-09 W8-A2 锚，R3 调研定标）

业界语音级联管线（cascaded pipeline）公开 SLO 口径，作为 B 线逐腿预算的外部参照系；
**本表是对照镜不是新预算线**——B 线预算仍按 §5 分档执行：

| 腿 | 业界 SLO | B 线现状（2026-10-09 实测口径） | 对账 |
|---|---|---|---|
| MT TTFT（翻译腿首 token） | **<250ms** | 本地 MT2（:1236）p50 ~260ms ✅ 达标；DeepSeek 云档（现役 mt 车道）TTFT p50 **735ms**（probe_mt_lane_ttft 三臂 24 请求/臂，2026-10-09） | 云档超标 ~3 倍——换道不涨速定案=DeepSeek 留任（粤译最地道+124tok/s），达标靠 spec HIT 把 MT 腿藏进说话期，不靠压 TTFT |
| TTS TTFB（合成首包） | **<300ms** | MiniMax bidi 420-870ms（头段催产 flush 后服务端地板 210-343ms+传输/前导修剪） | 剩余大头之一；进一步压=换供应商/边缘部署级，非当前刀位 |
| e2e（说完→出声） | **<2.5s** | 延迟探针预算 §5 分档 3500/4200ms；本地 MT2 档实测 avg ~2.2s ✅ | 预算线松于业界（方言对+本地硬件口径）；子句快路径感知 1.2-1.4s 已进实时带 |
| 实时感带 | **<700ms** | 常规路径结构性够不着（ASR 提交+MT+TTS 三腿物理和） | **唯一路径=spec HIT**（interim 投机翻译 prewarm-and-confirm：final 到=held PCM 零合成直播，mt_ms=0）——spec 命中即 «final 即声»，是全线唯一进 700ms 带的通道 |

结论（锚定用）：逐腿追 SLO 不是路径——MT/TTS 腿都在物理地板附近；**结构性杠杆只有
两个**：①提交闸提前（子句/意群档，已做）②spec 命中率（含 probe_mt_firstplay 量
「首可播块」分布，见 §6）。台架测 MT 首可播块用 `scripts/probes/probe_mt_firstplay.py`。

首跑读数（2026-10-09，DeepSeek 云档 6 句×两向×2 轮，reports/w8a 本地存档）：**MT 首可播块
p50 866ms / p90 1037ms**（逐句 p50 487-1009ms）——远超 MT TTFT<250ms SLO（首个
delta≠可合成块，凑齐分句才是 TTS 解锁点）；spec 仿真开火面=带逗号子句句（2400-4399ms
自源语起算），无逗号短句结构性零开火（只走 final 路）。**印证 <700ms 带唯一路径=spec
HIT**：spec 路出声（开火+首可播）早于源语结束的长句，final 到=held PCM 直播即声。

## 5b. 晚峰备胎道（seed-2.1-lite，2026-10-09 P5 定观察位）

**定位**：DeepSeek 留任主道（粤译最地道）；火山方舟 `doubao-seed-2-1-lite-260915`
为**晚峰（18:00-22:00）备胎观察位**——TTFT 1533/1981ms（probe_mt_lane_ttft 三臂）、
粤译质量追平 DeepSeek；晚峰 DeepSeek 若劣化（TTFT p90 >2s 或超时率上升）按下面
流程换道。方舟裸名 404（Ark 只认带日期版本 ID），`ARK_MODEL` 缺省已钉全名。

**预置落库步骤（CP API，代码零改；密钥 env 注入勿入码）**：

```bash
# ① mt 车道改指方舟（api_key 传 env 占位；CP 掩码面，明文只在设置库）
curl -X PUT "$CP/api/model-routing" -H "Authorization: Bearer $BOK_CP_TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "lanes": {"mt": {"provider": "openai",
                     "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                     "model": "doubao-seed-2-1-lite-260915",
                     "api_key": "$ARK_API_KEY"}}}'   # ← 由 curl env 展开注入，绝不写入本文件
# ② 快照存名（不含密钥；套档不清密钥）
curl -X POST "$CP/api/model-routing/presets" -H "Authorization: Bearer $BOK_CP_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name": "mt-evening-ark"}'
```

**晚峰双道终判流程（18:00-22:00 窗）**：
1. 主道（DeepSeek）与备胎（mt-evening-ark）各跑一轮 `probe_mt_lane_ttft --only deepseek,ark`
   + `probe_interpret_latency`（`BOK_PROBE_LAG_BUDGET_MS=4200` 云档）；
2. 终判判据：TTFT p50/p90 + e2e 延迟探针 PASS + 粤译眼测（唔标志记数）三对账；
3. DeepSeek 恶化 → `POST /api/model-routing/presets/mt-evening-ark/apply` 换道
   （下一通生效零重启）；峰后 `POST /api/model-routing/presets/cloud-deepseek/apply` 回切；
4. 结论记回本节（数字+日期），连败两窗则把备胎转正评估提级。

## 6. 测量工具

- `scripts/probes/probe_latency_soak.py`：多轮多样话术延迟测试台（逐轮墙钟首声 + eou/llm/tts
  三段 + 拆轮/打断/哑轮异常旗 + p50/p95 汇总 + JSON 报告）。改延迟相关代码后必跑。
- `scripts/probes/probe_mt_firstplay.py`：DeepSeek 流式 MT「首可播块」台架（2026-10-09
  W8-A2）——首个可合成分句（句末标点或 ≥12 字逗号界）墙钟分布 × 6 句题集，对照列=
  spec 稳定判据仿真触发时刻；§5a 的 MT 腿与 <700ms 带 spec 可行性测量仪器。
- `scripts/bench/measure_latency.py`：直打三 sidecar 的分段延迟（无 LiveKit）。
- `scripts/probes/probe_filler_timing.py`：垫话/首声预算（首声 <2.5s 判据）。
- `scripts/ops/llm_cache_report.py <worker.log>`：KV 命中与 TTFT 分布。
- 日志哨兵速查：`PERCEIVED_MS` / `PERCEIVED_BUDGET_EXCEEDED` / `LLM_FALLBACK_TEXT` /
  `MINIMAX_TTS_BIDI_STALL` / `[storm]` / `[digit-accum]` / `interrupted reply ledgered`。
