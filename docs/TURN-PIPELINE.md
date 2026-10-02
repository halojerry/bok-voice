# A 线单轮判序全景（2026-09-26 定稿，含 Laya 意图旁路）

> 一轮客户话从音频到出声的完整判序图。实现锚点：`apps/agent/agent_runtime/agent.py`
> 每轮 `on_user_turn_completed` + `livekit_plugins.py`（ASR 层）+ `qa_gate.py`（快路）+
> `fillers.py`（垫音）。改漏斗顺序前先读 AGENTS.md「话术分步推进」与「罐头音三件套」条目。

## 1. 成轮层（ASR，一句话怎么变成"一轮"）

三事件源：VAD 停嘴 0.35s / 句级标点 / 10 字稳定长度档（连续语流兜底）。
前置闸（按序）：smart-turn 语义闸（仅 zh/en，p<0.5=没说完→hold 等续段并入）→
join-hold 拼接（数字/系词收尾 800ms 等下段，治报号切碎）→ 回声守卫（AI 说话中
自闻 ≥0.9 相似整轮丢弃）→ 纯犹豫残片门（≥2 语气字不成轮）→ 迟到 FINAL 守卫
（告别直念窗不打断；极短数字尾巴丢弃）。

## 2. A 线漏斗（每轮按序过；命中即出声即结束本轮）

| # | 车道 | 触发条件 | 出口 |
|---|---|---|---|
| ① | WA 号码累积 | 报号碎片（<8 位数字/自报头「我的WhatsApp係」） | 暂存不出声，拼完整再侦测+复述确认 |
| ② | say 直念步 | 轮开始时当前步是待念直念步（say:1） | ref 首行直念（合规内容永不被跳过） |
| ③ | 分支动作 | 运营在步 ref 配「如果客户X→【收线/转人工/跳第N步/留本步】」 | 收线=双护栏（条件字面命中或 verdict=REFUSE+热词幻觉否决）；留本步=hold 挡规则推进 |
| ④ | REFUSE 收线 | 确定性拒绝正则 | 告别直念+收线 |
| ⑤ | DEFER 短应承 | 「我先查一下/稍等」类社交拖延 | 直念三语短应承，不推进不判 |
| ⑥ | 流程推进判定 | 规则正则 CONFIRM→auto_advance；模糊轮→后台 LLM judge（异步下一轮生效，WA 收号步有假确认护栏） | flow current 推进 |
| ⑦ | **话术图意图**（四级识别） | 见 §3 | 绑定动作：play_qa（+可选 then_jump）/ jump_step / notify_human（打铃不抢话）；once 账本防重复 |
| ⑧ | QA 快路 | 七道旁路闸（advanced/closing/wa/digits/refuse/verdict 全过）→ 0.6 余弦+0.4 子串 ≥0.90（词面没中→语义补位 0.80）→ 罐头已物化 | ~50ms 罐头直播；无录音落 LLM |
| ⑨ | 自由 LLM | 以上全未中 | a_reply 车道（本地/云端路由）→ MiniMax TTS |

closing 后图让位告别轮（⑦ 不抢收线话）。

## 3. 意图识别四级（⑦ 内部，2026-09-26 起）

| 级 | 机制 | 时延 | 生效轮 |
|---|---|---|---|
| 1 | 确定性关键词（双侧 casefold 子串，priority 小先） | 0 | 当轮 |
| 2 | 9B pending（上轮后台判，single-shot TTL） | — | 本轮消费 |
| 3 | 语义车道（词面未中 embedding 匹配） | ~百 ms | 当轮 |
| 4 | **Laya 当轮快判**（docs/LAYA-EVAL.md；23-100ms，conf≥0.5 采纳；低置信弃权→排 9B 后台判下一轮兜底） | 23-100ms | 当轮（高置信）/下轮（弃权回落） |

准确率分层保证：确定性层 100% 精确先走；Laya 只在高置信当轮触发（宁可慢半拍不误触发）；
实弹：ASR 烂转写轮 conf 0.47/0.09 全部正确弃权由 9B 兜底命中。

## 4. 出声来源

| 说什么 | 什么时候 | 来源 | 延迟 |
|---|---|---|---|
| 开场白 | 接通第一句 | 话术第 1 步 ref 首行脚本直念 | 零 TTFT |
| 直念步 | 进入 say:1 步当轮 | ref 首行罐头 | 零 TTFT |
| QA 快答 | ⑧ 命中且已录音 | pregen 罐头 | ~50ms |
| 图播快答 | ⑦ play_qa | 同罐头出口 | ~50ms |
| LLM 回答 | ⑨ | 本地 LLM→云 TTS | 1.9-2.5s |
| 心跳补位 | 8s 静默 | 三骨架轮换脚本直念 | 零 |
| 收线告别 | REFUSE/收尾 | 脚本直念 | 零 |
| WA 复述确认 | 号码捕获 | 三语脚本直念 | 零 |

## 5. 垫音（fillers.py）

- 触发：本轮回复首音频 >500ms（BOK_FILLER_DELAY_MS）未到 → **out-of-band 音轨**
  （绝不走 session.say——speech 队列串行会排到回复后面）。
- 选池：classify_filler_category 按客户话分五类（empathy/ack/check/minimal/default）
  ×20 条/语言；语言=本通钉死语言，池缺失明文跳过绝不跨语言。
- 双层：先查 tts-cache 人设音色物化版（同声音）→ miss 播源码资产（assets/fillers/
  manifest.json）兜底+后台补物化（日志 BOK_FILLER voice_fallback）。
- 节奏：垫话播完 → 300ms（BOK_FILLER_GAP_MS）→ 回复首帧（不掐垫话）。
- 纪律：相邻轮垫一轮歇一轮（防轰炸）；每通限 BOK_FILLER_MAX；单轮链发 ≤1
  （垫完回复还没到自动补一发）；垫话已盖耳→抑制 2s 超时道歉句（防三连叠音）；
  不进 LLM 上下文（KV 前缀/回声锚零污染）；让话家族（「继续讲/聽住」）已从选池剔除。

## 6. 闭环

意图：触发→执行→记账（once/graph_fired/turns provider=graph-jump|graph-play）→
学习回写（漏网轮挖掘→画布意图关键词提案→运营确认→下次关键词当轮命中）。
QA：命中→播放→账本（qa_played 轮换/gen=qa_fastpath）→挖掘（qa-pairs/cluster）→
采纳→pregen 物化→体检（drift retire/reanswer）。两级皆有账本有回写。
已知非闭环点：judge 判据意图的 9B 路径下一轮才生效（Laya 高置信轮已当轮化）——
准确率换的，非缺陷。
