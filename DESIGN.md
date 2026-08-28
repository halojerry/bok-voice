# Bok Voice — 实时语音助手架构设计（v0）

> 目标：一个在电脑上可流畅对话的智能语音助手。可导入知识库、人设、对话框架；在对话中持续成长；有情感、有语气、能共情。未来可拓展到硬件。
>
> 本文档是构建的单一事实源。改动选型或边界请先更新这里。

---

## 1. 定位与硬约束

- **形态**：本地优先的桌面/浏览器语音助手（电脑对话为主，硬件是未来）。
- **核心体验**：稳定、轻、对话流畅（打断/低延迟）、精准识别用户意愿。
- **能力**：知识库导入、人设/对话框架可编辑、对话中成长、情感/语气/共情。
- **隐私**：记忆与知识库本地化，模型 API Key 不落盘、不进 vault、不进 git。

---

## 2. 总体架构

```
┌────────────────────────────────────────────────────────────┐
│ 前端 voice-ui-kit (React)                                    │
│  ConsoleTemplate / ConnectButton · VoiceVisualizer ·         │
│  ControlBar · TranscriptOverlay · 情绪表情面板                │
└───────────────┬────────────────────────────────────────────┘
                │ WebRTC（small-webrtc-transport，音频 + 打断）
┌───────────────▼────────────────────────────────────────────┐
│ 编排 pipecat（Python，本地 loopback）                         │
│                                                              │
│  mic ─▶ VAD(Silero) ─▶ ASR(SenseVoice)                       │
│           │ 语音结束                                          │
│           ▼                                                  │
│  ┌─────────── 上下文装配 ────────────┐                       │
│  │ ① 输入情绪（SenseVoice 标签+词库） │                       │
│  │ ② 记忆检索 Bok /v1/context         │                       │
│  │ ③ 个人理解 Bok /v1/person/context  │                       │
│  │ ④ 人设人格卡（vault Markdown）      │                       │
│  └──────────┬───────────────────────┘                       │
│             ▼                                                │
│  意图路由（intent_llm / function_call）                       │
│             ▼                                                │
│  LLM（流式，输出带 [emotion:xxx] 标签）                        │
│             ├─ 文本 → TTS（流式朗读）                         │
│             └─ 情绪标签 → 提取 → 驱动 avatar + TTS 音色       │
│                                                              │
│  （每轮结束，异步）Bok /v1/conversations/observe               │
│        + /v1/memory/capture → 后台成长                        │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│ 记忆层 Bok（独立进程，loopback HTTP）                          │
│  知识库 vault + Personal Core + 检索 + 可撤销/可遗忘            │
└────────────────────────────────────────────────────────────┘
```

---

## 3. 组件选型（已定稿）

| 环节 | 默认 | 可切换 | 说明 |
|---|---|---|---|
| 编排 | pipecat (Python) | — | A 方案，负责打断/流式/低延迟 |
| VAD | Silero（本地） | — | 复用 xiaozhi `vad/silero.py` 思路 |
| ASR | SenseVoice（本地，Apache-2.0） | 云端 paraformer | 中文最优，**自带情绪标签** |
| LLM | DeepSeek（云端） | 本地 abliterated / Qwen | OpenAI-compatible 一行切换 |
| TTS | CosyVoice / fish-speech（本地） | 豆包（极致音色） | 主要负责朗读，支持情绪音色 |
| Embedding | bge-m3（本地 Ollama） | 不开 | Bok 语义检索增强，MVP 可关 |
| 情绪（输入） | SenseVoice 标签 + expression-trainer 词库 | — | 双保险 |
| 情绪（输出） | LLM `[emotion:xxx]` / emoji 标签 | — | 复用 xiaozhi `get_emotion` 机制 |
| 记忆/成长 | Bok（HTTP API） | MCP（未来） | 知识库 + Personal Core |
| 人设/框架 | 人格卡（vault Markdown） | — | 可编辑，随 vault 检索 |

---

## 4. 模型适配设计

### 4.1 唯一适配协议：OpenAI-compatible

