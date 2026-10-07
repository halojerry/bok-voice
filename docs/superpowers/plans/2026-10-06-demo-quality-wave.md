# Demo 质量波计划：卡死修复 + 音色目录扩展 + 语气词 + B 线控制台（2026-10-06）

> 背景：Ethan 2026-10-06 晚实弹报告+三轮裁定——①A 线多通电话卡死（全云腿下仍卡）
> ②新增德/法/日/葡四语音色（马来/阿语出局）、按语言×性别选、全量试听 ③AB 线语气词
> 结合上下文生成（每回复标记至多 3 枚+标点语气）④B 线控制台「可选听对方翻译」+
> 字幕按双方分栏成组、label 统一「翻译」⑤打断语义四分（短应承/假打断/真打断/合并
> 重生成）⑥teardown+DataStreamError ⑦声纹锁通话对象（防环境音冒充）⑧全程可拔插
> 场景环境音。本计划 = 取证结论 + 六个工作包（W1-W6）+ 验收判据。

---

## 0. 取证结论（2026-10-06 晚实弹，已完成诊断）

### 0.1 卡死三家族 —— 云腿无辜，病灶全在我们自己的集成层

数据源：`app-data/logs/agent.log`（今晚窗口 12:43–13:55 UTC）+ `bok_voice.db` turns 账本。

**云腿健康证据**（先排除嫌疑）：
- 豆包 ASR：`DOUBAO_ASR_TEXT … ASR_MS=1327~306(cloud)`，识别正常；
- DeepSeek LLM：`LLM_TTFT_MS 716/990 (official) cached=2432/3055`，命中前缀缓存，TTFT 亚秒；
- 结论：**卡的不是云，是 TTS 崩溃 × 打断级联 × 垫话缺席的组合拳**。

**流式重叠的事实口径（Ethan 2026-10-06 质询后的修正）**：管线本来就是重叠的——
LLM 首 6-10 字早切（`BOK_TTS_FIRST_CHUNK_CHARS`）+ bidi 头段催产 flush，TTS 不等整句。
所以稳态一轮 ≈ `max(LLM 首句, TTS 首音频) + 播报启动` ≈ **1.4-1.6s**（今晚健康轮
`PERCEIVED_MS` 1464/1568/1598/1636/2145/2181/2274 为证），不是 2s 串行和。
`commit_to_audio=2007ms` 那轮是**通话首轮冷启**：bidi 预热连接本身花了 2830ms 与轮赛跑
（`MINIMAX_TTS_BIDI_PREWARM connect_ms=2830`）+ 首次合成 1083ms——W1b 把 backup 池
提前到开场并行预热、A 线对偶件（interim→DeepSeek 前缀预热）再砍 LLM 首句腿，冷轮
拉平到稳态档。

**Family A —— 连环重问打断级联**（`call-6a8133f6` 13:13，账本 4 用户轮只 2 回复）
机制链：云档 A 线 `commit_to_audio=2007ms`（LLM 0.7–1.0s + TTS 首音频 ~1.1s）> 客户
耐心 1–2s → 客户重问 → 在途回复未开播即被打断弃流（`MINIMAX_BIDI_PERF … (interrupted)`
连续两次）→ 零回复落地 → 客户再问再掐 → starve-ack 只救一次（`连续 2 轮零回复,短承接让路`）
→ 客户放弃挂断。**加速器：云车道垫话自动关**（`agent.py` `filler auto-off (cloud a_reply
lane)`）——1.5–2.5s 的生成空窗无任何遮蔽，死寂直接诱发客户重问。

**Family B —— MiniMax bidi TTS 打断竞态崩**（`call-15c712aa` 13:14:52，今晚 ×3 房间
[`15c712aa`/`12173025`/`3a07cad9`] ×9 条；**历史存量病**：2026-09-22 ×342、09-23 ×912）
- 崩点签名：`RuntimeError: AudioEmitter isn't started`（`tts.py` `_main_task →
  output_emitter.end_input()`）——我们打断时的 cancel 路径与框架 emitter 生命周期赛跑，
  流从未出过音频就被关，框架收尾时炸。
- 后果：`FallbackAdapter switching to next TTS` → backup `speech-2.6-turbo` classic WS
  **全新连接** `ws_connect_ms=2468` + 首音频 607ms → **4–7s 黑窗**（13:14:52 崩 →
  13:15:00 recovered），期间 `不是啊。`/`我订单编号多少嘞？` 两轮彻底哑，
  `PERCEIVED_MS total=601 (… tts=-1000)`（TTS 首音频哨兵缺席）。

**Family C —— 秒断连零轮通话**（`call-63001ad3` 13:02:17 job→13:02:18 disconnect、
`call-2297b582` 同形；`call-82cdc639` failed+abandoned）
join 后 1 秒 participant disconnect——浏览器侧重连/刷新/网络抖动，**不是 agent 卡死**，
但污染「多通卡死」观感。低优先：disposition 细分 + RUNBOOK 行。

**观测洞**：被打断且未开播的回复不落 turns 账本（无 `gen=interrupted` 行）——看板会
低估回复数，诊断时也少证据。

### 0.2 音色：请求清单 × MiniMax 官方音色表差异（2026-10-06 `list_voices` 实拉）

**范围裁定（Ethan 2026-10-06）：豆包 ASR 不认的语种就不做——马来语、阿拉伯语整体出局
（目录+语言面都不加），语种面=德/法/日/葡四语。**

