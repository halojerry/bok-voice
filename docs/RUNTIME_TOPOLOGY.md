# Bok Voice 运行时关系拓扑（RUNTIME TOPOLOGY）

> 本文件是"安装后的 App 能不能正常使用"的唯一验收基准。任何改动必须保证
> 按这张图跑起来：组件齐全、端口可达、数据落在 app-data、bundle 只读。

## 1. 组件与端口

| 组件 | 端口/协议 | 职责 | 运行时 | 数据落点 |
|---|---|---|---|---|
| Tauri Shell (Rust) | — | 打开窗口、拉起 `bok.py serve`、托管静态前端 | 打包内 | — |
| `bok.py serve` | — | 编排：启动顺序、健康、模型下载 | 打包 Python | pid 文件 → app-data/run；日志 → app-data/logs |
| control-plane | :8000 HTTP | 业务 API、知识、设置、审计、LiveKit token | 打包 Python | SQLite → app-data/bok_voice.db |
| ASR sidecar | :8787 HTTP | 三语转写（zh/cantonese/en） | Mac=mlx_audio；Win=qwen-asr+CUDA | 模型 → app-data/models |
| TTS sidecar | :8788 HTTP | 合成 / 克隆 / 试听 | Mac=mlx_audio；Win=qwen-tts | 模型 → app-data/models |
| LLM | :1235 OpenAI 兼容 | A 线对话 + B 线翻译 | Mac=mlx_lm；Win=llama-server CUDA | 模型 → app-data/models |
| B-line worker | :8790 WS | 同传通道：ASR→翻译→TTS 队列 / 背压 | 内嵌 Node | 指标 → app-data/translation-metrics.jsonl |
| LiveKit server | :7880 WS/WebRTC | RTC 信令与媒体（7881/7882 RTC 端口） | 内嵌二进制 | keys → 内嵌 livekit.yaml |
| agent worker | 进程 | A 线智能体（VAD/对话/情绪/打断） | 打包 Python | 调 8787/8788/1235/8000 |

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

### B 线（同声传译）

```text
页面 (WebSocket)
  → B-line :8790 open_channel(sourceLang, targetLang)
  → 每通道：ASR :8787 → 翻译（本地 LLM :1235 或 DashScope）→ TTS :8788
  → 字幕 + 音频块（PlaybackChunkTrace）回流页面
指标：队列深度/背压/丢弃 → app-data/translation-metrics.jsonl
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

- `asr.provider`：`qwen3_asr`（本地 sidecar）/ `sherpa_sensevoice` / `fake`（仅测试）。语言值统一 `zh/cantonese/en`（粤语叫 `cantonese`，不再用 `yue`）；agent 在会话语言为粤语时给 sidecar 传 `language=cantonese` 强制模型按粤语转写，避免 auto 误判成普通话。
- `llm.provider`：`local_openai`/`mlx`（本地）/ `deepseek`（云端，缺 `api_key` 显式告警并回退本地）/ `fake`。
- `tts.provider`：`qwen3_tts` / `volcano_streaming`（需 `VOLC_*` 环境变量）/ `fake`（静音测试音，非火山 beep）。
  音色兜底按语言 `speaker_zh/speaker_cantonese/en`（旧键 `speaker_yue` 已迁移为 `speaker_cantonese`）；persona 绑定 `reference_audio` 优先。
- `vad`：`provider` + `max_buffered_speech` / `min_speech_duration` / `min_silence_duration` / `interruption`
  —— 直接构造 `inference.VAD` 与打断开关（环境变量 `VAD_*` 仅作部署覆盖）。
  基线默认：`min_silence_duration=0.45`、`min_speech_duration=0.15`；endpointing `min_delay=0.35`/`max_delay=1.2`。
  （勿为追低延迟把 min_silence 收到 <0.3 / min_delay 收到 <0.3：离线式 ASR 从停嘴到转写回来需 ~0.5-1.2s，
  端点判定太紧会让轮次在转写返回前提交 → 回复被丢、agent 不回话。）
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
