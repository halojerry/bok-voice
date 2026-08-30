# Bok Voice — 实时语音助手完整实施计划

## 目标与成功标准

在电脑上落地一个**实时语音助手**:可导入知识库、人设、对话框架;对话中持续成长;有情感、有语气、能共情。未来可扩展硬件。

**成功标准(可验证)**
- 对话流畅:用户说完话后首包语音 ≤ 1.5s;支持**打断(barge-in)**;连续多轮无卡顿。
- 稳定:全本地 ASR/VAD/TTS 断网可跑;LLM 云端↔本地一行配置切换。
- 成长:对话记忆后台沉淀进 Bok;重要记忆(decision/preference/identity/policy/sensitive/conflict)停下待确认,不污染知识库。
- 情感:能识别用户情绪并影响回复语气;助手输出带情绪标签,驱动前端表情 + 可选的 TTS 音色。
- 精准识别意愿:意图路由(直接回答 / 查记忆 / 工具调用)正确。

---

## 1. 最终架构(进程拓扑 + 数据流)

```
┌──────────────────────────────────────────────────────────────┐
│ 前端 voice-ui-kit (React, pnpm)                                │
│  ConsoleTemplate / 组件; transportType="smallwebrtc"           │
└──────────────┬───────────────────────────────────────────────┘
               │ WebRTC(SmallWebRTC) + RTVI(转写/情绪/状态事件)
┌──────────────▼───────────────────────────────────────────────┐
│ 编排 pipecat (Python, uv)  — 单个 PipelineWorker               │
│                                                                │
│  [transport.input] → SileroVAD → FunASR(SenseVoice)            │
│        → 上下文装配(情绪 + 记忆 + 人格) → LLM(流式)              │
│        → 情绪标签提取 → Qwen3-TTS sidecar → transport.output │
│                                                                │
│  进程内: Silero VAD · FunASR(SenseVoice) · Kokoro TTS · 情绪词库 │
│  网络:  DeepSeek/Qwen(HTTPS) 或 Ollama-abliterated(loopback)     │
└──────────────┬───────────────────────────────────────────────┘
               │ HTTP loopback (Bearer token)
┌──────────────▼───────────────────────────────────────────────┐
│ 记忆 Bok (独立 Python 进程, loopback:8771)                       │
│  vault 知识库 + Personal Core + 检索 + 可撤销/可遗忘              │
└──────────────────────────────────────────────────────────────┘
```

两个 Python 进程 + 一个前端。Bok 可先不启动,助手降级为"无记忆"仍可对话。

---

## 2. 协议设计(4 层,这是本计划的关键决策)

| 层 | 协议 | 理由(已对比) |
|---|---|---|
| 前端 ↔ 编排 | **WebRTC(SmallWebRTCTransport)+ RTVI** | 浏览器原生回声消除/Opus/低延迟;打断支持;无需 Daily 云。voice-ui-kit 原生 `smallwebrtc` transport + `webrtcUrl:'/api/offer'`。不用 raw WebSocket(无浏览器音频处理) |
| 编排 ↔ 记忆 | **HTTP loopback(Bok `/v1/*` + Bearer)** | Bok 本就是 loopback 服务;pipecat 是可信本机进程,直接读 `<vault>/.bok/auth-token`。MCP 留作未来给其他 Agent 复用 |
| 编排 ↔ 模型 | LLM=OpenAI-compatible(云端 HTTPS / Ollama loopback);ASR/TTS=进程内(无网络) | 国内模型全走 OpenAI 兼容;本地 abliterated 走 Ollama |
| 未来硬件 | **xiaozhi WebSocket 协议**(device-id 头 + 二进制 Opus + JSON 文本消息) | 用 pipecat `transports/websocket` + 自定义 serializer 接入 ESP32;M6 才做 |

---

## 3. 组件选型(经 pipecat 源码核实后的最终版)