官方存在、直接入目录：
- 德语：`German_FriendlyMan`(M) `German_SweetLady`(F)
- 法语：`French_MaleNarrator`(M) `French_CasualMan`(M) `French_Female_News Anchor`(F，**ID 含空格，原样保留**) `French_FemaleAnchor`(F)
- 日语：`Japanese_GenerousIzakayaOwner`(M) + 克隆两枚 `moss_audio_c373f8c3-…`(F) `moss_audio_10297aea-…`(M)
- 葡萄牙语：`Portuguese_Strong-WilledBoy`(M) `Portuguese_UpsetGirl`(F) `Portuguese_AnimeCharacter`(F) `Portuguese_LovelyLady`(F) `Portuguese_CaringGirlfriend`(F)

**不在官方表、需 audition 真合成定生死（2054 voice-not-exist 当场现形）**：
- `Japanese_efficient_reporter_vv1`、`Japanese_rebellious_youth_vv2`（vv 系历史音色，目录里同族 vv2 仍在役，大概率活）
- `Portuguese_Optimisticyouth`、`Portuguese_FunnyGuy`（官方葡语无此二名；最近邻 `Portuguese_Jovialman`(幽默男)/`Spanish_OptimisticYouth`(西语勿混)）

裁决规则：audition 死掉的 ID → 官方最近邻替换，映射表落本计划 §W2a 认账，不静默塞死 ID。
（马来语 `Malay_*` 三枚、阿拉伯语 `Arabic_*` 按 0.2 裁定出局，不入目录。）

### 0.3 语气词：机制在，映射面是死的

- B 线机制健在：`interpret.py` `_apply_voice_tags`（句首引导词→MiniMax 2.8 括号标记）+
  `_voice_tags_supported`（仅 2.8 系）+ `BOK_INTERP_VOICE_TAGS` 总闸默认开。
  **但 `_VOICE_TAG_LEAD_RE` 只认句首 `hahaha/coughs/ahem/sighs/lol/heh`——DeepSeek MT
  译文是干净书面语，永远不以这些词开头 → 实弹 0 触发**（今晚 interp-rev 日志零标记）。
- A 线无任何语气机制：emotion 参数逐轮下发被架构决议禁止（真人感正道=TTS SFT）；
  LLM prompt 不产标记；唯一在发的是 `(breath)` 换气注入（`BOK_BREATH_INJECT` 默认开）。
- MiniMax 2.8 官方标记词表（`_VOICE_TAG_RE` 已收录）：`laughs/chuckle/coughs/clear-throat/
  groans/breath/pant/inhale/exhale/gasps/sniffs/sighs/snorts/burps/lip-smacking/humming/
  hissing/emm/sneezes`——**全是非语言声，没有情感词**。所以「语气词」策略 = 非语言声
  标记（叹息/轻笑/呼吸）+ MT/prompt 侧保留语气词，不开 emotion 参数。

### 0.4 B 线控制台现状（`apps/web/components/interpret-console.tsx`）

- 字幕形态=**单列聊天气泡**（我方右/对方左），label=`我方说的`/`对方说的`/`译文·中`/兜底`同传`；
  无「原文+翻译成组」结构 → Ethan 要的双栏成组需要重构渲染层。
- 听感拓扑：me 路由=对方原声（`hearOrig` 开关，持久 `bok_interp_hear_orig`）；oth 路由=
  `trans-*` 轨（=我方译文，对方耳机听到的）；rev 译文（`trans-<我方语言>`）由官方
  AgentSessionProvider 自动播放。**没有「听对方听到的翻译」独立开关**；agent 侧
  `_apply_track_permissions` 把 `trans-<对方语言>` 限制给对方身份订阅。
- 数据流=官方 transcription 事件（`useTranscriptions`），方向归属靠 identity 前缀 +
  `lk.transcribed_track_id` ↔ `trans-<lang>` 轨名匹配。

---

## W1 · 卡死修复（P0，先修生产病）

**W1a MiniMaxTTS 打断竞态崩（Family B 根修）**
- 位置：`apps/agent/agent_runtime/providers/livekit_plugins.py` MiniMaxTTS 流的 cancel/关闭
  路径。修法：打断取消时不再让框架 `_main_task` 在未启动的 emitter 上 `end_input()`——
  在我们侧 cancel 收尾里守卫（emitter 未启动视作干净取消，吞掉该 RuntimeError 路径），
  不改 livekit-agents 库本体（1.8.2 锁版）。
- 验证：单测模拟「首音频前 cancel」不再抛 `AudioEmitter isn't started`；
  `scripts/e2e/e2e_barge_in.py` 基线（interrupted=yes）不回归；连续快问重问剧本 10 通
  `grep -c "MiniMaxTTS error"` = 0。

**W1b fallback 黑窗压缩**
- backup `speech-2.6-turbo` classic WS **会话期预热**（`MINIMAX_TTS_WS_POOL_PREWARM` 已有
  机制，从「崩后补救」提前到「开场并行预热」），崩→backup 首音频目标 <1.5s
  （现实测 2468+607ms）。
- 打点：`MINIMAX_TTS_WS_PERF ws_connect_ms` 走池后应 <500ms。

**W1c 垫话云车道解禁（Family A 加速器拆除）**
- `agent.py` 云车道 `filler auto-off` 改为默认开（垫话配额/冷却/让路政策全部现成，
  `BOK_FILLER_YIELD` 默认即时的让路档），新增 env `BOK_FILLER_CLOUD`（默认 1=云车道也
  arm）走 `_FORWARD_ENV` 立法（258→259，`tests/test_bok_module_contract.py` 两处计数同步）。