所有 LLM / Embedding 都收敛到 `base_url + api_key + model`，pipecat 的 `OpenAILLMService` 支持 `base_url` 覆盖，国内/本地/云模型几乎零代码接入。

| 类型 | base_url 示例 | 说明 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | 默认，便宜、中文+推理强 |
| 通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-plus / qwen-max |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | glm-4-plus / glm-4-flash |
| 豆包 | `https://ark.cn-beijing.volces.com/api/v3` | 需 endpoint id，TTS 尤其强 |
| Kimi | `https://api.moonshot.cn/v1` | 长上下文 |
| SiliconFlow | `https://api.siliconflow.cn/v1` | 一个 key 聚合多家 |
| Ollama（本地） | `http://localhost:11434/v1` | Qwen / DeepSeek-R1 / abliterated |
| vLLM / Xinference | `http://127.0.0.1:8000/v1` | 高吞吐本地部署 |

### 4.2 本地 abliterated 模型

- 来源：HuggingFace 搜 `abliterated`（Qwen/Llama 系居多），取 GGUF 量化版。
- 接入：`ollama` 导入 GGUF → Modelfile → 作为一个 `model` 名，与 DeepSeek 一键切换。
- 注意：① 消融可能轻微波及能力，先试聊验证；② 合规/内容责任在用户侧；③ 建议默认仍 DeepSeek，abliterated 作为"本地+不拒绝"档。

### 4.3 配置 schema（`config.json`）

```json
{
  "llm": {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "env:DS_KEY"
  },
  "asr": { "provider": "sensevoice_local", "lang": "zh" },
  "tts": { "provider": "cosyvoice_local", "voice": "温暖女声", "emotion": true },
  "emotion": { "enabled": true, "lexicon": "local", "sensevoice": true },
  "persona": { "card": "02-Projects/Bok-Voice/persona.md" },
  "memory": { "bok_url": "http://127.0.0.1:8771" },
  "embedding": { "provider": "none" }
}
```

- `api_key` 一律用 `env:` 引用（同 Bok `CredentialStore` 思路），不落盘。
- "本地↔云端"切换 = 改一处配置，不改代码。

---

## 5. 情绪系统（双保险闭环）

### 5.1 输入情绪（理解用户）

- **SenseVoice**：ASR 结果自带情绪标签（happy/sad/angry/…）+ 音频事件。
- **expression-trainer 词库**：对 ASR 文本做情绪词/强度/极性匹配（146 情绪词）。
- 二者合并成 `user_emotion`，注入 LLM 上下文，用于"共情"。

### 5.2 输出情绪（表达语气）

复用 xiaozhi `get_emotion` 的机制：

1. 人格卡 prompt 要求 LLM 在回复首部/尾部输出 `[emotion:happy]` 或 emoji。
2. pipeline 用确定性规则提取（不额外调 LLM）。
3. 提取结果同时：
   - 驱动前端 avatar/表情；
   - 传给支持情绪的 TTS（CosyVoice instruct / fish-speech / GPT-SoVITS 参考音频）控制哭/笑等音色。

> 情绪标签是"约定"而非"魔法"：依赖人设 prompt 训练 LLM 稳定输出，需要一轮 prompt 调优。

---

## 6. 人设 / 对话框架（人格卡）

人格卡是一张存于 vault 的 Markdown（`02-Projects/Bok-Voice/persona.md`），随 Bok 检索可被引用、可版本化：

```markdown
---
type: persona
name: 小鹿
---
# 人设
你是小鹿，一个温暖、有点俏皮的陪伴型助手。

# 语气
口语化、自然、偶尔用「呀」「呢」，不书面。

# 情绪表达
每次回复首部输出 [emotion:xxx]，从 happy/sad/angry/... 中选择。

# 对话框架
- 开场：主动问候 + 简短
- 倾听：先共情，再回应
- 追问：没听懂时确认，不假装理解

# 边界
不提供医疗/法律/投资建议；被问到敏感话题时温柔转移。
```