| 环节 | 默认 | 可切换 | pipecat 原生? |
|---|---|---|---|
| VAD | Silero | — | ✅ `SileroVADAnalyzer` |
| ASR | SenseVoice(`iic/SenseVoiceSmall`) | 云端 paraformer | ✅ `FunASRSTTService` |
| LLM | DeepSeek | Qwen / Ollama-abliterated | ✅ `DeepSeekLLMService`/`QwenLLMService`/`OllamaLLMService` |
| TTS | **Qwen3-TTS(本地 sidecar)** | 豆包(云端) | ✅ 本地克隆/预置/指令控制 |
| Embedding | 不开(MVP) | bge-m3(本地) | — |
| 记忆 | Bok | — | 自写 HTTP client |
| 情绪(输入) | SenseVoice 标签 + 词库 | Hume(云,备选) | 部分(需自写提取) |
| 情绪(输出) | LLM `[emotion:x]` 标签 | — | 自写提取 |
| 前端 | voice-ui-kit | — | ✅ |

**三个关键修正(相对早期 DESIGN.md,已用源码核实)**
1. **pipecat 原生覆盖了几乎整条链路**,MVP 无需运行 xiaozhi。xiaozhi 的角色降为"**未来硬件协议参考**"(其 VAD/ASR/LLM/TTS provider 代码不再需要,pipecat 有更好的原生实现)。
2. pipecat 的 `fish` = **Fish Audio 云 TTS(需 key)**,不是开源 fish-speech。本地 TTS 统一改为 **Qwen3-TTS sidecar**;情绪/音色通过参考音频克隆与 instruct 控制。
3. pipecat 的 `FunASRSTTService` 调 `rich_transcription_postprocess` 会**剥离 SenseVoice 的 `<|HAPPY|>` 情绪标签**,所以"输入情绪"需在 postprocess 前截取标签(自写一个轻量包装)。

---

## 4. 各项目如何最优复用(明确边界)

- **pipecat**:编排框架 + 60+ 原生服务。我们**只写 4 个自定义件**:① Bok 记忆 processor,② 情绪提取(输入标签 + 词库 + 输出标签),③ 人格卡加载,④ Qwen3-TTS sidecar。
- **Bok**:记忆/成长/知识库,原样作为独立服务,**不改动其代码**。仅通过 `/v1/context`、`/v1/person/context`、`/v1/search`、`/v1/conversations/observe`、`/v1/memory/capture`、`/v1/import/markdown`、`/v1/web-clips` 接入。
- **voice-ui-kit**:前端 UI,用 `ConsoleTemplate` 起步,后续换自定义组件;接入 `smallwebrtc` transport。
- **expression-trainer**:复用①`data/emotion-lexicon.json`/`tiered-lexicon.json`(情绪词库,注意大连理工数据源授权,必要时换精简自建词表)②人格/Prompt 编辑器的交互范式。
- **xiaozhi-esp32-server**:复用①`textUtils.py` 的 emoji→emotion 映射思想(22 情绪)②未来 ESP32 的 WebSocket 协议规范。MVP 不运行它。

---

## 5. 分阶段实施(里程碑)

### M1 — 最小对话 spike(验证地基)
- pipecat `SmallWebRTCTransport` + Silero VAD + FunASR(SenseVoice) + DeepSeek LLM + Kokoro TTS。
- voice-ui-kit `ConsoleTemplate`(smallwebrtc)连 `/api/offer`。
- **验收**:电脑上流畅对话、能打断、首包 ≤1.5s、断网(仅 ASR/TTS/VAD)可跑。

### M2 — 记忆 + 人设(验证成长)
- 自写 `bok_memory.py`(HTTP client):每轮前 `context`+`person_context` 注入;每轮后异步 `observe`+`capture`。
- 人格卡 `persona.md`(存 vault)→ LLM `system_instruction`。
- **验收**:问"上次聊的 X"能检索到;对话后记忆沉淀;重要记忆在 Bok UI 待确认。

### M3 — 情绪闭环(验证情感/语气/共情)
- 输入情绪:SenseVoice 标签(自定义提取)+ 词库匹配 → 注入 LLM 上下文。
- 输出情绪:人格卡要求 LLM 输出 `[emotion:xxx]` → 提取 → 前端 avatar(自定义 RTVI 事件)。
- 情绪 TTS(可选):自写 Qwen3-TTS service,按情绪选参考音色。
- **验收**:情绪识别打标测试通过;输出标签稳定(失败回退 happy);共情回复自然。

### M4 — 实时语音翻译(第二产品线)
- 复用同一地基,插入 `TranslateStage`(ASR 与 TTS 之间)或独立 pipeline;语言对配置化;翻译时关闭人设/情绪/记忆注入。
- **验收**:中↔英 说完后 1–2s 出声。