- 效果判据：2s 空窗被垫话盖住，客户不重问 → Family A 级联不点火。

**W1d 账本补洞**
- 被打断且未开播的回复也落 turns（`gen=interrupted`，复用 `_PartialCaptureStream` tee
  语义，空文本允许）——看板/诊断不再低估。
- RUNBOOK「哑轮/卡死」行补 Family A/B 第一现场指针。

**W1e Family C（低优先）**
- disposition 细分：秒断连 ≠ failed；RUNBOOK 加一行「join 后 1s 断连=浏览器重连，非 agent」。

**W1f 打断语义四分（Ethan 2026-10-06 两轮定调：「打断就停生成，用户说完再继续；误打断
要继续/重生成且上下文匹配」+「嗯/好的也不到一秒——短应承不是打断」）**
- 现状缺口：`打断即弃流`（`_LlmFallbackStream.abandon()`）在**语音一进来就熔断**——
  连官方 `resume_false_interruption=True`（假打断恢复同一段播报）想恢复时，流已被我们
  杀掉，恢复了个空 → 假打断/短应承全变哑窗。这是 Family A 的放大器之一。
- 修法（四分法，abandon 从「打断瞬间」推迟到「打断确证」）：
  ①**短应承 backchannel**（用户说了「嗯/好的/对/哦/明白/ok」这类 ≤2s 应承词，ASR 文本
  判据=现成 `defer_ack` 短应承词表复用）→ **不是打断**：AI 短暂让位后**原位恢复同一段
  播报**，不 abandon、不重生成——客户在附和，不是抢话；
  ②**假打断**（VAD 误触发/环境音/回声，或 ≤`false_interruption_timeout` 就停嘴，框架走
  恢复路径）→ 不 abandon，在途 LLM 流保活，恢复同段播报；
  ③**真打断确证=新用户轮落定**（客户说了新内容/新问题，非应承词）→ 才 abandon+abort
  （现状语义，部分回复照旧 tee 落账 `gen=interrupted` 保上下文）；
  ④真打断后下一轮回复=被打断部分（已在 chat 历史）+ 新输入合并生成——上下文匹配靠
  落账，已具备，补单测钉死（打断轮在历史里、下一轮 LLM 请求含被打断部分）。
- **声纹/环境音判据现状**（Ethan 问「声纹识别？环境音影响？」）：A 线是 1v1 通话、房间
  里只有一个远端说话人，**不需要声纹区分说话人**；环境音误触由既有三层接——VAD
  `min_duration=0.6s` + 回声守卫（`_echo_filter`/AI 自闻 ≥0.9 丢弃）+ ambient keyboard
  检测（今晚日志在跑）+ 纯犹豫残片门。本波不引入声纹腿。
- 风险与兜底：abandon 推迟会让「晚到答案」窗口变大——`_late_answer_dedup_verdict`
  （≥0.85 弃重）+ `_RepeatSelfGuardStream` 复读防线是既有兜底，验收剧本加「短应承连发
  轮 + 假打断连发轮」专测恢复不重复。
- 验收：`e2e_barge_in.py` 基线不回归 + 新增探针轮（①插「嗯/好的」→ 恢复同段零哑窗；
  ②<1s 假打断 → 恢复；③真插话 → 停+合并重生成）。

**W1g teardown 清洁度 + 浏览器 DataStreamError（Ethan 2026-10-06 报障）**
- 症状：web 控制台 `DataStreamError: Participant agent-… unexpectedly disconnected in
  the middle of sending data`（13:24:55）——agent 参与者在 transcription 数据流还开着时
  就断线，livekit-client 在浏览器侧抛未处理异常（dev overlay 全屏红）。
- agent 侧另一半尸体（今晚日志对上）：`entrypoint did not exit in time, cancelling` ×42
  + `failed to send session event` ×N + `Task exception was never retrieved (_on_close)` ——
  entrypoint 被强杀时没等 transcription/数据流关完 → 房间侧留下半开流 → 浏览器报错。
- 修法：①挂断路径顺序化——`session.aclose()` 后**等在途 transcription 流 END 再退房**
  （超时上限 2s，超时才强退，打点 `TEARDOWN_STREAM_FLUSH`）；②web 侧兜底——
  interpret-console/CallStudio 对 DataStreamError 加静音捕获（participant 断线时的
  半开流是收线正常副产品，不该炸 overlay；`lib/logger.ts` defaultWrite 对该错误降
  warn）。根修在 ①，②只是不再吓人。
- 验收：挂断 10 通浏览器控制台零 DataStreamError；agent.log `entrypoint did not exit
  in time` 归零（或仅剩 2s 超时兜底档）。

**W1 验收**：复演今晚剧本（粤语、快问重问刺激）10 通零哑轮（turns 对账零未回复用户轮）；
全量 pytest；barge_in + edge_cases 回归绿；CI 绿。

## W5 · 声纹锁：锁定通话对象，环境音/其他人声不许冒充（P2，默认关可拔插）

**需求（Ethan 2026-10-06 澄清：声纹=区分「谁才是通话对象」，不被环境音影响）**
- 实弹痛点在今晚账本里：`call-15c712aa` 有一轮用户转写是**「打打键盘的声音。」**——
  ASR 把键盘噪声幻觉成了客户说话并当成一轮。环境里的人声（电视/家人）同理会冒充客户。

