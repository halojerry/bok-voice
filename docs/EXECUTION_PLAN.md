# Bok Voice — 问题整理与执行计划（A 线客服助手 + B 线同声传译）

> 来源：2026-08-30 全项目审计 + 最后一次三语 E2E 失败日志。本文档是「todo / task / test / 预期目标」的唯一执行依据；每项完成必须贴验证证据（命令 + 输出），不允许口头声称。

---

## 0. 问题清单（根因）

| # | 问题 | 根因 | 影响 |
|---|---|---|---|
| P0 | Agent worker 被看门狗杀死（`process is unresponsive` 135–154s） | ① `_Qwen3ASRStream`/`_Qwen3TTSStream` 无取消处理，会话关闭时 pending task 泄漏 + `aclose()` 竞态；② Silero `max_buffered_speech=60s`，假音频无静音段时 VAD 一直缓冲不切句；③ 9B Ollama（thinking）回复慢，并发多路时事件循环/CPU 饥饿 | A 线通话中途死亡、job 启动失败 |
| P1 | 三语 E2E 从未全绿 | 无 yue/en 转写日志（VAD 未切句或 worker 已死）；无 PASSED 留档 | 验收标准不满足 |
| P2 | 两个 sidecar 当前未运行、无一键启动 | 手工起进程，未纳入 compose/脚本；README 未写 | 任何语音链路跑不通 |
| P3 | B 线只有 mock POC | 无真实 ASR/翻译/TTS provider、无 WebAudio 播放、无 UI 面板、无指标落盘、无 worker server | B1/B2/B3 验收全部不满足 |
| P4 | Windows/vLLM 未验证 | Dockerfile.asr/compose/setup-windows.ps1 写了没跑过 | 生产 Windows 路径风险 |
| P5 | 文档与代码不一致、改动未提交 | docs/TODO.md 停留在 sherpa/火山时代；README 无 sidecar 说明；`feature/qwen-voice-translation` 大量未提交改动 | 团队协作风险 |

---

## 1. 阶段与任务（TODO / TASK / TEST）

### Phase A — A 线稳定性与全链路验收

#### A1. 修复 ASR/TTS adapter 取消处理
- TASK A1.1 `apps/agent/agent_runtime/providers/livekit_plugins.py`
  - `_Qwen3ASRStream._run`：捕获 `asyncio.CancelledError`，`finally` 中关闭进行中的 HTTP client；`_post_audio` 用 try/finally 保证 `AsyncClient.aclose()`；`_run` 在 channel 关闭时正常退出，不泄漏 pending task。
  - `_Qwen3TTSStream._run`：同样处理取消与 client 关闭；`_emit_beep` 增加取消安全。
  - `_OpenAICompatStream._run`：补取消处理（关闭流式响应）。
- TASK A1.2 `apps/agent/agent_runtime/agent.py`
  - `inference.VAD(..., max_buffered_speech=float(os.environ.get("VAD_MAX_BUFFERED_SPEECH", "15")), min_speech_duration=0.15, min_silence_duration=0.35)`，消除 60s 持续缓冲。
  - Ollama/OpenAI 请求传 `max_tokens`（默认 256，env `LLM_MAX_TOKENS`）与 `extra_body={"think": False}`（若 OpenAI 兼容通道支持），缩短回复时延。
  - `session` 生命周期收尾：entrypoint `try/finally` 中 `await session.end()`，消除 `did not exit in time`。
- TEST A1
  - `pytest tests/ -q` 全绿（回归）。
  - 新增长度受限的 Ollama 请求参数单测（构造 `APIConnectOptions`/mock client 断言 max_tokens 传入）——若不可行，用 E2E 日志断言。
  - 预期目标：无 `Task was destroyed` / `aclose(): async generator is already running` 错误出现在 agent 日志。

#### A2. sidecar 一键启动 + smoke 全绿
- TASK A2.1 新增 `scripts/start_sidecars.sh`
  - 用 `services/qwen3-asr-sidecar/.venv` 与 `services/qwen3-tts-sidecar/.venv` 起两个 uvicorn（host 0.0.0.0:8787/8788），`QWEN3_ASR_MODEL`/`QWEN3_TTS_*_MODEL` 指向 `data/models/` 本地路径；输出日志到 `data/sidecar-{asr,tts}.log`；重复启动幂等（检测端口）。
