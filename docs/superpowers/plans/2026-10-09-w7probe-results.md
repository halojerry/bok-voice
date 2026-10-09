# W7-P 探针结果（2026-10-09，阶段一可行性判读）

三轨并行 subagent 真链路实弹（worktree session-20261009-091609-w7probe），P4（官方 AgentSession 真形）进行中。原始数字另存 `reports/w7probe/`（gitignore）。

## P1 MiniMax bidi 官方攒句 —— GREEN

探针 `probe_minimax_bidi_native.py`（speech-2.8-hd + 产品 zh 默认音色 + pcm/24k）：

| 场景 | 数字 | 判定 |
|---|---|---|
| A 逐字直喂（1-3 字片×100ms）末字→首音频 | 294-699ms 典型（个别轮 1076/1115） | 门槛 ≤400ms 达成 |
| A 碎裂 | 三轮复跑零 <1.5s 短句、零 >1s 块间隔；音频总量=非流式 HTTP 对照同量级 | PASS |
| A word_streaming 字幕 | 每条消息重发本句累积词表、句内时间戳严格递增 | 可用 |
| B task_flush 催尾 | 干等 2604ms（~2.6s 空闲兜底窗）vs flush 408-512ms | **flush 省 ~2.1s** |
| C task_cancel 存活 | canceled 回包 456-1080ms；之后 task_continue 继续出完整音频 | PASS |
| D ws ping/pong | RTT 122-261ms | PASS |
| E 早切对照 | 旧「前 6 字早切+flush」头段音频恒为 1.03-1.25s **韵律断裂碎句** | 早切形状劣于官方攒句 |

**附带侦查（产品 impl）**：`_MiniMaxBidiSession._ping_loop` 缺省 60s ping（pong 10s 超时/连失 2 强断重预热）——**无 120s 静默掉线隐患**；2205 双形态（非 task_failed=0.2s 原样重发不重连 / task_failed 携带=限流守卫熔断）姿势正确。

## P2 豆包 SAUC 官方臂 —— NUANCED GREEN（一金子发现）

探针 `probe_doubao_native_arms.py`（resource=volc.seedasr.sauc.duration，语料 corpus zh）：

**S1 判停窗（静音起点→definite final）**：

| 臂 | 延迟均值 |
|---|---|
| **缺省（不设参数）** | **3044ms ——文档写 800，实测 ~3s！** |
| end_window_size=500 | **549ms** |
| =800 | 915ms |
| =1000 | 1133ms |

显式设置时 1:1 跟踪（开销 25-190ms）；全程静音前零成句。**金子**：SAUC 缺省判停 3s 是「SAUC final 一直没法当轮界」的根因；显式 w=500 即得 549ms 官方轮界。

其余臂：**nonstream 二遍**=干净语料零成本 no-op（CER 0.0 双臂、零回退替换）；**ddc**=引擎缺省已折叠 3× 结巴音频（ddc 零增量→客户端 `_fold_stutter` 对该端点属多余件）；**force_to_speech_time**=句头完整率双臂 4/4（句头问题在客户端侧，该参数无收益无害）；**result_type=single**=该资源不生效（仍是全量累积，客户端去重不可省）。

## P3 DeepSeek 裸链 —— GREEN

探针 `probe_interp_official_chain.py`（mt 车道 api.deepseek.com deepseek-flash thinking-off；bidi 预连后 t0=发 LLM 请求）：

- **C2 链路 EVS（LLM 流式→逐 token 直发 bidi→首音频）**：zh→en 1156/1188/1428ms；zh→cantonese **821/925/1259ms**——6/6 ≤1500ms（对照：现碎片口径 perceived p50 4027ms）。
- C1 token 节奏：间隔 p90 ≤63ms、整条 <1s 喂完——攒句不饿；单句 utterance 全部 sentences=1 零碎片。
- C3 前缀预热：本尺度无效（prompt ~44 token < DeepSeek 缓存块粒度，cached 0/44）——诚实负结果；官方路径投机走框架 `preemptive_generation`，不依赖此项。
- C4 时段：TTFT p50=623ms（早间）；尾漂 644→878ms 轻度负载效应。

## 合成判读（给 Ethan 的拍板面）

官方路径轻载预期 EVS = **SAUC 判停 549ms（w=500 显式）→ 框架轮处理（P4 实测中）→ LLM TTFT ~500-650ms → 首句合成首包 420-870ms ≈ 1.5-2.0s**（说话人停嘴后）；每轮=一条连续语音（2.3-5.6s 出声），句内零天窗。P4 将钉死框架层开销与打断行为。

## P5 MT 车道 TTFT 竞测（2026-10-09 追加）——DeepSeek 留任

探针 `probe_mt_lane_ttft.py`（6 句 zh × cantonese/en 两向 × 两轮=24 请求/臂）：

