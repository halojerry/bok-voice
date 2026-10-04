# 方案：A线体验修复 + B线LiveKit双端同传 + 云端熔断（v7 终版，2026-09-04）

> 推理边界（拍板）：ASR=本地 Qwen3-ASR、LLM=本地 mlx，无云端推理；唯一云依赖=TTS(MiniMax)。
> Wave1/2 零云端依赖；Wave3 Supabase 缓行（无 VPS）；Wave4 分发版加固。

## Wave 1 —— A 线体验
1. 延迟（本地战场）：dev webui 复测基线 → 压尾部（对话记忆 8→3 行内、知识 snippet 上限、`LLM_HISTORY_TURNS` 校准）；背景 judge 与主回复共用 :1235 加让路节流；前缀可变段（用户语言/当前步）不动保 KV-cache；本地 ASR interim（sidecar mlx 滑动窗增量解码 → agent 开 interim_results → 开 PREEMPTIVE_GENERATION）。目标停嘴→首声 ≤1.5s。
2. 明确拒绝→直接收尾：`flow.py` 加 REFUSE verdict + 词典补全（唔需要/唔办/我唔要/唔要啦/拒绝/唔好再打嚟…，负向排除「唔使担心/唔使唔该」类）；拒绝轮注入收尾指令→一句礼貌再见→新端点 `POST /api/supervisor/{call_id}/end`（settle+断房，disposition=declined）；规则先行不交 LLM judge。

## Wave 2 —— B 线同传 v2：LiveKit 房间 × AgentSession 对话模型 ×2 方向
- 房间三/四参与者：我方端(web) + 对方端(web) + interp-zh-en agent + interp-en-zh agent（各为 AgentSession 全托管，与 A 线同一套会话机制：VAD(silero 基线)→Qwen3-ASR(cantonese hint)→LLM=翻译(mlx :1235,「只输出译文」prompt)→TTS(:8788，后续可切 MiniMax stream)）。
- 「我方输出=对方听到」：每 agent 发布译文轨后 `set_track_subscription_permissions` 幂等白名单（我方译文轨只授权对方、反之亦然；开源 SFU 强制执行，livekit 1.1.15 `participant.py:521-541`）。
- 字幕：AgentSession 自带 `lk.transcription` 转写流，前端 `useTranscriptions` 渲染双方原文+译文，零自造协议。
- 落库：`cp.add_turn(role=me/other, transcript="原文：…\n译文：…")` + 房间关闭 `cp.settle` → 总结/知识蒸馏/vault 全复用。
- 改动：新 `apps/agent/agent_runtime/interpret.py`；CP token 加 role、CreateCallRequest 加 kind/object_id 可空/source_lang/target_lang、call_sessions.kind 幂等加列（deps.py）；web 新 `app/(app)/interpret/page.tsx`（复用 CallStudio 的 useSession/RoomAudioRenderer/switchActiveDevice/lib/audio.ts 设备选择）；bok.py 起 interpreter worker；StageHeader 加「同传」；/translate 冻结留 POC。

## Wave 3 —— Supabase（必要性：熔断+数据分析必须有自有云后端；无 VPS；缓行）
1. 许可/熔断：devices(机器码)/entitlements/device_commands(签名)/command_acks；三层心跳（启动链→每通电话前→通话中 CP 15-30s + `_supervisor_watch` 2s 感知远程收线）；`disable_service`/`terminate_call`/`wipe_local_data` 签名指令+幂等+ack；离线宽限 72h。
2. TTS key 保护（可选二阶段）：Supabase Edge Function 代理 MiniMax（`MINIMAX_BASE_URL` env 已支持）；首包劣化明显再议 VPS。
3. 数据上送：CP asyncio 水位批量上送 calls/turns/settlements/knowledge/usage/audit；`usage_records` 接写入方；设备 ID Rust 首启生成；Tauri updater。

## Wave 4 —— 分发版加固
分发 profile（锁 asr/llm 本地+tts=minimax）；设置页收敛（删 providers/base_url/api_key/persona 引擎下拉、CP 拒绝客户端覆写、留麦克风/扬声器+音色）；Nuitka 编译+无 sourcemap+模型目录改名+macOS 签名公证+EULA 禁逆向；`verify_bundle.sh`/`doctor` 适配。
诚实边界：防解包是高成本壁垒非绝对；本地模型文件可被技术用户识别（UI 层完全隐藏）。

## 已核实的代码事实锚点
- TTS 无跨 provider 回退：provider 每 job 启动一次性选定，MiniMax 失败=每轮 beep（`agent.py:548-622`、`livekit_plugins.py:1254-1263`）→ 熔断「停 TTS=哑火」成立。
- 心跳钩子现成：`_supervisor_watch` 2s 轮询（`agent.py:973-1005`）、Tauri setup 每启 spawn（`lib.rs:342-346`）、`/api/token` 每通电话必经（`main.py:418-446`）、B 线 worker 启动拉 CP 设置（`server.mjs:178-193`）。
- 延迟病灶：尾部「对话记忆」逐轮 append 留 8 行（`agent.py:780`、`livekit_plugins.py:553-554`）；背景 judge 与主回复共用 :1235（`agent.py:808-845`）；本地 ASR 无 interim（sidecar `app.py:146-177`）。
- 设置页暴露面：`settings-meta.ts` providers 数组、personas 页语音引擎下拉（第二出口）、`/api/tts/preview` provider 参数、base_url/api_key 输入框。
- MiniMax key 现明文存 settings DB（现行风险，Wave3 二阶段网关化）。