- 人格卡在"上下文装配"阶段被读入 system prompt。
- 支持多张人格卡切换（陪伴/教练/翻译/学习）。

---

## 7. Bok 集成（记忆/成长）

pipecat 进程 ↔ Bok 进程走 loopback HTTP（MVP），未来可切 MCP。

| 时机 | 调用 | 作用 |
|---|---|---|
| 每轮对话前 | `POST /v1/context` + `POST /v1/person/context` | 检索知识库 + 个人理解，注入上下文 |
| 需要精准召回 | `POST /v1/search` | 用户明确提问时检索 |
| 每轮对话后（异步） | `POST /v1/conversations/observe` | 留幂等收据（只存行为，不存口水话正文） |
| 有价值的对话 | `POST /v1/memory/capture` | 后台 LLM 提炼成记忆卡，重要项待确认 |
| 导入资料 | `POST /v1/import/markdown` / `/v1/web-clips` | 知识库导入 |

- 记忆写入只走 `observe` + `capture`，不直接 `write`，避免语音口水话污染 vault。
- Bok 的重要记忆（decision/preference/identity/policy/sensitive/conflict）会停下等确认，符合"可撤销、可遗忘"。

---

## 8. 同声传译（v2，档位 1：实时语音翻译）

- 复用同一条 pipecat 地基，新增 `TranslateStage`（ASR 与 TTS 之间）或独立 pipeline。
- 语言对（中↔英/日）做成配置；翻译时**关闭人设/情绪/记忆注入**，保忠实度。
- 延迟账：ASR 终句 ~300–600ms + 翻译 ~300–800ms + TTS 首包 ~300ms ≈ 说完后 1–2s 出声。
- 真·同传（边说边译 + 译文修订）为 v3 研究项，不在 MVP。

---

## 9. 目录规划

```
voice-assistant/
├── DESIGN.md                 # 本文档
├── config.example.json       # 配置样例
├── server/                   # pipecat 编排
│   ├── app.py                # 入口（陪伴 pipeline）
│   ├── pipeline.py           # 五段编排 + 上下文装配
│   ├── providers/
│   │   ├── llm.py            # OpenAI-compatible 适配
│   │   ├── asr.py            # SenseVoice STT service
│   │   ├── tts.py            # CosyVoice / fish-speech / 豆包
│   │   ├── emotion.py        # 输入情绪（词库）+ 输出情绪（标签提取）
│   │   └── memory.py         # Bok HTTP client
│   └── persona.py            # 人格卡加载
├── web/                      # voice-ui-kit 前端（React）
└── bok/                      # Bok 子模块/引用（不复制）
```

---

## 10. 里程碑

1. **M1 — 最小对话 spike**：pipecat `ASR→LLM→TTS` 回环 + voice-ui-kit 前端，验证"电脑上流畅对话 + 打断"。（不接 Bok/情绪）
2. **M2 — 记忆 + 人设**：接 Bok（context/observe/capture）+ 人格卡注入，验证"成长 + 人设"。
3. **M3 — 情绪闭环**：SenseVoice 输入情绪 + LLM 输出情绪标签 → TTS 音色 + avatar。
4. **M4 — 翻译 v2**：TranslateStage 实时语音翻译。
5. **M5 — 打磨**：音色、打断体验、延迟、意图精准度。

---

## 11. 风险与开放问题

- [ ] **情绪标签稳定性**：LLM 输出 `[emotion:xxx]` 需 prompt 调优 + 容错（提取失败回退 happy）。
- [ ] **打断（barge-in）体验**：pipecat 的打断与 SenseVoice 的 endpoint 参数需联调。
- [ ] **abliterated 模型质量**：先试聊 + 跑分验证，默认仍用 DeepSeek。
- [ ] **Bok loopback 边界**：pipecat 与 Bok 同机 loopback，无冲突；未来硬件需重新评估可达性。
- [ ] **情感词库数据源授权**：expression-trainer 词库标注基于大连理工本体库，商用前需确认授权，可先换精简自建词表。
- [ ] **豆包 TTS endpoint id**：接入需先在火山引擎建模型 endpoint。
