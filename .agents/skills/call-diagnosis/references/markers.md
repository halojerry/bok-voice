# agent.log 标记词汇表

日志在 `~/Library/Application Support/BokVoice/logs/`（时间戳 UTC，本地 +8）。插件层打印不带 call id，靠行号窗口/时间对齐归属。

## ASR 层（QWEN3_*）

| 标记 | 含义 |
|---|---|
| `QWEN3_ASR_TEXT '...' zh ASR_MS=N(stream)` | 滑窗 partial 解码文本（/api/chunk 响应） |
| `QWEN3_ASR_SENTENCE_COMMIT source=vad-pause/partial-punct chars=N '...'` | 说话中途句级提交成轮（vad-pause ≥10 字门；punct ≥6 字门） |
| `QWEN3_ASR_JOIN_HOLD src=digits/copula/vocab` | 微停顿跨段拼接 hold（续接可能句唔发 EOS 等下段） |
| `QWEN3_ASR_JOIN_FLUSH chars=N` | hold 超时整段发出 |
| `QWEN3_ASR_REDECODE_DROP committed=... finish=...` | 停嘴重解与已提交碎片相似 ≥0.55 且长度相当 → 丢弃（纯质量重解） |
| `QWEN3_ASR_REDECODE_TAIL committed=... tail=...` | 重解更长=同头+新尾，对齐截尾照发（2026-09-12 起） |
| `QWEN3_HOTWORD_ECHO_DROP src=stop-mouth/join-flush/interim/...` | 词表回声整条丢弃（纯回声/无分隔符顺串/seen 后孤词） |
| `QWEN3_HOTWORD_ECHO_STRIP heard=... keep=...` | 剥尾保头（真话头+词表尾）——keep 即净文 |
| `QWEN3_ASR_HESITATION_DROP` | 纯犹豫残片（呃呃呃呃类 ≥2 语气字）不成轮 |
| `QWEN3_ASR_HINT` / `ASR_MS` | 语言 hint / 解码耗时 |
| `QWEN3_ASR_PREFLIGHT chars=N` | 稳定前缀发框架（抢跑/speculator 用） |
| `QWEN3_ECHO_SELF_HEARD_DROP` | AI 自听回声整轮丢弃（AEC 失效自闻自答守卫） |

## 流程层（[flow]）

| 标记 | 含义 |
|---|---|
| `rule=auto step=N` | 规则级必定推进（平台词/通知步宽松/开场） |
| `rule=confirm step=N` | CONFIRM 推进 |
| `judge(bg)=confirm/objection/unclear step=N` | 背景 LLM 判定（含 `blocked (wa step...)`/`blocked (no ack signal)` 拦截） |
| `say-step verbatim step=N chars=N` | 直念步整段照念（跳 LLM） |
| `refuse -> closing` | 进收尾态 |
| `defer-ack` | DEFER 短应承直念（2026-09-12 起） |
| `[whatsapp] captured/offered num=...` | WA 号码捕获 |

## LLM/TTS/体感

| 标记 | 含义 |
|---|---|
| `LLM_TTFT_MS N (official) cached=X/Y prompt=Z gen=G tps=T` | 首 token 延迟+缓存命中+生成量；cached 比值低=前缀分叉/大跳变 |
| `LLM_REQ_MS header=N msgs=M` | 请求发出耗时+消息数 |
| `TTS_FIRST_AUDIO_MS` / `AGENT_METRICS tts ttfb=... audio=Ns` | TTS 首包/整段时长（audio>10s≈回复过长） |
| `MINIMAX_BIDI_PERF sentences=N canceled=0/1 first_audio_ms=Y` | sentences=0+canceled=1=回复零音频被掐（碎片饿死铁证） |
| `MINIMAX_TTS_STALL` / `MINIMAX_TTS_BIDI_DEAD` | TTS 看门狗/连接死亡 |
| `PERCEIVED_MS total=T (eou=A llm=B tts=C)` | 北极星：讲完→出声三段拆账 |
| `REPEAT_SELF_SUPPRESSED sent=...` | 出口复读防线剥掉复读句（2026-09-12 起） |
| `TAIL_ANCHOR_MIMIC_SUPPRESSED` | 剥掉拟声复刻的【你上一句】块 |

## 垫话/罐头/QA

| 标记 | 含义 |
|---|---|
| `BOK_FILLER fired count=N line=...` | 垫话出声（连发=回复饿死信号） |
| `BOK_FILLER hold reply Nms` | 垫话播完+gap 后衔接回复（正常时序） |
| `QA_FASTPATH bypass reason=...` / `QA_FASTPATH_SUMMARY` | 快路闸门归因（match0=词条不中，verdict/digits/refuse/wa/advanced=旁路） |
| `TTS_CACHE hit=1 chars=N` | 罐头缓存命中 |

## 心跳/会话

| 标记 | 含义 |
|---|---|
| `[heartbeat] silent 8s -> nudge N/M` | 心跳补位 |
| `closing agent session due to participant disconnect` | 客户端挂断（正常收尾） |
| `draining worker` / `exiting forcefully` | worker 被杀（down/竞态）——之后无新日志=worker 没起来 |
| `registered worker` | worker 注册成功（serve 后应见 3 条：bok-voice/fwd/rev） |

## 殭尸/版本陷阱

- `bok.py down` 杀不净失联 worker（orphan sweep 只扫部分）——A/B 或诊断前 `ps aux | grep agent_runtime` 必须为 0。
- 双 worktree 共享 app-data：main 的 serve 会把别的 worktree 起的进程当已就绪跳过，版本混搭无告警。
- 桌面包跑 `_up_/` 旧代码；dev 栈跑 worktree 代码——先确认日志里的路径。
