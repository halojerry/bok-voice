# Bok Voice 运行时关系拓扑（RUNTIME TOPOLOGY）

> 本文件是"安装后的 App 能不能正常使用"的唯一验收基准。任何改动必须保证
> 按这张图跑起来：组件齐全、端口可达、数据落在 app-data、bundle 只读。

## 0. 分发型拓扑（P0 起双形态，spec=2026-09-10-thin-node-saas-design.md）

单机形态（本文其余部分描述的 dev/打包形态）不变。分发货形态新增：
- **云 CP**：同一 control-plane 代码，`DATABASE_URL` 指 Supabase Postgres；托管管理台静态 UI
- **节点包**：LiveKit + ASR/LLM sidecar + agent/interp worker + node-agent（tools/node_agent.py，
  心跳 :8000/api/nodes/heartbeat，commands 通道 P3）+ 节点本地托管坐席 UI（runtime-config.js 注入
  cpUrl/livekitUrl，spec §8 纯内网档）
- 新数据列：turns.org_id/line/speaker/gen/template_step/started_ms/ended_ms/perceived_ms（分析账本，spec §6.1）
- 新表：orgs/nodes（org 缝 + 节点注册表，node_token 只存 sha256）

## 1. 组件与端口

| 组件 | 端口/协议 | 职责 | 运行时 | 数据落点 |
|---|---|---|---|---|
| Tauri Shell (Rust) | — | 打开窗口、拉起 `bok.py serve`、托管静态前端 | 打包内 | — |
| `bok.py serve` | — | 编排：启动顺序、健康、模型下载 | 打包 Python | pid 文件 → app-data/run；日志 → app-data/logs |
| control-plane | :8000 HTTP | 业务 API、知识、设置、审计、LiveKit token | 打包 Python | SQLite → app-data/bok_voice.db |
| ASR sidecar | :8787 HTTP | 三语转写（zh/cantonese/en） | Mac=mlx_audio；Win=qwen-asr+CUDA | 模型 → app-data/models |
| TTS sidecar | :8788 HTTP | 合成 / 克隆 / 试听 | Mac=mlx_audio；Win=qwen-tts | 模型 → app-data/models |
| LLM | :1235 OpenAI 兼容 | A 线对话（flow judge、CP 摘要同源）；B 线翻译回退 | Mac=mlx_lm；Win=llama-server CUDA | 模型 → app-data/models |
| MT LLM（可选） | :1236 OpenAI 兼容 | B 线同传专用翻译（Hy-MT2 小模型，逐句无状态；模型缺失自动跳过 → B 线回退 :1235） | Mac=mlx_lm | 模型 → app-data/models |
| B-line worker | :8790 WS | 同传通道：ASR→翻译→TTS 队列 / 背压 | 内嵌 Node | 指标 → app-data/translation-metrics.jsonl |
| LiveKit server | :7880 WS/WebRTC | RTC 信令与媒体（7881/7882 RTC 端口） | 内嵌二进制 | keys → 内嵌 livekit.yaml |
| agent worker | 进程（健康 :8081/worker） | A 线智能体（VAD/对话/情绪/打断） | 打包 Python | 调 8787/8788/1235/8000；TTS=MiniMax 云（`tts_cache` 本地音频缓存叠加） |
| interpreter worker ×2 | 进程（健康 :8082 fwd / :8083 rev） | B 线双 AgentSession 同传（`bok-interp-fwd/rev` 显式分发） | 打包 Python | 调 8787/8788/1236(MT,回退 1235)/8000；TTS=MiniMax 云(或本地 8788) |

### 本地 TTS 音频缓存 + 垫话 + Q→A 快路（2026-09-09，`docs/superpowers/specs/2026-09-08-*-design.md`）