**方案：enrollment 声纹锁（pre-ASR 门）**
- 位置：VAD 段与 ASR 之间。第一段**确证语音**（首个成功成轮的段）做 enrollment 提取
  embedding；之后每个 VAD 段算 embedding 相似度，低于阈值 → 判「不是通话对象」→
  不进 ASR、不成轮，打点 `SPEAKER_LOCK_DROP`（带相似度值）。
- 模型腿：本地小 embedding（resemblyzer / ECAPA-TDNN 级，CPU 每段 ~20ms，Mac 无压力），
  模型入 ensure 可选档（不默认下载）。**kill-switch `BOK_SPEAKER_LOCK` 默认 0**。
- 已知坑与对策：①客户中途换设备/蓝牙耳机切换=音色突变 → 相似度骤降连续 N 段时
  触发重 enrollment（打点 `SPEAKER_RELOCK`）；②1v1 房间本就单说话人，锁的是
  「非人声+人声但不是他」两类；③与回声守卫/键盘检测叠加不互斥（多层纵深）。
- 验收：播环境音/旁人说话素材 10 段零成轮（账本无幻觉轮）；本人语音 0 误杀；
  开关关闭=逐字节旧路径（`test_forward_env` 立法注册）。
  **⚠ 2026-10-07 第四波实弹改判**：「旁人声冒充」v1 嵌入做不到（真语音异人
  0.816-0.973 与同人重叠，见执行纪要第四波）——已落地能力=环境音/键盘噪声
  滤除；认人=v2 ECAPA 独立票。验收面同步缩为「环境音段零成轮+本人 0 误杀」。

## W6 · 全程环境音轨：可拔插场景底噪（P2，demo 真实感）

**需求（Ethan 2026-10-06）：全程循环环境音——车里、办公室等，可拔插，作为贯穿背景**
- 基建现成：`BackgroundAudioPlayer`（垫话 out-of-band 音轨同一套）——加「场景底噪
  循环层」：低音量无缝循环铺底，**AI 说话时自动 duck（避让 -12dB 级）、停嘴回涨**，
  绝不与垫话/回复抢道。
- 场景档起步三枚：`office`（写字楼底噪）/`callcenter`（客服中心人声嗡+键盘）/
  `car`（车内路噪+起步引擎）。资产=随源码分发无缝循环 wav（`agent_runtime/assets/
  ambience/` + manifest，**同 fillers 分发模式**；CC0/自录，license 记 manifest），
  外部目录可自行投放扩充。
- 选择面：env `BOK_AMBIENT_SCENE`（none 默认）+ 设置面下拉（A 线客服档；B 线可选开，
  出站在我方译文轨）。
- 回声注意：底噪发布在 agent 出站轨，客户侧浏览器 AEC 处理 + 我方回声守卫兜底；
  底噪电平恒定低（-30dBFS 级）不触发我方 VAD（我方听的是客户轨，无自听）。
- 验收：开 `callcenter` 档实弹一通——全程底噪在、说话 duck 生效、ASR 零误触、
  PERCEIVED 无回归；关闭=零音频发布（逐字节旧路径）。

## W2 · 音色目录扩展 + 语言×性别选择 + 全量试听（P1，demo 硬需求）

**W2a 目录扩容**（`apps/web/lib/minimax-voices.ts`，唯一数据源）
- 新增**四语组**（德/法/日/葡，按 §0.2 裁定；马来/阿拉伯出局），条目按 §0.2 差异表；
  `MinimaxVoiceEntry` 加 `gender: 'f'|'m'` 字段（既有三语组同步补标，分组注释转正为数据）。
- audition 裁决：跑 `scripts/seed/cache_minimax_auditions.py`（真合成 t2a_v2，物化到
  `assets/minimax-auditions/`）验证全部新 ID；死 ID → 最近邻替换并落映射表进本计划。

**W2b 类型与筛选面**
- `MinimaxVoiceLang` union 扩（+de/fr/ja/pt）；`lib/voice-options.ts` `KNOWN_LANGS`
  同步；`resolvePreviewLang` 前缀映射扩（`German_/French_/Japanese_/Portuguese_` → 对应语）；
  CP 侧 `language_boost` 映射扩（German/French/Japanese/Portuguese 都在 MiniMax boost 枚举内）。
- 下拉按性别 optgroup（女/男）——personas / settings 三键 / interpret 会话音色三处
  消费同一 `buildVoiceSelectOptions`，一处改全生效。

**W2c B 线语言面**
- interpret 页 `myLang`/`targetLang` 下拉加四语（德/法/日/葡）；MT=DeepSeek 任意对天然支持；
  ASR=豆包 2.0 十三海外语种在列（ja/de/fr/pt），**仍跑一遍 `probe_cloud_asr.py` 四语
  小语料实测确认再放开 UI**（宣发≠实测，一门语料一炷香的事）。
- A 线语言三态（zh/cantonese/en）**不动**（每通钉死立法）。

**W2e 试听全量物化**
- audition 一次性物化全部目录（含既有三语）→ web 试听按钮优先吃本地物化音频
  （零延迟、离线可试、不烧云费），`/api/tts/preview` 作 fallback。
- 试听语言联动：新语种试听样本文本（`previewSampleText` 扩）。

**W2 验收**：audition 全绿或映射表认账；`npx tsc --noEmit && npm run build` 绿；
e2e 三语回归不受影响；B 线新语种实弹一通（zh↔ja）出声。

## W3 · 语气词/真人感（P1.5，**上下文驱动生成**——Ethan 2026-10-06 定调：语气词要结合
上下文来生成才真实，不吃静态映射表）