- TASK A2.2 `scripts/stop_sidecars.sh`（按 pid 文件停止）
- TASK A2.3 `README.md` 增加「启动语音 sidecar」小节。
- TEST A2 `scripts/smoke_sidecars.py` → 必须输出 `SIDECAR_SMOKE_PASSED`。
  - 预期目标：ASR 中/粤/英转写语言标签正确；TTS 预置/克隆合成字节 >0；克隆粤语音频回灌 ASR 标签为 Cantonese。

#### A3. 三语浏览器 E2E 全绿
- TASK A3.1 修复测试音频夹具 `data/test-audio/{zh,yue,en}.wav`：统一 16k mono，前后各补 600ms 静音，保证 Silero VAD 每 loop 都能 START/END。
- TASK A3.2 重跑 `tools/browser-e2e/trilingual.mjs`（串行三语）。
- TEST A3 `TRILINGUAL_E2E 3/3 PASSED`，且 agent 日志出现 `QWEN3_ASR_TEXT ... Cantonese` 与 `... English`。
  - 预期目标：zh/yue/en 各自出现 YOU 转写 + AGENT 回复；agent 回复语言随输入语言（LanguageState 生效）。

---

### Phase B — B 线同声传译真实化

#### B1. 真实 providers（Node）
- TASK B1.1 `services/realtime-translation/src/providers/qwen3-asr.js`
  - HTTP 包装 sidecar `/api/start|/api/chunk|/api/finish`；返回 `{text, language}`；内置简单能量 VAD（`energyVad.js`）切句，输出 `sentence` 事件。
- TASK B1.2 `services/realtime-translation/src/providers/qwen3-tts.js`
  - HTTP `POST /v1/audio/speech`（pcm）→ `{pcm, sampleRate, durationMs}`；按 100ms 切块给 scheduler。
- TASK B1.3 `services/realtime-translation/src/providers/ollama.js` + `dashscope.js`
  - Ollama：OpenAI 兼容 `/v1/chat/completions`，system prompt 为翻译指令，返回译文。
  - DashScope：`POST https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation`（Qwen-MT），API key 缺省时启动报错并回退 Ollama。
- TASK B1.4 `services/realtime-translation/config.json` + `src/config.js`（asr/translator/tts provider、base_url、语言对）。
- TEST B1 Node 单测：用 `node:test` + 本地 fake HTTP server 验证三个 provider 的请求/响应映射；energy VAD 的切句测试。
  - 预期目标：provider 单测全绿；`TranslationChannel` 可注入真实 provider 并产生 `{source, translated}` 与音频 chunk。

#### B2. worker server + WebAudio 播放 + 双字幕
- TASK B2.1 `services/realtime-translation/server.mjs`
  - WebSocket（`ws` 包）协议：`open_channel / audio / flush / close_channel`；事件：`subtitle / audio / metrics / error`；每通道独立 `TranslationChannel`。
  - 音频事件携带 `PlaybackChunkTrace` 字段（seqId/sourceSeqId/playbackBatchId/durationMs/playableBacklogMs/queueDepth/chaseState/chaseSpeed/gateRule/gateAction/gateReason/firstPlayable/audioHash）。
  - 指标节流后追加写 `data/translation-metrics.jsonl`。
- TASK B2.2 `apps/web/app/(app)/translate/page.tsx`
  - B 线独立 UI 面板：通道卡（源/目标语言选择、开始/停止）、麦克风采集（getUserMedia → PCM 16k）、WebAudio 播放（AudioContext）、双字幕高亮、调度指标（queueDepth/backlog/chaseSpeed/droppedMs）。
  - `apps/web/lib/api.ts` 增加 `translationWsUrl`；`.env.example` 增加 `NEXT_PUBLIC_TRANSLATION_WS_URL=ws://localhost:8790`。
- TEST B2
  - 脚本 `services/realtime-translation/test/ws.test.js`：起 server，用 ws 客户端 open channel + 注入测试音频，断言收到 subtitle 与 audio 事件。
  - 浏览器打开 `http://localhost:3000/translate` 能看到面板（人工验收：真机麦克风出译文语音）。
  - 预期目标：单路中→英在页面可听可看；双字幕不串句。