- **缓存**：`agent_runtime/tts_cache.py`——`CachedTTS` 包装 MiniMaxTTS（仅拦
  `synthesize()` 整句路径，stream 透传），key=sha1(归一化文本+音色+模型档+采样率)，
  PCM 存 **app-data/tts-cache/**（LRU 500 条）。脚本直念线（开场白/心跳/收线/WA 确认）
  经 `_say_script`：命中 ~0ms 出声，未命中边播边落盘。开关 `BOK_TTS_CACHE=0`。
- **预生成**：`bok.py tts-pregen`（--greetings 无变量脚本线 / --objects 逐对象
  开场白收线心跳 / --qa 快答库启用条目应答 / --fillers 按人设音色物化垫话，
  2026-09-10 双层出声复活）——离线批量合成，需 CP 或 MINIMAX_API_KEY。
  greetings/fillers/qa 落盘**打钉不逐出**（逐对象开场白与运行时 tee 不钉，
  LRU 500 只淘汰未钉条目）。**人设保存点自动物化（W3）**：CP POST/PUT
  /api/personas 音色变化 → 后台 detached 子进程跑 `scripts/pregen_tts.py
  --greetings --fillers --qa --persona <id>`（新会话不被栈重启打断，单飞，
  `BOK_PERSONA_AUTO_PREGEN=0` 关），日志 **app-data/logs/tts-pregen.log**，
  响应 `tts_pregen.status` 即提醒面。
- **垫话**：`agent_runtime/fillers.py`——LLM 慢轮回复首音频 500ms 未到播预合成
  应承语。**2026-09-10 资产化改版**：垫话=随源码分发 wav 资产（`assets/fillers/`
  +manifest，`scripts/gen_filler_assets.py` 固定音色/参数预生成，三语各 10 短句
  万能话术 1.0-1.5s），运行时只播文件绝不云合成；**播放排序=垫话播完→300ms
  （`BOK_FILLER_GAP_MS`）→回复**（不再掐垫话，回复首帧经 `_RelaySynthesizeStream`
  hold 扣压）；**链发**（2026-09-10）：首条播完回复仍未出声 → gap 后自动补第二发
  （`BOK_FILLER_CHAIN` 默认 1，每轮封顶 1 次、与主动播共享 `BOK_FILLER_MAX=3`
  计数；池 39 条=每语言 10 短 1.0-1.5s+3 长 1.7-2.3s）；语言=装配时钉死的通话语言，
  池缺失跳过绝不跨语言。垫话绝不进
  LLM 上下文。开关 `BOK_FILLER=0`。
- **抢跑防抖 + PrefillSpeculator**（2026-09-10）：框架抢跑默认关
  （`PREEMPTIVE_GENERATION=0`，命中在本仓 STT 架构下结构性不可能——PREFLIGHT
  只发稳定前缀而提交是全句 FINAL，843 失效/0 命中实测）。替代预热=
  `prefill_speculator.py`：说话中按稳定前缀发 max_tokens=1 out-of-band 请求
  （严格前缀=上次真实请求快照+回复历史原文+user 前缀），真轮只 prefill 分叉
  尾巴；`BOK_PREFILL_SPEC=0` 关。诊断 `BOK_PREEMPTIVE_DEBUG=1` +
  `scripts/probe_preemptive.py`。
- **turns 分析账本**：A 线 `_on_conversation_item` 每轮上报
  line/speaker/gen/template_step/started_ms/ended_ms/perceived_ms（B 线
  `line=b`、speaker=me/other）；`gen`=llm/script/qa_fastpath 生成源；
  perceived_ms=eou+llm+tts 三段（时序：item_added 时 pending 已就位即取）；
  上报任务挂断 flush 防 teardown 丢轮。
- **Q→A 快路**：`agent_runtime/qa_gate.py` + CP `/api/qa-entries`、
  `/api/reports/qa-pairs`——四道闸（作用域/关键信号旁路/推进收线让位/阈值 0.90）
  全过且应答音频已缓存才跳过 LLM；挖掘入库 `bok.py tts-mine --apply N`。
  开关 `BOK_QA_FASTPATH=0`。

### 音频设备（设置页）

- 桌面壳（macOS）通过 Tauri `list_audio_devices` / `set_system_output`（CoreAudio）
  枚举并切换**系统默认输出设备** —— A 线远端 `<audio>` 与 B 线 WebAudio 都跟随。
- 麦克风：macOS 打包需 `NSMicrophoneUsageDescription`（Tauri 合并
  `desktop/src-tauri/Info.plist`）与 `com.apple.security.device.audio-input`
  entitlements，否则 TCC 静默拒绝 → 设备列表为空。
- 选择持久化在 localStorage（`bok.audio.mic` / `bok.audio.out`），接通/开始采集时应用。
- **CoreAudio 注意**：CFString 属性（设备 UID/名称）由 CoreAudio 以「对象指针写入
  outData」返回，须用 `CFStringRef*` 接收并 `CFRelease`；按字节缓冲解引用会
  SIGSEGV（设置页打开即崩）。`cargo test` 有真机枚举回归用例。

## 2. 数据流

### A 线（客服语音助手）

```text
浏览器/客户端 (WebRTC)
  → LiveKit :7880（信令/媒体）
  → agent worker（VAD 切句 → ASR :8787 转写 → LLM :1235 生成 → TTS :8788 合成）
  → 音频轨回放
同时：通话/转写/结算/审计 → control-plane :8000 → SQLite（对象、人设、知识、模板、设置、审计）
```

> **前端就绪自愈**：桌面壳异步拉起整栈服务，WebView 先于服务就绪加载。前端
> `lib/api-ready.ts` 的 `useControlPlaneReady` 轮询 `/health`，Control Plane 就绪后
> 自动重拉对象/人设等数据；`TypeError: Load failed` 不再直接上屏，而是映射为
> 中文提示「本地服务启动中/无法连接」。
>
> **URL 归一**：前端 API 基址、B 线 WS、LiveKit、agent 的 CONTROL_PLANE_URL 一律
> 默认 `127.0.0.1`（服务只绑 IPv4；避免 macOS localhost 优先解析 ::1 导致
> fetch 恒定失败）。构建/打包（verify_bundle.sh）会校验 `out/` 不含
> `http://localhost:8000`。

### Supervisor（主管台）

- 暂停/接管：control-plane 把通话置 `paused` 或 `escalated_to_human`；agent 的
  `_supervisor_watch` 每 2s 轮询通话状态 → 暂停自动回复（`on_user_turn_completed`
  抛 `StopResponse`）并 `interrupt(force)` 打断当前发言，人工接管会话。
- 恢复：`POST /api/supervisor/{id}/resume-agent` 把状态置回 `active` 并清
  `escalated_to_human`；agent 恢复自动回复。
- 转人工：置 `ended` + `disposition=transferred`，agent 退出并触发结算。
- 拒绝收线：客户明确拒绝/告别（`flow.py` REFUSE 判定）→ agent 注入收尾话术讲一句
  礼貌再见，随后 `POST /api/supervisor/{id}/end` 置 `ended` + `disposition=declined`
  并断房，结算由 agent `_on_close` 幂等触发。

### 外呼战役（mock 档，spec 2026-09-12-outbound-campaign-roster）

```text
web /campaigns（建波/启停/进度表）
  → CP POST /api/campaigns（object_ids 名单 + scenarios/scripts mock 剧本钩子）
    + POST /api/campaigns/{id}/start
  → CP 常驻 campaign loop（campaign.py `_campaign_loop`，5s 巡检 POLL_S）
      ①收割：dialing/in_call 的 item 其通话已终态 → item 落结果（幂等）
      ②串行：无进行中 item 且有 pending → 建通话 + explicit agent dispatch
        （metadata 带 `dial` 块），item 置 dialing；**任意时刻至多 1 路在跑**
      ③名单尽 → campaign done
      起拨前 gap 冷却：最近终态 item 距今 < gap_seconds 不起下一通（首通不受门控）
  → agent 收 metadata `dial` 块 → dial_outbound（dialer.py，四态出口）
      real 档：官方 CreateSIPParticipant(wait_until_answered) + SipCallError 码映射
               （486/603 拒接、408/480 无人接、5xx trunk 故障）；需 Redis + 公网
               reachable 的 trunk，本地 mock 档无需
      mock 档：CP `POST /api/sip/mock/callee` 派生 scripts/mock_callee.py 子进程
               （真 TTS 客户语音进房；answer 逐句轮播 / no_answer 不入房 /
               reject 进房即离 / hangup_mid 说一句就走）
        · 台词从 dial 块 `script` 下发，空台词按语言默认 2 句兜底
        · `speak_interval_s` 控句间隔；子进程会等 AI 讲完（对端音轨能量）
          再出声，避免与开场白撞轮
      → wait_for_participant：超时=no_answer、进房 1.5s 内离房零音频=rejected
  → agent `PUT /api/calls/{id}/dial-result`（answered→ACTIVE，三失败态→ENDED+
    disposition）→ CP 按 call_id 反查 campaign item 同步状态（只认 dialing/in_call）
  → 接通后走正常 A 线装配（开场白=话术第 1 步直念）；captured 号码自动入名册
  → web /roster（认领池：unclaimed → claimed → handled）
```

- `BOK_SIP_MODE` 是 dial 后端 kill-switch（有值即终局：`mock`/`real`，非法值
  回落 mock）；缺省读设置 DB `sip.mode`（设置页 SIP 卡片）。agent 侧
  `resolve_dial_mode` 与 CP 侧 `_dial_mode` 同语义双实现（跨包分层，CP 不 import agent）。
- mock 客户子进程日志落 `runtime/logs/mock-callee.log`（`MOCK_CALLEE event=…`
  结构化行，E2E 断言素材）；房间断开立即收尾，CP 起子进程后起 daemon reaper 防僵尸。
- campaign 名单由 `POST /api/campaigns` 一次建仓：对象无电话 → item 直接 `skipped`
  （且不作 gap 冷却锚）。
- mock 剧本钩子（`scenarios`/`scripts`/`mock_speak_interval_s`）只服务演练与 E2E；
  campaign 级存 `campaigns.scripts_json`（无独立列，`__` 前缀键放 campaign 级参数），
  起拨时按 object_id 取台词塞进 dial 块 `script`。真实 SIP 拨号恒为空。
- 全链路 E2E：`python scripts/e2e_campaign.py`（3 对象战役——1 接通走完话术+captured
  入名册 / 1 无人接 / 1 接通即挂；断言串行、终态三态、名册入册与 handled 回写）。

### B 线（同声传译 v2，LiveKit 双端）

```text
web /interpret 两端（me/other，各自选麦克风/扬声器）
  → CP /api/calls(kind=interpret) + /api/token(role=me|other)
  → LiveKit 房间（me-<room> / other-<room> + 两个 interpreter agent）
  → interpreter(AgentSession 全托管):silero VAD → Qwen3-ASR(源语言钉死,
    cantonese 走 hint) → MT 翻译模型(:1236 Hy-MT2,官方模板逐句无状态、
    历史不累积;未部署自动回退 :1235 主 LLM) → MiniMax 云 TTS(默认 turbo 档,
    按 target_lang 三键换音色 + language_boost;设置非 minimax 时本地 Qwen3-TTS 回退)
  → 译文轨 trans-<lang> 只授权对方订阅(set_track_subscription_permissions);
    字幕 lk.transcription 全量广播(原文+译文,前端 useTranscriptions 渲染)
  → 译文句 add_turn(原文：…\n译文：…) → 房间断开 settle → 总结/知识蒸馏/vault
agent worker:A 线 `agent_name="bok-voice"` 显式分发(官方推荐;隐式 dispatch 已废除,
杜绝同传房被客服 agent 隐式抢派)。operator/supervisor token 由 CP /api/token 挂
RoomAgentDispatch(metadata={call_id})精确派发;CP /api/token 即官方 TokenSource
endpoint 契约({serverUrl, participantToken},201),官方 SDK 可直连。
interpreter worker:bok serve 起 2 个常驻进程(interp-fwd/rev,agent_name
bok-interp-fwd/rev 显式分发,方向语言对+精确 identity 由 me 端 token 的
RoomAgentDispatch metadata 下发;无房间时空闲,job 到达才拉管线)。
三个 livekit-agents worker 健康端口显式分拆(A 线 8081/interp 8082·8083,
WorkerOptions.port)——默认同为 8081 会竞态,后绑者 Errno 48 即崩
("Agent did not join the room" 根因,2026-09-06)。
旧 v1(/translate + WS :8790)冻结保留作 POC,不再迭代。
```

## 3. 生命周期

### 启动顺序（`bok.py serve`）

1. 确保 app-data 目录（run/logs/models/vault）存在
2. control-plane :8000（注入 `DATABASE_URL=sqlite:///<app-data>/bok_voice.db`、`VAULT_ROOT=<app-data>/vault`）
3. LiveKit :7880（内嵌二进制 + livekit.yaml；不依赖 Docker）
4. ASR :8787、TTS :8788、LLM :1235（并行拉起）
5. B-line :8790（注入 app-data 配置文件）
6. agent worker（注册到 :7880）
7. 轮询全部端口 UP → 前端可用

### 关闭（`bok.py down`）

按 pid 文件逐个 SIGTERM（run/*.pid）。Tauri 退出时调用 `stop`。

### 失败处理

- 任一服务超时未 UP：`bok.py serve` 返回非零，日志在 app-data/logs，不静默继续
- 模型缺失：首启向导 `setup status/download`，幂等 + 断点续传
- 硬件不满足（Windows 无 NVIDIA GPU）：`doctor --packaged` 阻止 LLM 启动并给文案

## 4. 路径约定

| 路径 | 可写 | 用途 |
|---|---|---|
| bundle（`.app/Contents/Resources`） | 否（只读） | 代码、Python 运行时、二进制、静态前端 |
| `~/Library/Application Support/BokVoice`（win `%LOCALAPPDATA%\BokVoice`） | 是 | models / vault / logs / run / bok_voice.db / audit / bline.json |
| `~/.lmstudio/models` | 只读引用 | 本机开发/软链复用（`--` 目录名映射） |

## 5. 默认配置与环境变量

打包模式（`BOK_PACKAGED=1`）由 `bok.py serve` 注入：

| 变量 | 值 | 作用 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<app-data>/bok_voice.db` | 业务数据持久化 |
| `VAULT_ROOT` | `<app-data>/vault` | 知识库 markdown 落盘 |
| `LIVEKIT_URL` | `ws://127.0.0.1:7880` | control-plane 签 token 时下发的服务器地址 |
| `LIVEKIT_API_KEY` | `devkey` | control-plane `/api/token` 签发真实 JWT（缺失会 503） |
| `LIVEKIT_API_SECRET` | `devsecret` | 同上；与 livekit.yaml `keys` 一致 |
| `BOK_BLINE_CONFIG` | `<app-data>/bline.json` | B 线通道配置（ASR/TTS/翻译/指标路径） |
| `QWEN3_TTS_DATA_DIR` | `<app-data>/tts-data` | TTS 语音克隆注册数据（registry + 参考音频），bundle 只读/可升级 |
| LLM 默认 | `provider=local_openai` + `http://127.0.0.1:1235/v1` | A/B 线共用本地 LLM |
| 服务绑定 | 127.0.0.1 | 仅本机可访问 |

### 设置（`/api/settings`，Agent 运行时会真实消费）

- `asr.provider`：`qwen3_asr`（本地 sidecar）/ `fake`（仅测试）。语言值统一 `zh/cantonese/en`（粤语全时空唯一拼写 `cantonese`）；agent 在会话语言为粤语时给 sidecar 传 `language=cantonese` 强制模型按粤语转写，避免 auto 误判成普通话。
- `llm.provider`：`local_openai`/`mlx`（本地）/ `deepseek`（云端，缺 `api_key` 显式告警并回退本地）/ `fake`。
- `tts.provider`：`qwen3_tts` / `volcano_streaming`（需 `VOLC_*` 环境变量）/ `fake`（静音测试音，非火山 beep）。
  音色兜底按语言 `speaker_zh/speaker_cantonese/en`（旧拼写键已由启动迁移改写）；persona 绑定 `reference_audio` 优先。
- `vad`：`provider` + `max_buffered_speech` / `min_speech_duration` / `min_silence_duration` / `interruption`  —— 直接构造 `inference.VAD` 与打断开关（环境变量 `VAD_*` 仅作部署覆盖）。
  基线默认（2026-09-05 句号级提交落地后）：`min_silence_duration=0.45`、`min_speech_duration=0.15`；
  A 线 turn_detection=`stt`（STT 句末 END_OF_SPEECH 提交，句级 FINAL→EOS，说话中即提交，
  数字串/短句/1.5s 限流保护；续接可能句——归一后 ≥2 位数字或系词收尾——会在句末被扣住
  `QWEN3_ASR_JOIN_HOLD_MS`（默认 800，0=关）等续段并入同一 sidecar 会话、一条 FINAL
  覆盖全段，超时由 flush 补发（该轮多等 ≤HOLD ms；flush 与正常停嘴同一套短尾规则并带
  会话纪元守卫，finish 等待期续讲开新会话唔会被 reset 清轮）），
  endpointing `min_delay=0.25`/`max_delay=0.6`。
- `sip`（Wave2）：`mode`（`mock`/`real`，外呼拨号后端；env `BOK_SIP_MODE` 优先且
  有值即终局）/ `trunk_id` / `address` / `auth_username` / `auth_password`（secret 掩码：
  GET 回空串 + `has_auth_password`，PUT 传空=保留旧值）/ `numbers`（本端号码池）/
  `ringing_timeout_s`（默认 30）/ `max_call_duration_s`（默认 600，mock 档作 agent 侧时长
  保险丝）。切 `real` 的前提：已注册 SIP trunk + Redis（LiveKit 的 SIP 服务依赖）+
  公网可达；未满足时保留 `mock`。
  语言钉定 + 热词：每通对话语言钉死随 `/api/start?language=` 下发（Chinese/English/Cantonese
  规范名）；热词 context（官方 customizable context，system message 词汇表软偏置）随
  `/api/start?context=` 下发——话术模板 hotwords 字段 + 话术领域词 + 对象文字字段
  （`BOK_ASR_HOTWORDS=0` / `QWEN3_ASR_CONTEXT=0` 两级回退）。
  （历史警戒已失效条件化：当年压端点致哑火=轮次在离线 ASR final 前提交；现提交结构性等待
  STT FINAL 且句级路径 FINAL 即句文，三语 E2E 实证 0 丢转写。回退开关：`TURN_DETECTION=`
  置空回 EOT 模型档 + `QWEN3_ASR_SENTENCE_COMMIT=0`（两者须一起关，否则句级 FINAL 会
  叠进停嘴 FINAL 重复转写）；**kill-switch 档 endpointing min_delay 自动回 ≥0.35**
  （`_endpointing_delays_from_env` 强制，无需手动——未校准 EOT 配 0.25 早提交截断粤语，
  p6 实证）；B 线 interp env 已强制 sentence-commit=0。真实音频滑窗句间只出逗号，
  VAD 停嘴微停顿（≥0.45s）为第二句边界源（`QWEN3_ASR_SENTENCE_PAUSE_TRIGGER` 默认 1）。）
- `policy`：`offline_first`/`cloud_first`；建通话（`POST /api/calls`）时写入 manifest。

### LLM prompt 结构与多客服并发容量

- **每轮 system 顺序**：稳定指令前缀（用户语言规则 / 回复节奏 / 应答准则 / 话术总览 / 当前步）
  + 人设 base（`_instructions`+facts） + 易变参考尾部（知识库 / 联网 / 对话记忆）。
  目的：让 token0 起的公共前缀逐轮字节不变，命中 mlx_lm 的 prompt KV-cache——
  实测同 system + 不同 user 轮次 `prompt cached=1413/1434`，第二轮 2571ms→502ms。
- **绑话术的对象默认不做 RAG**（`has_steps` 门控，`CONTEXT_RAG=1` 强制开）：单对象只上话术，
  减少每轮 prefill；知识单条截断 350 字。无模板的开放咨询才检索知识库+联网。
- **多客服并发容量**（单机 M4 Pro 48GB，Qwen3.5-4B-MLX-4bit）：mlx_lm 的 prefill 是
  ~0.6k token/s 的架构硬墙（Qwen3.5 混合线性注意力，批多宽同速）；KV-cache 命中则绕过它。
  前置做足后同机约 **4-8 路交互客服**（warm 前缀每路≈decode+小尾 prefill）；冷启动同撞
  大 prompt 时受 prefill 墙限（2-4 路亚秒）。bok.py 已给 mlx server 加 `--prompt-cache-size 128`
  （默认 10 会被 4-6 路并发打穿）。超过此容量 → 第二台 Mac 起同栈 / GPU(CUDA) 服务器 vLLM
  （Mac 本机 vLLM 跑不了，且那是另一套架构）。

### 数据快照与清理

- `call_sessions.template_id`：建通话时把对象绑定的模板 id 快照到通话记录（审计"这场用了哪版话术"）。
- 删除话术模板会同步清空引用该模板的对象卡（`object_profiles.template_id`）。
- 删除知识文档会同时移除 vault 源文件，重启重建索引后不再"复活"。

## 6. 故障排查

1. `python tools/bok.py status` — 七项端口 UP/DOWN
2. `python tools/bok.py doctor --packaged` — 结构/依赖/硬件体检
3. app-data/logs/*.log — 各服务日志；app-data/audit/*.jsonl — 审计
4. bundle 只读：任何试图写 bundle 的路径都要改到 app-data