**W3a audition 台架（与 W1 并行开）**
- 同句 ± 标记 A/B 合成（`(sighs)/(breath)/(laughs)/(emm)` × 2.8-hd/2.8-turbo × 粤/普/英），
  人耳测 + ASR 回灌确认「出真声而非念字」；产出报告进本计划。

**W3b A 线：LLM 上下文自决标记（主路径）**
- `BOK_A_VOICE_TAGS`（**默认 0，demo 档显式开**）：reply prompt 静态前缀加判断框架——
  「结合对话语境与客户情绪，需要时可用 `(sighs)/(chuckle)/(breath)/(laughs)` 等标记、
  以及自然的 `……`/`！`/`？` 标点来表达语气；**每回复标记至多 3 枚，多数轮次应当少用
  或不用**」（Ethan 2026-10-06 裁定：上限 1→3，标点也是语气载体——标点本就进正文，
  TTS 天然带韵律，无需剥除；括号标记才走剥除管道）。
- 加不加、加几枚由 LLM 按上下文自决，不做机械规则。
- 出口剥**括号标记**保 transcript/账本干净：`tts_text_transforms` 同款剥法（挂法同
  `_strip_expr_markup`）；repeat-guard/跨轮账本比对前剥（复用 B 线 `_strip_voice_tags`
  纯函数，上收 core 共享，A/B 线同源）。标点不剥。
- 每通频率护栏：同一标记连续三轮出现即剥离（防 LLM 腔调固化，纯函数单测）。

**W3b-pre 口气判定现状（Ethan 问「现在能识别说话人口气吗」——2026-10-06 口径）**
- **文本层=已具备**：A 线每轮已在跑 `EmotionProcessor.classify`（`ExprAwareLLM` 前置
  `<expr>` → `lk.expression` 11 档 mood 给前端 avatar）；但其英文关键词表对中文弱
  （中文多半回落 calm）。W3 的主判据交 LLM（DeepSeek 看措辞+标点+上下文，判得比关键词
  表准）；B 线对源文分类前先把 `EmotionProcessor` 中文关键词表补齐（现成 11 枚举不动，
  只补 zh 触发词）。
- **声学层（韵律/声纹情绪）=今天没有**：豆包 SAUC 只回文本+置信度，不给韵律特征；
  上声学情绪识别=新模型腿，本波不引入（记档：若文本层效果不够，候选=MiniMax/本地
  speech-emotion 小模型，独立评估票）。

**W3c B 线：源语情绪→标记（上下文=对方这口气）**
- B 线的「上下文」= 源语说话人的情绪，不是 MT 自由发挥。链路：复用
  `EmotionProcessor.classify`（现成英文关键词表 + calm 回落）对**源文**分类 →
  有限映射 sad→`(sighs)` / happy·excited→`(chuckle)` / 其余不加 → **频率护栏（每 N 句
  至多 1 枚，默认 N=4）+ 每通上限**，防止句句叹气。
- `_VOICE_TAG_LEAD_RE` 句首引导词映射保留（源文真有笑声/叹词时照翻照标）。
- `BOK_INTERP_VOICE_TAGS` 总闸不变；`_strip_voice_tags` 字幕口径不变。

**W3d breath 确认**：`BOK_BREATH_INJECT` demo 档在发（已有机制，验收时打点确认）。

**不做**：emotion 参数逐轮下发（架构决议：真人感正道=TTS SFT）；标点驱动注标记
（MiniMax 标记词表没有情感语气，机械注入风险>收益）。

**W3 验收**：audition 报告归档；e2e 断言字幕零标记泄漏；A/B 线听感对比 Ethan 点头。

## W4 · B 线控制台（P1.5）

**W4a 字幕双栏成组**
- 重构 `interpret-console.tsx` 字幕卡：**我方列 / 对方列**两栏，每栏内「原文气泡 +
  其翻译气泡成组」（原文在上、翻译紧随，视觉同组）；label 统一为「原文」「翻译」，
  删 `译文·X`/`同传` 字样；流向筛选 pill 与大字幕窗同步同构。
- 数据层不动（transcription 事件归属逻辑复用 `whoIs()`）。

**W4b 听对方听到的翻译**
- agent 侧：`interpret.py` `_apply_track_permissions` 放开 `me-` 身份对
  `trans-<对方语言>` 的**订阅权**（订阅≠自动播，不影响对方收听）。
- console 侧：新增 toggle「听对方听到的翻译」（持久 `bok_interp_hear_their_trans`），
  默认关；开=该轨进 me 路由（我方扬声器），与 `hearOrig` 同族互不干扰。

**W4 验收**：`e2e_interpret` 8/8；双栏+成组渲染截图给 Ethan 确认；toggle 实弹听一通。

---

## 执行顺序（2026-10-06 定稿）

**W1（P0 生产病，a-g 七件）→ W6（环境音轨，demo 面子立竿见影）→ W2（音色四语+试听）
→ W4（B 线控制台）→ W3（语气词，W3a audition 台架与 W1 并行开）→ W5（声纹锁，默认关
可拔插，独立 PR）。** 每波独立 PR、全量绿+CI 绿才合、`[SESSION_ID]` 纪律；Mimosa 门
输出每次提交必读。

## 执行纪要（2026-10-06/07 第一波落地）

第一波六包全落地，session 分支 `session-20261006-233933-0219`（基线 26a6ea6）：

