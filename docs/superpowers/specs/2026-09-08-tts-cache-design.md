# TTS 音频缓存 + 垫话 设计（PR-1 / PR-2）

日期：2026-09-08。状态：已批准（与 `2026-09-08-qa-fastpath-design.md` 配套，快路为 PR-3）。

## 背景与目标

云 TTS（MiniMax）首包 ~300-500ms+网络是剩余延迟大头之一，且架构拍板 TTS 必须走云。本地设备侧可做的：把**可预知文本**的音频预合成/缓存到本地，播放时绕过云端直出 PCM；对**必须 LLM 临场生成**的慢轮（生成 ~1.5-2s），垫一句自然话术把感知出声压到 ~0.7-0.9s。

- 开场白现状：文本预生成（不走 LLM），但音频仍通话开始时实时云合成。
- 目标 1（PR-1）：话术直念线命中缓存 ~0ms 出声；`tts-pregen` 离线批量预合成后首通即秒。
- 目标 2（PR-2）：LLM 慢轮垫话，感知首声 ~0.7-0.9s；快轮零打扰。

## 调研结论（设计依据）

- `session.say(text, audio=AsyncIterable[rtc.AudioFrame])` 官方支持（livekit-agents 1.8.0 `agent_session.py:1430`），完全绕过 TTS，文本照常进上下文；`add_to_chat_ctx=True` 默认保持转写/上下文一致。
- SynthesizeStream 协议：基类每个流实例 `_num_segments` 只记 1（`tts.py:757-765`），我们的 bidi 流整轮只开一个 segment（`livekit_plugins.py` init_done 旗标）——播本地 PCM 走同一协议无兼容问题。
- tts_node 对 `capabilities.streaming=True` 的 TTS 逐 LLM delta 推 `push_text`，无上游切句 → **流式路径首句拦截是负收益**（输入侧缓冲等首句会把首包推迟 ~1.5s），明确不做任何流式拦截/存储。
- `SpeechHandle.interrupt(force=False)`（1.8.0 公开 API，`speech_handle.py:185`）支持定向取消垫话。
- `_trim_lead_silence`（livekit_plugins.py:1710）为模块级纯函数，离线预生成可直接复用。

## PR-1：缓存底座

### TtsAudioCache（新文件 `apps/agent/agent_runtime/tts_cache.py`）

- Key：`sha1(归一化文本 + voice_id + model 档 + sample_rate)`。归一化=全半角标点统一+去首尾空白；**不做数字归一**（号码读法逐字对应，归一会错配）。
- 存储：`<app-data>/tts-cache/<sha1>.pcm`（24kHz 单声道 s16le，与管线一致零转码）+ `<sha1>.json` 元数据（文本/音色/时长，排查用）。遵守「打包资源只读、运行时写 app-data」铁律。
- 上限：LRU 500 条（≈100-200MB），按 mtime 淘汰；写入失败（磁盘满等）静默降级，绝不影响播放；读取损坏 → 返回未命中走云端。
- 开关：`BOK_TTS_CACHE=0` 一键全关（纯透传）。

### CachedTTS（包装 MiniMaxTTS，模板=官方 StreamAdapter 组合姿势）

- 仅拦 `synthesize()`：命中 → 缓存 PCM 组 ChunkedStream 直接回；未命中 → 走内芯、成功消费后落盘（写前做 `_trim_lead_silence`，与运行时首包修剪语义一致）。
- `stream()` / `prewarm()` / `aclose()` / metrics 事件全透传；透传层暴露「真回复首音频」回调注册（PR-2 垫话取消用）。
- MiniMaxTTS 增加公开只读方法 `resolved_voice()` / `resolved_model()`（包装层取缓存 key 用，不碰私有成员）。
- 装配：agent.py 构造 MiniMaxTTS 后按开关套 CachedTTS。

### _say_script 直念接入（agent.py）

帮手函数接入 4 个直念点（开场白 / 心跳 nudge / 收线 farewell / WhatsApp 捕获确认）：