### M5 — 打磨
- 延迟、打断、意图精准度、音色、多人格卡切换。

### M6 — 硬件(未来)
- pipecat `transports/websocket` + xiaozhi serializer 接入 ESP32;Bok 已是记忆 provider,无缝复用。

---

## 6. 关键实现细节(已在计划中定型)

- **Bok 鉴权**:pipecat 进程读 `<vault>/.bok/auth-token`,请求带 `Authorization: Bearer`;两进程同机同用户。
- **记忆写策略**:只走 `observe`(幂等收据)+ `capture`(后台提炼),**绝不 `write`**,防语音口水话污染 vault。
- **情绪标签约定**:人格卡 prompt 要求 LLM 首部输出 `[emotion:happy]`;提取用确定性正则,失败回退 `happy`,不额外调 LLM。
- **模型切换配置**(`config.json`,api_key 用 `env:` 引用不落盘):
  ```json
  {"llm":{"provider":"deepseek|ollama","base_url":"...","model":"...","api_key":"env:DS_KEY"},
   "tts":{"provider":"qwen3_tts|doubao","voice":"..."},
   "emotion":{"enabled":true,"lexicon":"local"},
   "persona":{"card":"02-Projects/Bok-Voice/persona.md"},
   "memory":{"bok_url":"http://127.0.0.1:8771","vault":"./vault"}}
  ```
- **人格卡 schema**:vault 内 Markdown(frontmatter `type: persona`),字段:人设/语气/情绪表达约定/对话框架/边界;支持多卡切换。

---

## 7. 目录规划

```
voice-assistant/
├── DESIGN.md              # 架构文档(实施时同步更新)
├── PLAN.md                # 本文档
├── config.example.json
├── server/                # pipecat 编排 (Python, uv + pyproject.toml)
│   ├── main.py            # WorkerRunner 入口
│   ├── bot.py             # run_bot: 构建 PipelineWorker
│   ├── providers/
│   │   ├── bok_memory.py  # Bok HTTP client + 上下文装配 processor
│   │   ├── emotion.py     # SenseVoice 标签提取 + 词库 + 输出标签提取
│   │   └── tts_qwen3.py  # Qwen3-TTS sidecar
│   └── persona.py         # 人格卡加载
├── web/                   # voice-ui-kit 前端 (React, pnpm)
└── bok/                   # Bok 独立 checkout(或 submodule,不改动)
```

---

## 8. 测试与验收

- **M1**:真机麦克风冒烟 + 延迟/打断人工验收。
- **M2**:检索回归(Bok 自带 `test_retrieval_regression.py`)+ 记忆沉淀人工验收。
- **M3**:情绪打标小样本测试;标签稳定性(连续 N 轮无缺失)。
- **整体**:pipecat 官方 `pipecat eval` 行为评测(可选,用 Ollama judge)。

---

## 9. 风险与假设

- **假设**:目标平台 macOS 优先(当前开发机),Windows 后置;Python 用 `uv`、前端用 `pnpm`。
- **SenseVoice 情绪标签被 pipecat 剥离** → 需自写提取(计划内已定)。
- **Qwen3-TTS 中文情感与三语言音色克隆更符合客服场景** → 默认 Qwen3-TTS sidecar 跑通。
- **abliterated 模型**可能轻微损能力 + 合规责任在用户侧 → 默认仍 DeepSeek,abliterated 为"本地+不拒绝"档。
- **expression-trainer 词库**基于大连理工本体库,商用前确认授权 → 可换自建精简词表。
- **SmallWebRTC 在 Tauri/浏览器环境**的麦克风权限与回声消除需 M1 验证。
- **豆包 TTS** 需先在火山引擎建模型 endpoint(接入时处理)。

---

## 10. 实施顺序(批准后执行)

1. 环境检查(Python/uv、node/pnpm、麦克风、Ollama 可选)。
2. 搭 `server/`(pipecat 依赖)+ `web/`(voice-ui-kit 脚手架)。
3. 写 M1 最小 pipeline + `/api/offer`,本地跑通对话与打断。
4. 接 Bok(M2)→ 情绪(M3)→ 翻译(M4)→ 打磨(M5)。
5. 同步更新 `DESIGN.md` 与 `config.example.json`。