| commit | 内容 |
|---|---|
| b6dd12b | 计划档 + env 预注册四键（_FORWARD_ENV 258→262；W3 复用既有 BOK_A_LINE_VOICE_TAGS） |
| 577f87f | W1g-web：DataStreamError 收线良性噪音降 warn（判据单源；特征短语 livekit-client 全 bundle 唯一构造点） |
| 1a48690 | W2：四语 18 条入目录+gender 表（缺标即抛）+audition 全量物化 35 mp3（四候选 ID 全存活零替换）+试听吃本地物化+_norm_lang/boost 四语 |
| e2d8357 | W6 模块：ambience.py（无限迭代器循环+dB 域 duck 轨迹）+三场景种子化资产+manifest |
| 09de3fc | W3 查因：probe_voice_style_gate（H1 排除/H2 强形式否定/真因=script 线 79%+换气地板够不着+概率产出）+bench_voice_tags_ab（35 条物化 reports/voice-tags-ab/） |
| efb02b2 | W3 修投：白名单+sighs/chuckle/laughs、换气地板 20→12、NATURALNESS_BLOCK 语境化改版（≤3 枚+标点） |
| 7b1ae9c | W1c/d/f/g：垫话云车道解禁（BOK_FILLER_CLOUD）、零产出打断落账、deferred abandon（确证点=下一轮非短应承用户轮；嗯/好的豁免；逃生口 BOK_INTERRUPT_INSTANT_ABANDON）、挂断 TEARDOWN_STREAM_FLUSH ≤2s |
| 8155d43 | W6 接线：agent.py 四挂点（解析/duck 钩子先于 session.start/起播/收摊） |
| 08119ed | W1a+b：框架取证 tts.py:601 无条件 end_input——零音频收尾垫静音根修（MINIMAX_TTS_ZERO_AUDIO_PAD 打点）+beep 自启真 bug 修+prewarm 并行暖 classic 池线 |

全量 pytest **4842 passed / 4 skipped**（基线 4753，+89 测试）；tsc 绿；web node 套件绿。

## 执行纪要·续（2026-10-07 第二波 PR #194 + 第三波）

**第二波（PR #194，main=95c9633）**：W4 双栏成组+label 统一「翻译」+听对方翻译
toggle（a178c53）；W2c 判决落地——豆包对 de/fr/ja/pt 完全不可用（24/24 幻觉/空串、
nostream 0/21 同签名），源语下拉收三语/目标语保七语（7f38d42+9d6bd81）；
edge_cases 模板闸修复（621220f）。实弹验收：三语 E2E/barge_in interrupted+resumed/
interpret 8/8/ambience callcenter 起播+duck/零 ZERO_AUDIO_PAD/零 TTS error 全过；
audition symlink 在 CP Docker web-build stage 悬空→真文件直发（CI 修复）。

**第三波（session-20261007-b-061319-71c4）**：
- `6046a21` A 线对偶件：PrefillSpeculator 云放行（DeepSeek 前缀缓存预热，
  BOK_PREFILL_SPEC_CLOUD 缺省开+三护栏 16k 字符/每通 12 次/端点闸；abort 云端
  零注入确认；首轮 6k 级真实预热仍本地专属）。**合流后实弹验证：fire lane=cloud
  + LLM_TTFT_MS cached=N/M 抬升**（基线 716-990ms cached=2432/3055）。
- `9450dfa` W5 声纹锁模块件（v1 纯 numpy mel 白化统计声纹：同人 0.84-0.98/
  异嗓 0.03-0.14/键盘 0.05-0.13；fail-open 三态；默认关）。**接线+真实语音阈值
  实弹留下窗**；v2=ECAPA-ONNX 单点替换 embed_pcm。
- `810d73a` edge_cases E2/E4 根修 8/8：E2=Vivian 声源债（Sinji 渲染修）、
  E4=E3 风暴静默吞掉共用通话首句（独立通话+账本权威断言修）；ambient 轨三重
  证据无罪。
- **SubK 产品侧两张真票（下窗派单）**：①风暴过期死气窗（active_until 过期后
  无新输入=对停嘴客户无限静默，实测 ~30s——考虑过期即 resume 或看门狗短应承）；
  ②风暴后首真回复被 REPEAT_GUARD 误杀（33 字投诉回复 REPEAT_GUARD_CANCEL_DROP
  ——跨轮复读账本在风暴 ack 弹幕语境下过严，考虑风暴窗内不进账本或豁免段）。
- 四语双向候选（按语种分 ASR 车道，MiniMax 伪流式无热词）仍为独立评估票。

**第四波（session-20261007-121244-fcc1，W5 接线+真语音阈值实弹）**：
- `SegmentSpeakerGate` 接线件进 speaker_lock.py：每 VAD 段早期判定（1.5s 前缀含
  VAD 前导，一次性 admit 缓存）、段末登记钩（`has_enroll_text`≥4 实词字符防幻听
  锚噪声）、DROP/RELOCK/ENROLL 打点单源。**豆包线**（`_DoubaoLiveStream`：
  START 起段/`_feed` 停喂 WS/`_maybe_interim` 抑制/END 镜像收线窗整段吞+关会话
  止损/段末登记）与**本地线**（`_Qwen3ASRLiveStream`：INFERENCE_DONE 不进
  `_pending` 不喂 sidecar、END+`_hold_flush` 两路镜像抑制+登记）同挂；agent.py
  装配点每通一把双路传入（总闸关=两车道字节零漂移，缺省）。测试三面
  （gate 单测 9/豆包 3/本地线 3）+全量 **4905 passed**。