- 命中 → `session.say(text, audio=缓存帧)` ~0ms。
- 未命中 → `synthesize()` 整句合成（连接已预热，短句首包与流式无差）→ 落盘 → `audio=` 播出 → 同文本第二通起秒出。这就是带 `{name}`/单号变量话术的自动沉淀。
- 任何缓存 I/O 异常 → 退回普通 `session.say(text)` 流式路径，行为与现状完全一致。
- `add_to_chat_ctx` 保持默认，KV 严格前缀不变。

### tts-pregen 工具（tools/bok.py 子命令）

- `--greetings`：无变量脚本线全量预合成（兜底问候 3 句、WA 未捕获分支、心跳无名骨架——骨架常量从 agent.py 提取为模块级共享，避免双份维护）。
- `--objects`：经 CP API 取对象清单，逐对象渲染开场白/收线（含 `{courier}` 等变量）批量预合成 → 首通即秒。
- 音色取当前生效 voice-map；音色/模型档变更后重跑即全量重建（key 失效自然淘汰）。
- 独立进程跑：`repo_python()` + 仓库 PYTHONPATH + `SSL_CERT_FILE=_certifi_bundle(...)`（照抄 bok.py 既有固化逻辑，否则 MiniMax WSS 必炸）。

### 打点与验收

- `TTS_CACHE hit=1/0 key=…` 逐直念打点；命中行走 `session.say(audio=)`，`TTS_FIRST_AUDIO_MS` 语义由读盘组帧时间取代，实测 ≈0-5ms。
- 测试：key/归一化/LRU/损坏降级（`test_tts_cache.py`）；包装层命中/未命中/透传/首音频回调（`test_cached_tts.py`，FakeLiveKitTTS 先例）；直念点单测。全量 pytest + 三语 E2E 回归。

## PR-2：垫话（只依赖 PR-1）

**原则：只垫「必须 LLM 临场生成」的轮次**（话术缓存未命中、走正常 LLM 路径）；快轮永远不垫，生硬感最低。

- 触发：hook 正常返回（无 StopResponse、非脚本直念轮、非 closing/REFUSE 态、非 WA 步）后起定时器 `BOK_FILLER_DELAY_MS`（默认 700ms）；真回复首音频先到 → 作废不垫；到点仍无首音频 → 播垫话。
- 音频：`tts-pregen --fillers` 预生成短语库，每语言 2-3 条轮换（默认语料进代码常量，可用 env 覆盖，最终话术用户定稿）；走 TtsAudioCache 本地直出，**缓存未命中直接跳过**（垫话绝不允许打云端）。
- 播放：`session.say(filler_text, audio=帧, add_to_chat_ctx=False)`——不进 LLM 上下文：KV 前缀不变、4B 不被垫话锚定、`last_reply` 不被污染。
- 取消：CachedTTS 首音频回调 → 对垫话 SpeechHandle `.interrupt()`（定向，不碰回复流）；用户插话走官方打断机制自然掐掉。
- 护栏：每通限次 `BOK_FILLER_MAX`（默认 2）；三骨架轮换；`BOK_FILLER=0` 一键全关；垫话=speaking，心跳答案在途窗语义不变。
- 打点：`BOK_FILLER fired/canceled` + 感知首声时间（=min(垫话首声, 回复首声)）。
- 测试：假 session+假时钟单测（门控矩阵/取消竞态/限次/轮换）、barge-in E2E 回归、人工听感验收。

## 风险与对策

- say(audio) 被打断且无同步转写时 assistant 不入 ctx：框架行为，与 LLM 路径一致，可接受。
- 垫话取消竞态：回调时序单测覆盖；兜底=垫话自然播完（短语 ≤1.2s，最坏多等 ~0.5s）。
- 打包 app 需重打包才含新代码（既有版本断层事实）；验证走 dev 栈。

## 非目标

流式路径任何拦截/存储（调研证实负收益）；垫话接入 CP/web 配置面（先 env+代码常量）；Q→A 快路（另见 qa-fastpath 设计）。