| 臂 | TTFT p50/p90/max | tok/s | 术语 | 粤译 | 判定 |
|---|---|---|---|---|---|
| **deepseek-flash（现役）** | **735/803/1060ms** | 124 | 4/4 | **最地道**（12/12 粤标记） | ✅ **留任当家** |
| ark seed-2.0-lite-260428 | 2148/3375/4146ms | 29 | 4/4 | 次之（偏书面） | ❌ 2.9× 差距 |
| ark seed-2.1-lite-260915（bonus） | 1533/1981/3462ms | 51 | 4/4 | **追平 deepseek** | 🟡 晚峰备胎观察位 |
| qwen-flash | 1324/2429/**63s 尖刺** | 155 | 4/4 | **最弱**（繁体壳普通话+错译） | ❌ 出局 |

关键事实：①「快」名声在外的 seed-2.0-lite 流式 TTFT 实测 2.1s——type4me 的快不来自这条腿；②三家短 prompt 盘上缓存恒不命中（冷热 Δ 噪声内）——「同题重放省 TTFT」不是杠杆；③qwen 63s 排队尖刺=RPM 面单点风险；④方舟裸名模型 404，须用带日期全名（GET /api/v3/models 列账 135 个钉死）；seed-2.0-lite 走 chat/completions 全程通用（与 translation 模型只认 Responses 相反）。

**稳态滞后更新**：TTFT 地板 0.75s（本日读数）+ 首句短促 0.25s + MiniMax 首包 0.3s ≈ **1.3s 可达**，≤1.5s 目标成立；再往下无云道可换（唯一未试=晚峰时段性，18-22h 复测窗口 deepseek×2.1-lite 各一轮）。

## P4 官方 AgentSession 真形（2026-10-09 终版）——骨架可用，引擎判负

真房间 5 跑（探针文件 W8 以合规形状重生，原始数字=reports/w7probe/p4-session.md）：

| 场景 | 数字 | 判定 |
|---|---|---|
| S1 单轮 EVS | mean **2338ms**（最好 1.61s，5 跑 2.05-2.71s）——拆账：SAUC 轮界 471-571 + DeepSeek TTFT 483-726 + **MiniMax 首包 725-949**（AgentSession 语音路径开销高于裸链 420-870）+ 框架调度 | 贴 2.0s 线未稳过 |
| S2 连续语音 600ms 间隙 | **轮切分非确定**：3/5 切开、2/5 并单 final 且**第二句整吞** | 同传不可接受 |
| S3 打断 | 5/5 PASS（lead 612-631ms 掐在途译文） | 可用 |
| S4 追嘴档 | **零收益**（mean 2772 vs S1 2338；说话中出声 0/15 轮） | 机制见下 |

S4 机制：①1.8.2 preemptive **只吃 PREFLIGHT_TRANSCRIPT**，emit INTERIM 的 DoubaoSTT 缺省零抢跑；②重标后 **DeepSeek TTFT(0.36-0.97s) ≥ interim 节奏(0.17-0.7s)=投机完成前被逐 interim 取消**（采纳 0-1/3，playout 停到 commit）。**引擎终判：interp_lite/manual 胜出**；追嘴正确路径=lite clause-commit+我方 spec-mt；P5 TTFT 735ms 反转 lite 审计表「投机收益窗小」判断。

## 侦察五路（R1-R5，2026-10-09）

- **R1 LiveKit**：PREFLIGHT 投机（P4 证云腿物理不成立，留架构位）、user_turn_limit、dynamic endpointing(-100~300ms)、min_words 打断门、FlushSentinel、multi-user-translator 发布形状——1.8.2 全现货，归档为引擎翻案时启用。
- **R2 xiaozhi**：轮式架构不搬；七件可抄（两档标点集/sentence_id 票贯穿音频队列/两级打断/TTS 三档契约/MT 首可播块台架/流式术语滑动替换/竞速断句旁证）。
- **R3 GitHub/商用**：**稳态 1.3s 已快过全部公开商用**（KUDO 4.1s/豆包同传 2-3s/Timekettle 3-5s/Forasoft 1.4-1.7s）；auto_tempo 变速背压（Palabra 背书）、sokuji 右上下文门、业界 SLO 锚（<700ms=实时感带，唯一路径=spec HIT 面）。
- **R4 VAD**：本地 VAD 保留（CPU≈零/A 线依赖/尾 0.9s 是并段设计）；C 轻量兜底=显式 end_window=800+definite 看门狗；去本地 VAD 仅当「句头被吞」独立票翻案。
- **R5 StreamSpeech**：「<500ms」系讹传，论文最优档 AL 1.27-1.69s 与我们同量级；无 zh/粤语权重；不接。三份礼物=spec 门控同构佐证/小闸斜率平/<1.3s 是当前技术物理面。

---

**总判读**：三轨门槛全达成 + 引擎终判 + R1-R5 弹药清单齐——阶段二拍板面完整，W8 开工。

W7-A 带课五条：①end_window_size 必须显式设置（缺省 3s 不可用）；②`_fold_stutter` 官方路径退役（引擎缺省已折叠）；③早切/head-flush 全退役（官方攒句更优且治韵律断裂）；④轮尾 task_flush 必须发（省 2.1s）；⑤ping 保活已健在勿重复造。
