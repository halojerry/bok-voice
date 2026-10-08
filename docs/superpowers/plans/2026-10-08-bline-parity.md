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

## 执行纪要（2026-10-08 晚，#225/#224/#226/#227 四 PR 全合流）

- **W0（#225+#227）**：观察窗缺省 0.45→0.2（窗税 −250ms）；MT 首 chunk
  早交。**热修**：首版用 FlushSentinel 被证伪——哨兵属 generate/llm_node
  车道，`say()` 签名 `AsyncIterable[str]` 不认（e2e I6「gen not drained」
  红的实证），热修改纯文本早放；且发现 MiniMax bidi 插件内建 A 线同款
  `_first_chunk_cut`+head-flush 对 say 流生效——早放文本即全部所需。
  wiring pin 立代码级禁令（say 车道禁哨兵）。
- **W2（#224）**：C2 必中体有界等待（`BOK_INTERP_SPEC_WAIT_S` 0.6s）+
  B2 人设音色 collapse（三语=A 线同把声，`_persona_voice_map`）+ C1 裁剪
  （spec-ready 缩窗废弃）+ ①号探针入库（SSRF 护栏：CP 环回 allowlist+
  wss 公网门）+ 本计划档/S2S_ROADMAP 增补二。
- **W1（#226）**：DoubaoSTT `clause_commit`（B 线专用旗，A 线零漂移）——
  interim 子句级闸（A 线 `_sentence_boundary` 移植单点 import）说话中发
  FINAL；committed-prefix 对齐（exact→归一→失配重置）；`BOK_INTERP_CLAUSE_COMMIT`
  缺省开。env 立法三键并立（_FORWARD_ENV 274→277）。
- **集成验收**：全量 5035 passed/2 skipped；e2e_interpret **8/8**（热修后）；
  装配观测「utt-merge wait=0.2s, clause-commit=on; spec defer-hit 0.6s」全在场。
- **遗留真相（探针未过账如实记）**：①延迟探针 3522/4391>3500——当夜
  DeepSeek 首 token 878→1452ms 恶化+**服务端整体缓冲实锤**（11 delta 同帧
  到达：短输出假流式，first_ms≈mt_ms 结构性，客户端无解）；②探针语料=
  15 字单子句句，CLAUSE_COMMIT 闸（逗号前需 ≥6 字）结构性不触发——W1 的
  赢面（说话中重叠）需多子句长语料或真人通话才可观测，下一刀=探针语料
  补多子句刺激+真人实弹采 CLAUSE_COMMIT 命中率；③DeepSeek 首 token 是
  当前最大单腿（0.9-1.5s），杠杆=前缀缓存命中率核查（usage
  prompt_cache_hit_tokens）+ flash/v4-pro A/B。

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

## 执行纪要·追加（2026-10-08 晚，call-21739d55 复盘波）

- **W1×spec 结构性饥饿（真因，真人通话实锤）**：Ethan 真人同传「长句切碎+
  不追嘴」。turns=16 轮全 me 侧；perceived 2977-4742ms；**整通 INTERP_SPEC
  零开火**（armed 但 silent）。代码序定案：`_maybe_interim` 里 clause-commit
  先跑、会话级 interim 只带 `_clause_tail` 剥前缀尾巴 → spec 检测器候选首见
  于 interim k-1、k 时被 commit 剥走=第二次目击永不到场。10-06 spec 实弹
  在 W1 之前，故当时能命中；W1 部署（10-08）后 spec 全饿死。
- **第二把刀=字数口径劈叉**：提交闸 len(sentence)≥6 含标点（「坐地铁到啊，」
  放行）vs spec 闸剥标点数内容字 5<6（不开火）——同一子句「提交了却不投机」，
  碎片照样付全价 MT。已统一为 len 含边界标点。
- **第三刀=C2 等窗 0.6→2.0**：晚档 DeepSeek 假流式首 token 542-2131ms+
  TTS 排干——0.6s 等窗必中体几乎必超时回落「重新付全价 MT」；等待永不劣于
  兜底。env 可回旧档。
- **修复形状**：DoubaoSTT 新挂点 `raw_interim_listener`（镜像
  stable_prefix_listener 纪律）——流层喂「上一提交坐标之后的尾巴+本次刚
  提交子句」（=下一 FINAL 同坐标系；commit interim 恰构成候选第二次目击，
  后续自动回余段坐标不重复开火）；interpret 豆包档挂点直喂+会话层 interim
  让位防双喂；本地 ASR 无挂点=旧喂法逐字节。
- **音色选择器统一（用户拍板翻案）**：「我方/对方音色」下拉与「人设音色」
  下拉=两套选择器且与人设预设对不上——砍掉目录下拉+人设下拉，改
  「我方人设/对方人设」两个选择器，配音语义（我说的译文用我的声、对方说
  的用对方的声，人设页预设/克隆音色，全场同声跨语言不换声）；voices_json
  直接按 myLang/otherLang 填（B 线七语 _norm_lang 已收）；web 停发
  persona_id（worker B2 层保留给 API 派发）。parseVoiceMap/primaryVoiceFor
  收编 lib/voice-map.ts 单源（personas 页拷贝删除）。
- **晚档基线读数（fwd zh→canto）**：ASR_MS 233-796 / DeepSeek mt_ms 542-
  2131（first_ms≈mt_ms 8/8=假流式再证）/ TTS 首 548-1075（cold 1075）/
  INTERP_BACKLOG depth=2 无 drop；「嗯。」纯应承走 FRAG hold 600ms 超时
  单发（perceived 3317——产品项：纯应承是否直杀待议）。
- **验收**：定向三件 119 绿→全量 **5040 passed/2 skipped**（+5：豆包挂点
  双测+接线 pin+等窗缺省+口径）；web tsc+build 绿+voice-map node 测试 2/2。
  回退：`BOK_INTERP_SPEC_MT=0`（spec 整链关）或摘挂点（会话层喂法自动回
  旧路=饥饿形态回归,勿单独摘）；`BOK_INTERP_SPEC_WAIT_S=0.6` 回旧等窗。

- **spec 复活实弹证据（探针脚本入仓）**：`scripts/probes/probe_interp_spec_live.py`
  （多子句刺激+停顿挤压=逗号停顿压到 VAD 静音线内,对真人形状;含全静音护栏）
  call-cf4e2a99：`CLAUSE_COMMIT chars=20 lag=3770(说话中) → INTERP_SPEC fire
  chars=20 → defer wait=2s → defer-fallback`（DeepSeek 晚峰 2512ms 超 2s 等窗
  =HIT 未落地,管线全程活着）。注意 TTS 渲染逗号=0.3-0.6s 停顿会被 VAD 劈段
  （e2e 句形铁律）——多子句刺激必须挤压停顿才逼近真人。
- **:1236 启动闸补漏（用户质询「为何还启 hy-mt」）**：`_start_mt_llm` 此前
  不吃 posture——mt 云档仍因「模型在盘」起本地 Hy-MT2（主树=闲置常驻违全云
  指令;干净树缺 sidecar venv 直接 FileNotFoundError 打死 serve）。已补
  posture 闸（mt_local=False 跳过,同 :1235/:1237 姿势）。
- **BOK_AMBIENT_SCENE 移除（用户指令）**：环境音轨污染探针 captured 判据
  （57s 房间底噪被当译文音频）;栈已按无 ambient 重启。
