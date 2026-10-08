# B 线同传「跟 A 线一样快」——全云重叠管线执行计划（2026-10-08）

用户指令链：全云不动摇（豆包 ASR / DeepSeek LLM / MiniMax TTS，零本地模型产品腿）；
B 线体验=A 线（同流式同速度）；S2S 形状+我们三件套=靶图；重叠是唯一能藏掉
DeepSeek 0.8s 物理腿的手段。

## 0. 调研定案（四源官方文档 + 三 subagent 实测，2026-10-08）

- **MiniMax bidi 官方**（t2a_v2_bidi 文档）：服务端攒句规则=句末标点
  （。！？…；!?. 及换行）**立即**合成；次级标点（，、：,;:）**须攒够长度**才
  合成；累计长度上限强切；停发超时自动催出。`task_continue` 任意粒度安全
  （逐 token）；`task_flush` 催尾不闭会话。——与 9-29 实测一致；我们的
  head-flush 是官方姿势的合法强化。
- **DeepSeek 官方**：SSE 流式标准；**磁盘 Context Caching 自动**，前缀稳定的
  长输入首 token 大降（128K 13s→500ms）；usage 带 `prompt_cache_hit_tokens`
  可观测。——MT 请求静态前缀（instructions+术语）字节稳定 ⇒ 子句级多次
  请求全吃缓存，first token 进 500ms 级。
- **火山官方 AIGC demo**：自家管线 VAD `SilenceTime=600ms` 档——0.45-0.9s
  停嘴窗是行业正常带；无服务端魔法治停（与①探针互证）。
- **LiveKit 官方**：`FlushSentinel` 在 say()/TTS 转发路径受支持（本仓 1.8.2
  源码实证：流上 FlushSentinel=硬段边界，逐段独立合成、下段合成与上段播放
  重叠、打断按段截断）；官方 Pipeline Translator 配方=同形状
  （preemptive_generation；我们双方向「绝不丢话」语义保留 manual FIFO 分叉，
  有档案）。
- **subagent 实测**：①豆包 definite utterance 滞后 5-6s=**死路**，interim
  `result.text` ~400ms 更新=快车道；utterance 自带 words[] 词级时间戳
  （未来字幕料）。②HF speech-to-speech 无重叠形状（转写 final 才触发 LLM），
  偷五件工程件（revision turn 协议/CancelScope gen 计数/句批参数/延迟分段
  schema/stale-filter）。③A/B 基线：A 首声 p50=1763（eou600+llm666+tts546）；
  B 窗关档 1666 **已反超**；窗开档 2332（窗税+450）；结构性缺口=MT 短句等
  整句（+224，first_ms≈mt_ms 实锤）+TTS 首音频 B 已占优（468<546）。

## W0 速赢（B 线单侧，~1 小时）

1. **观察窗缺省 0.45→0.2s**（`BOK_INTERP_UTT_WAIT_S` 缺省改 0.2，env 可回调；
   测试计数钉同步）。窗税 −250ms。
2. **MT 首 chunk 早交（官方 FlushSentinel 车）**：`_mt_stream_say` 生成器——
   首段按「首个子句边界 或 N 字（缺省 10，`BOK_INTERP_MT_FIRST_CHUNK_CHARS`，
   =0 回旧行为）」切出即 yield 文本 + `FlushSentinel()`；数字/拉丁 run 铁闸
   （对齐 A 线 `_first_chunk_cut` 语义）；后续段照旧标点切、尾段 flush。
   ⇒ 首声 ≈ MT 前 10 字 + 合成地板 210-340ms，短句不再等整句（−200ms+）。

**验收**：probe avg ≤3.2s；短句 first_ms 显著 < mt_ms；e2e_interpret 8/8。

## W1 路线 1 主刀：说话中成句（interim 句级闸，1-2 天）

- DoubaoSTT 增 `clause_commit` 模式（B 线专用旗 `BOK_INTERP_CLAUSE_COMMIT`
  缺省开，A 线不传=逐字节旧路）：interim `result.text` 上移植 A 线句级闸
  纯函数（子句边界标点 + ≥6 字 / 跨窗稳定 / 长度档 / 限速 1.0s）——稳定
  子句前缀即发 FINAL（**说话中 MT 起跑**）；**committed-prefix 对齐**
  （后续 interim 减已提交前缀，HF 偷法#5 snapshot 纪律）；VAD END + 0.2s
  尾窗 = 尾句 commit 兜底（真停嘴边界）。
- 账本改子句坐标（一 utterance 多 src 条）；INTERP_LAG 加
  `commit_src=clause|tail` 列；DeepSeek 前缀缓存吃满并观测 cached_tokens。
- spec 处置：降级为「尾句保险」——捞回 worktree 冻结件 **C2（必中体有界
  等待，`BOK_INTERP_SPEC_WAIT_S`）**；C1（缩窗）废弃——W0 已直砍窗。

**验收**：连续语流译文与语音重叠（人工听感第一）；probe avg ≤2.5s；
e2e 8/8；A 线零漂移（旗不传）；402/429 监控（请求变多、缓存抵消）。

## W2 收尾沉淀

- **B2 人设音色 collapse**（worktree 冻结件，独立有效：三语言=A 线同款
  人设单音色）合入；**①号探针脚本**（worktree commit 96941a3）合入。
- HF 偷法清单排后续 W 线（revision turn 协议、CancelScope gen 计数、句批
  参数、延迟分段 schema、stale-filter）。
- 计划档/AGENTS/LATENCY_BUDGETS 同步；三 subagent 报告要点回写
  S2S_ROADMAP 增补二。

## 风险与回退

- 每刀独立 kill-switch：`BOK_INTERP_UTT_WAIT_S`（=0.45 回旧窗）、
  `BOK_INTERP_MT_FIRST_CHUNK_CHARS`（=0 回旧整句）、
  `BOK_INTERP_CLAUSE_COMMIT`（=0 回停嘴成句）、`BOK_INTERP_SPEC_WAIT_S`
  （=0 回旧 miss 档）。
- interim 前缀修订风险：跨窗稳定闸 + committed-prefix 对齐兜底。
- 子句级 commit 请求量 ↑：DeepSeek 磁盘缓存抵消大半，观测 usage 防账单惊喜。
- 全程全云：零本地模型产品腿；探针仪器腿（BOK_LOCAL_TTS/ASR=1）仅验收时
  临时拉起、跑完即回纯云。

## 顺序

W0（今天）→ W1（主刀）→ W2（收尾）。每步独立 PR+全量 pytest+CI+真栈 e2e+
探针读数；A 线全程零漂移。
