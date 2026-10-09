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

**三轨门槛全部达成**（P1 ≤400ms 零碎裂 ✓ / P2 判停窗可调 ✓（须显式设置）/ P3 ≤1.5s ✓）。等 P4 数字后进阶段二拍板，确认后才动 W7-A 产品代码。

W7-A 需带上探针教训：①end_window_size 必须**显式**设置（缺省 3s 不可用）；②`_fold_stutter` 官方路径直接退役（引擎缺省已折叠）；③早切/head-flush 全退役（官方攒句更优且治韵律断裂）；④轮尾 task_flush 必须发（省 2.1s）；⑤ping 保活已健在勿重复造。