- **真语音阈值实弹翻案（关键勘误）**：probe_speaker_lock（四把 macOS 嗓音×
  office 底噪 12/6/0dB）实测——同人净 0.940-0.995、同人+0dB 底噪 ≥0.859、纯
  office 底噪 0.694-0.697、**异人嗓音 0.816-0.973 与同人带噪重叠**：合成素材的
  0.03-0.14 异人分离度**不迁移**，v1 白化 mel 统计嵌入**只分语音/非语音、不分人**。
  缺省阈值重标 0.55/0.40→**0.78/0.65**（旧值真语音上连纯底噪 0.69 都放行=恒
  no-op）；探针 PASS=零误杀+纯环境音 3/3 判丢+端到端活。**能力边界改判**：v1=
  环境音/键盘噪声滤除（15c712aa 原始痛点覆盖），「旁人声冒充」需 v2 ECAPA-ONNX
  （独立票：ensure 可选档+单点换 embed_pcm，接线全不动）。
- W5 验收面更新：`BOK_SPEAKER_LOCK=1` 实弹一通（观测 SPEAKER_LOCK_ENROLL 后播
  环境音段验证零幻听轮）留下窗；模块 docstring/测试头已带勘误段防旧口径回流。

**第五波（session-20261007-165220-1a9f，PR #198 main=f9a0263）**：
- **W5 实弹收官**：栈 `BOK_SPEAKER_LOCK=1 BOK_LOCAL_ASR=1`（探针仪器位）跑
  canto E2E **PASS 1/1**；`SPEAKER_LOCK_ENROLL dur≈2.4-2.5s chars=8-10` 每通
  必落、**零 DROP/零 RELOCK**（E2E 单一合成嗓音零误杀）。全云档跑探针须
  `BOK_LOCAL_TTS=1 BOK_LOCAL_ASR=1` 双仪器位（:8788 渲染+:8787 语言判定腿）。
- **票①死气窗（74ebce3）**：第一性定案=风暴状态机观察「抢话」不观察「停嘴」
  ——惰性过期撞上停嘴客户=无限静默,风暴盲 nudge 升级 farewell+no_response
  把刚讲完的客户误诊挂断。修=engage/续期处 arm 到期钟（quiet_s 到点自清+零
  starve+三语回收线 storm-reclaim 车道,迟到 no-op,resume/cap/teardown 收钟）
  +nudge 风暴让位（风暴活期间整体跳过、短周期重挂不拆錶）。新 env
  `BOK_INTERRUPT_STORM_EXPIRY_RESUME`（缺省开,_FORWARD_ENV 263→264）。
- **票②复读误杀（2a3b2ce）**：SubK 账本假设**证伪**（starve-ack gen=script 被
  reply_ledger 只回 llm+ack 锚豁免双重过滤）——真凶=`_on_item_for_context`
  无条件 set_last_reply/record_reply,风暴打断的半截碎片进锚,重生成同答案首句
  vs 自家碎片 ≥0.9→头冻结→6s 看门狗→33 字死在 REPEAT_GUARD_CANCEL_DROP。
  修=`item.interrupted` 复用 ack 豁免通道（锚/账本/摘要三面让开;prefill 喂入
  与 chat ctx 真历史不动）。
- 全量 **4910 passed**（+5）；commit 分层经 reset+重放保证（重组树 reflog
  逐字节验证）。两张票实弹验收=单测+源级 pin（无风暴剧本 E2E,下一窗可加
  barge_in 连发脚本顺带观测 `[storm] expiry-resume` 行）。
- **事故记录**：harness shell 因 cwd 停在已删 worktree 目录 spawn 全灭
  （ENOENT-on-dead-cwd）——解法=Write 工具原地重建该目录复活 cwd（勿用定时
  任务绕）。

**第六波（session-20261007-202348-d436 + 204411-5cc6，PR #203/#204 main=b7d20ad）**：
- **死气窗票收官链**：#200 timer 自毁守卫根修（fire 醒来 now≥active_until 恒真
  →逐值比对 `_storm_expiry_should_clear`）+#202 探针加固（offscript 强刺激保
  LLM 车道）——`probe_storm_expiry` **三关全过**（expiry 打点/回收线 2.6s/尾问
  真答 1.2s）。
- **#201 灰区前缀不早丢**：probe 第三关抓到同嗓音尾问前缀 0.75 被误杀（标定用
  整段、门判前缀方差大）——前缀只裁 <relock_sim 清弃；灰区照喂 ASR 段末**整段
  复核**，复核判丢才吞 FINAL（两车道三处否决点）。
- **W5 v2 认人票（#203）**：官方 speechbrain ECAPA ckpt（Apache-2.0）自导出
  单文件 ONNX（84MB sha256 存档,parity=1.0,导出件 `scripts/seed/export_ecapa_onnx.py`
  torch 懒导入）；`SpeakerLock` 双档——**显式 env `BOK_SPEAKER_LOCK_MODEL`
  才激活**（不自动扫盘,灭环境泄漏类;`_FORWARD_ENV` 264→265），阈值 0.62/0.40
  自动切档，enrollment=running-mean(前 3 确证段,relock 重置)；缺位=v1 字节不变。
  TTS 四嗓音标定：同人 ≥0.78|异人 max 0.57|噪声≈0（v1 零分离→真分离）。
  **实弹**：ECAPA 档栈 canto E2E PASS、`probe_speaker_lock` PASS（零误杀/纯环境
  音 3/3 判丢 sim≈-0.03/端到端活）。全量 4918。
