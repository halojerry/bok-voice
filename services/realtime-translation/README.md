# Bok Realtime Translation Worker（B 线）

同声传译引擎：每通道独立 `TranslationChannel`（ASR 状态 / 句边界 / 翻译队列 /
TTS 流 / 播放队列），调度模型对齐“金喜同传”的 `PlaybackChunkTrace` 字段
（`queueDepth` / `playableBacklogMs` / `chaseState/chaseSpeed` /
`gateAction/gateReason` / `droppedBlocks/droppedMs` / `sourceSeqId`）。

## 运行

前置：Qwen3-ASR `:8787`、Qwen3-TTS `:8788` 与本地 LLM `:1235`（OpenAI 兼容）
已启动（统一入口：仓库根目录 `python tools/bok.py serve`）。

```bash
npm install          # 需要 ws（离线缓存亦可）
npm test             # 单元 + WebSocket 集成测试
npm run start        # 启动 worker，ws://localhost:8790
node demo.mjs        # 多通道 mock 演示（backlog/chase/drop）
node demo.mjs --real # 真实 Qwen3-ASR + 本地 LLM + Qwen3-TTS 演示
```

Web 面板：`http://localhost:3000/translate`（顶栏「同传」），
需设置 `NEXT_PUBLIC_TRANSLATION_WS_URL=ws://localhost:8790`。

## 协议

客户端 → 服务端：
`open_channel` / `audio`(pcm base64, sampleRate) / `flush` / `tick`(advanceMs) /
`discard`(uptoSourceSeqId) / `clear` / `close_channel`

服务端 → 客户端：
`channel_open` / `subtitle` / `audio`(含 PlaybackChunkTrace) / `metrics` / `error`

指标（节流后）追加写入 `../../data/translation-metrics.jsonl`。

## 配置

`config.json`：asr / translator(local_openai|dashscope) / tts / server。
DashScope 翻译需 `DASHSCOPE_API_KEY`，缺失时 worker 自动回退本地 LLM。