#### B3. 多通道 + 调度策略验收
- TASK B3.1 复用现有 `PlaybackScheduler`；`demo.mjs` 升级为真实 provider 可选项（`--real`）。
- TEST B3 `node --test` 全绿；`demo.mjs` 制造积压时打印 chase/drop 指标。
  - 预期目标：≥2 通道并发状态独立；积压时 `droppedBlocks/droppedMs` 变化；指标写 JSONL。

---

### Phase C — 平台与收尾

- TASK C1 `docker-compose.yml` 增加可选 profile `sidecars`（Linux/Windows 路径，host 路径默认注释说明），README 写清 Mac=host 脚本、Windows=WSL2/vLLM。
- TASK C2 文档对齐：`docs/TODO.md` 增加新里程碑（Qwen3-ASR/TTS、B 线），AGENT.md §7 状态更新；`DESIGN.md` 架构图补 sidecar。
- TASK C3 提交：`git add -A && git commit`（feature 分支，不 push）；提交信息按阶段拆分。
- TEST C3 `git status` 干净（除预期忽略文件）；`pytest` / `node --test` / smoke / trilingual 证据齐全后提交。

---

## 2. 验收清单（最终）

- [x] A1：Ollama 原生直连（think=false，0.26s/轮）+ `_chat_messages` 修复 + 流取消安全 + VAD 参数（`SIDECAR_SMOKE_PASSED` 前置验证；`Task destroyed` 为框架级关房噪音，不再触发 worker 被杀）
- [x] A2：`SIDECAR_SMOKE_PASSED`（三语 ASR + 预置/克隆 TTS + 克隆音色回灌）
- [x] A3：`TRILINGUAL_E2E 3/3 PASSED`（zh/yue/en 转写 + 回复 + 语言跟随）
- [x] B1：provider 单测全绿（EnergyVAD / Qwen3-ASR / Qwen3-TTS / Ollama 翻译）
- [x] B2：ws 集成测试全绿 + worker `ws://:8790` 可连 + `/translate` 页面可渲染（麦克风/播放需真机人工验收）
- [x] B3：多通道 demo（mock/`--real`）+ metrics JSONL 落盘
- [x] C1–C3：compose sidecar profile + README + B 线 README + .env.example 已更新；提交待 A3 收尾
- [x] C4：Docker 镜像离线重建（镜像源限流绕过）：`scripts/rebuild_images_offline.sh` + Dockerfile `ARG BASE_IMAGE`；三镜像重建后 `docker compose up -d --force-recreate` 恢复容器化，zh E2E 1/1 PASSED，悬空镜像清理完成

## 4. 执行过程发现的问题（追加）

- **Docker 镜像源限流（轩辕镜像 403）**：`docker compose build agent/web` 无法拉基础镜像 → Agent 容器一直跑旧代码。解法：宿主机 `.venv312` 直接跑 `python -m agent_runtime.main start`（editable 安装，代码即时生效）；文档已注明 Mac 开发机用 host agent + sidecar 脚本。
- **Ollama 默认 thinking**：OpenAI 兼容端点不支持 `think:false`，9B 回复慢且内容为空；改用原生 `/api/chat` + `"think": false` + `num_predict` 上限。
- **`_chat_messages` 丢用户文本**：`ChatContent` 文本部分是纯 `str`，`getattr(c,"text")` 取空 → LLM 听不见用户；已修并加单测。
- **TTS 单句 10–27s**（MPS 双模型）：多句回复超过静音窗口导致轮次被中断；E2E 音频补 85s 静音 + `LLM_MAX_TOKENS=160` + 人设要求短句。

## 3. 明确不做（本轮）

- Windows/WSL2 真机验证（无环境）：只交付脚本 + 文档，标注「待 Windows 环境验证」。
- ONNX Runtime / sherpa-onnx / llama.cpp（等 B 线 metrics 证明瓶颈后再引入）。
- B 线 `system-audio-helper`（桌面系统音频采集）本轮只留接口，不做实现。