- **v2 遗留（明账）**：阈值=TTS provisional,真人通话 `SPEAKER_LOCK_DROP/RELOCK`
  分布终裁；模型分发未建（他机跑导出件）；B 线 interpret 未接锁。

**第二波待办**（合流后）：
- 实弹剧本验收：barge_in 基线不变 + 短应承/假打断连发探针轮 + `MINIMAX_TTS_ZERO_AUDIO_PAD` 频率观测 + `BOK_AMBIENT_SCENE=callcenter` 实弹一通 + 挂断 10 通零 DataStreamError。
- W2c 后置门：probe_cloud_asr 四语小语料实测（豆包宣发≠实测）。
- W4：B 线控制台双栏成组+label 统一「翻译」+听对方翻译 toggle（agent 侧 `_apply_track_permissions` 放开 me- 订阅）。
- W5：声纹锁（默认关，独立 PR）。
- W3 残留：script/罐头线 79% 出声轮无标记=步文案带标记+重物化，独立票；sighs/chuckle/laughs 人耳终裁（台架已物化）。
- W1g 残留：`entrypoint did not exit in time` 的 12s close-flush 窗口未动（另一张票）。
- 候选评估票：audition symlink 在 Next 静态导出的拷贝行为（CI build 裁决，红则改实体拷贝）。

**W4c+B 语气词 v2（2026-10-08，call-996f3917 实证）**：
- 病灶①：`_apply_voice_tags` 只认句首——句尾「呃，你在说什么？哈哈哈。」译出
  「…? Hahaha.」漏网被 2.8 逐字念出。修=v2 三层（白名单归一（全角/大写→ASCII）
  →句首引导词（旧契约）→**任意位置**笑声簇（拉丁词边界+CJK 哈哈/嘻/嘿/呵）），
  产出恒 ⊆ A 线 `VOICE_TAG_WHITELIST`（`voice_style.norm_voice_tag` 公开共用，
  A/B 词汇不双轨,parity test 钉死）。
- 病灶②：纯笑句「哈哈。」整句转 `(laughs)` 后字幕译文列**空白**（`_strip_voice_tags`
  剥后空串）。修=`_caption_text`（字幕/落库口径）：剥后空/纯标点→本地化占位
  「（笑）/(laughs)」；web 侧 `subText` 同口径。
- 病灶③（上下文层）：本地 Hy-MT2 对模板外指示无视（2026-09-16 实测留档）——
  确定性层兜底两车道；云端/回退 LLM 车道 `_translation_instructions` 加语气规则
  （笑声/叹气/咳嗽按语境转标记,至多 1 枚/句）。门关路径 `_speech_text` 也剥
  （云端 MT 产出的标记在非 2.8 档念出来=假人念稿）。
- 病灶④（W4c 列语义）：Ethan 拍板「我说的原文和译文是一列的,对方说的原文和
  译文是一列的」。旧版译文按**译文语言**分列（原文译文分家,pairSubtitles 同侧
  配对恒落空=孤儿泡满屏）+ `Object.values(Map)` 恒空（livekit `trackPublications`
  是 Map）→ trackSid 查找**从未生效**、全部译文落 fallback（flow 都错）。修=
  `whoIs` 按 flow 分侧（fwd 译文→我方列）+ Map `.values()` 迭代 + 列头语言对
  源→译；whoIs 入 node 测试标记区（结构化 SubRoomLike 替身,14/14）。

**W4d 麦设备角色·虚拟设备排除（2026-10-08，call-933945a5「我听到的译文是对方
说话的 TTS」）**：web-client.log 铁证三连——①会话头 `role_conflict` FATAL（两侧
麦=同一支默认 Built-in，fatal 判定层工作正常）；②saved 我方麦=AirPods 失联 →
stale 回落默认=与对方麦同支；③auto-assign 补位时 `realMic` 只排 default 伪条目，
**BlackHole 2ch (Virtual) 混进候选按枚举序中选**——虚拟回环设备当人麦。rev 腿
`audio=text-only`（无 TTS）排除翻译方向 bug；听到的 TTS 全来自 fwd trans-en，
内容装着对方语音=两个 agent 在吃同一支麦/虚拟回环。修=`isVirtualAudioDevice`
纯函数（BlackHole/Oray/Soundflower/Loopback/Voicemeeter/VB-Audio/GroundControl/
Dante 族）进 `device-roles` 单源；console realMic/realOut 双过滤硬排除（候选不足
≥2 就不分配,fatal 红字继续当诚实信号）；`deviceRoleIssues` 虚拟槽=warn（用户刻
意路由实验不拦死）。回归测试用该通真实设备表（过滤后 BlackHole/Oray 出局,我方
补位=AB13X 真实件）。**已知残余**：stale 设备重连不自动复归（saved 只存 id,蓝牙
重连换 id 无从匹配）——AirPods 掉线又回来需手动重选,独立票。

## 风险与不做的事

- A 线语言三态立法不动（粤语规范值 `cantonese` 全栈唯一拼写——新语种只进 B 线语言面，
  不进 `LanguageState`/voice-map/A 线装配）。
- emotion 参数逐轮下发不开（架构决议）；语速统一 1.2 不动。
- livekit-agents 1.8.2 锁版，W1a 只在我们插件层守卫，不碰库。
- 马来语/阿拉伯语 ASR 支持未证实——probe 先行，UI 标注限缩，不硬上。
- 每波独立 PR、全量绿+CI 绿才合、`[SESSION_ID]` 纪律；Mimosa 门输出每次提交必读。
