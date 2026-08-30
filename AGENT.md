# Bok Voice × LiveKit —— Agent 知识备忘（官方复用 & 决策记录）

> 本文档是「如何在 Bok Voice 里最大化复用官方 LiveKit 能力、少造轮子」的长期备忘。
> 任何后续会话/Agent 都可以先读本文件，再决定动手。最后更新：2026-08-29。

---

## 1. 项目与官方能力对照（结论速览）

| 层面 | 官方现成件 | Bok Voice 现状 | 决策 |
|---|---|---|---|
| 实时服务器 | `livekit/livekit-server` Docker 官方镜像 | 已用（docker-compose `livekit` 服务） | 保持 |
| Agent 运行时 | `livekit.agents`（Python）：`AgentSession` / `Agent` / `WorkerOptions` / `cli` / `inference` / `stt` / `tts` 抽象 | `apps/agent/agent_runtime/agent.py` 已在用（本地 1.7.1） | 保持官方框架，只做 provider 插件 |
| 前端实时组件 | `@livekit/components-react`（npm latest=2.9.24）：`LiveKitRoom`、`useSession`、`useAgent`、`useAgentExpression`、`useSessionMessages`、`useTranscriptions`、`useVoiceAssistant`、`BarVisualizer`、`VoiceAssistantControlBar` | 已装 2.9.24，已用旧 API（`useVoiceAssistant`/`useTranscriptions`/`BarVisualizer`） | 逐步切到 Session API（`useSession`/`useAgent`），**勿升 npm 3.0.0**（历史线） |
| 官方 Agents UI 组件 | `@agents-ui/*`（shadcn registry，非 npm 包）：`AgentSessionProvider`、`AgentAudioVisualizerAura`、`AgentChatTranscript`、`AgentSessionView_01` 等 | 手动复制源码到 `components/agents-ui/` | 只复制 **Tailwind v3 兼容**的轻组件（aura/session-provider/shader-toy）；聊天类组件是 **Tailwind v4-only**，等迁 v4 再用 |
| SIP/PSTN | `livekit/sip` | 未来计划 | 直接用，勿自研 |
| 硬件/机器人 | `livekit/portal`（Robot/Operator）、ESP32 客户端 SDK | 未来计划 | 直接用官方 |

**结论：实时/UI/服务器全部用官方；自研只保留业务域（多账号客服：账号/对象/人设/知识库/结算/主管台/报表 = control-plane + packages/*）。**

---

## 2. 前端接入官方 Agents UI —— 关键决策

### 2.1 版本事实（已实测）
- `@livekit/components-react@2.9.24` = npm `latest`，**已导出**：`useSession` / `SessionProvider` / `useSessionContext` / `useSessionMessages` / `useAgent` / `useAgentExpression`（含 `AgentMood`、`EXPRESSION_ATTRIBUTE='lk.expression'`、`DEFAULT_MOOD_TTL_TURNS=2`）/ `useChat` / `useTextStream`。
- **坑**：2.9.24 的 `useAgent()` 返回 **`microphoneTrack`**（文档里写 `audioTrack` 是口径不一致）。
- **坑**：`AgentState` 比 `useVoiceAssistant` 多 `idle` / `pre-connect-buffering` / `failed` 三态。
- `livekit-client@2.22.1` 满足 peerDep；`TokenSource` / `TokenSource.endpoint|literal|custom` 均可用。
- npm 上的 `3.0.0` **不是 latest**（旧 components-core 0.9.2 历史线），**不要升**。

### 2.2 Tailwind v4 边界（最大风险）
- `@agents-ui` 官方目标 **Tailwind CSS v4 + React 19**。
- **v4-only**：`agent-chat-transcript` / `agent-session-view-01` 依赖 AI Elements 基础件（`oklch()` 相对色、`color-mix(in oklch,…)`、`size-2.5`、`inset-s-*`、`shadow-xs`、`@theme` token）。Tailwind 3.4 下会样式错乱。
- **v3 兼容**：`agent-session-provider`、`react-shader-toy`、`agent-audio-visualizer-aura`（+其 hook）。`agent-chat-indicator` 需把 `size-*`/`bg-muted-foreground` 手改 v3。
- 决策：Tailwind v3 阶段只复制 v3 兼容件；转写面板继续用自写（视觉已对齐官方）；迁 v4（未来）再上 `AgentSessionView_01` / `AgentChatTranscript`。

### 2.3 复制源码位置
- 上游：`github.com/livekit/components-js` → `packages/shadcn/`（registry 与组件源码）。
- 本地目标：`apps/web/components/agents-ui/…`、`apps/web/hooks/agents-ui/…`（tsconfig `@/*`→`./*` 可解析；Tailwind content 已含 `./components/**`）。

### 2.4 令牌契约（重要，已实测）
- `TokenSource.endpoint('/api/token')` 期望响应 **`{server_url, participant_token}`**（camelCase `{serverUrl, participantToken}` 亦可）；**`{url, token, roomName}` 会被解析成空值、连接必失败**。
- 我们 FastAPI `POST /api/token {account_id, call_id}` → `{url, token, roomName}`。
- **决策：用 `TokenSource.custom` 直连 control-plane 做键名映射，不动 FastAPI：**
  ```ts
  const tokenSource = TokenSource.custom(async () => {
    const res = await api.token({ account_id: ACCOUNT, call_id });
    return { serverUrl: res.url, participantToken: res.token };
  });
  ```
- `createCall→token` 的业务注册留在闭包内；挂断仍走 `hangup→settle→getSettlement`。
- `AgentSessionProvider` 只提供 context；**需显式 `session.start()` / `session.end()`**；`session.isConnected` 替代 `!!creds.token`。

### 2.5 视觉化与 mood（官方 Expressive 模式）
- `AgentAudioVisualizerAura` props：`size`（icon/sm/md/lg/xl）、`state`、`color`、`colorShift`、`themeMode`、`audioTrack`。**无 track / connecting 状态也能渲染**（官方 hook 对 connecting/disconnected/idle/failed 有分支）。
- 通话内：`const { microphoneTrack, state } = useAgent()`；`const { mood } = useAgentExpression()`；mood → 11 色 `MOOD_COLORS`（`#1FD5F9` 为中性/calm），用 `motion` + `chroma-js` 做 1s 平滑过渡（官方 `useMoodColor` 模式）。
- 11 mood 枚举：`excited happy playful curious surprised hopeful empathetic sad angry anxious calm`；默认 2 个 agent turn 后衰减回 `null`（`ttlTurns` 可调，0=不衰减）。
- 主页无 agent 会话：aura 用 `state="connecting"` + 中性色作纯演示，**替代手绘 `DotVisualizer`**。

---

## 3. 后端情绪（expressive / mood）链路 —— 关键事实

- 前端 `useAgentExpression` 读的是**转录段属性 `lk.expression`**（值 = `{"expression": "...", "mood": "..."}`），**不是** `ConversationItem.emotion`（livekit-agents 1.7.1 无此字段）。
- mood 归一化在 `livekit/agents/tts/_mood.py` 的 `match_mood()`，靠**英文关键词表**；中文 label 会回落 `calm`。
- 发布端在 `voice/room_io/_output.py` 的 `TranscriptForwarder`：**无条件**剥离 LLM 文本里的 markup 并发布 `lk.expression`，**不受 `AgentSession(expressive=…)` 开关 gating**。
- `expressive=True` 的硬门槛（`agent_activity.py:2795`）：TTS 必须是 `livekit.agents.inference.TTS` 网关且声明 markup 方言（仅 `cartesia / inworld / xai / fishaudio` 四家）。自写的 `VolcanoTTS`（直接 `tts.TTS` 子类）不受 expressive 支持。
- **最优路径（Path B，保留火山 TTS，改动 2 处）**：
  1. LLM `instructions` 追加：每句开头吐 `<expr type="expression" label="<英文mood>"/>`（11 枚举之一）。
  2. `AgentSession(..., tts_text_transforms=["filter_markdown","filter_emoji", _strip_expr_markup])` 把 `<expr/>` 从进 TTS 那一路剥掉（转录那一路保留原样，框架自动发布 mood）。
  ```python
  import re
  _EXPR_RE = re.compile(r"<expr\b[^>]*?/>|<[^>]+>")
  async def _strip_expr_markup(text):
      async for chunk in text:
          yield _EXPR_RE.sub("", chunk)
  ```
- **确定性兜底（已实现并验证）**：真实模型未必遵守吐标签指令（实测 DeepSeek 不吐），故 `providers/livekit_plugins.py` 里新增 `ExprAwareLLM` 包装器——每次 assistant 回复前强制前置 `<expr type="expression" label="..."/>`（label = `EmotionProcessor.classify(最后一条 user 文本)`，英文 11 类，无匹配回落 calm）。agent.py 在创建 `AgentSession` 前用 `llm_provider = ExprAwareLLM(llm_provider)` 包裹任意 LLM（DeepSeek/Ollama/Scripted 都适用）。**端到端已验证：接通后前端 `useAgentExpression` 拿到 mood（实测 calm），点阵颜色/文案随情绪驱动，转写文本保持干净。**
- `plugins/emotion.py` 目前是孤立 stub（未 import、3 类中文词），需扩成 11 类英文 key 接进 label 校验/兜底。
- **Bug 隐患**：`providers/livekit_plugins.py` 的 `SherpaSenseVoiceSTT` 里 `re.sub(r"<\|[^|]*\|>", "")` 会把 SenseVoice 的 `<|HAPPY|>` 情绪标签一起洗掉，需修正。

---

## 4. 供应商接入矩阵（少造轮子）

| 供应商 | 官方 SDK | LiveKit 插件 | 决策 |
|---|---|---|---|
| 豆包/火山 | 火山引擎 SDK | 社区 `livekit-plugins-volcengine`（全栈 STT/TTS/LLM/Realtime；TTS 带 emotion/emotion_scale、流式） | **用插件替换自研 `VolcanoTTS+volc_v3_protocol.py`** |
| MiniMax | MiniMax SDK | 官方 `livekit-plugins-minimax-ai`（TTS-only；voice_setting.emotion 9 种） | 需要时直接用官方插件 |
| 阿里 Qwen | Qwen3-ASR / Qwen3-TTS sidecar | 本地自托管；生产 Windows 走 WSL2+vLLM，Mac 开发走 transformers/MPS | 当前默认实现 |
| 智谱 GLM | zai SDK | 无 | **自研包装**（照 `livekit-plugins-openai` realtime 模板；GLM-4-Voice 端到端语音+情绪，价值最高） |
| 讯飞 | 星火 SDK | 无（情绪弱） | 可选/暂缓；接则照模板包装 |
| DeepSeek/Ollama | OpenAI 兼容 | 官方 `livekit-plugins-openai` 可自定义 base_url（Ollama=localhost:11434/v1） | 自研 OpenAI 兼容包装等价，可保留 |
| 本地 sherpa/SenseVoice | — | 无官方插件 | 自研包装合理（保留；注意修情绪标签 bug） |

官方 plugins extras（pip `livekit-plugins-<名>`，76 个）常见：openai, anthropic, deepgram, cartesia, elevenlabs, rime, azure, google, aws, silero, groq, minimax, fishaudio, inworld, xai, turn-detector, fal, playht …（完整清单在 `livekit/agents/livekit-plugins/` 目录）。

---

## 5. 关键契约备忘（前端）

- token 响应字段映射：`{url→serverUrl, token→participantToken}`；`ignoreUnknownFields` 会忽略多余字段（多带 roomName 无害）。
- `useAgent()` 字段：`microphoneTrack` / `cameraTrack` / `state` / `agent`。
- `AgentState` 全枚举：`idle / pre-connect-buffering / connecting / initializing / listening / thinking / speaking / disconnected / failed`。
- 依赖清单（mood 动画）：`motion`、`chroma-js`、`class-variance-authority`、`clsx`、`tailwind-merge`；`next-themes` 可省（暗色主题直接传 `themeMode="dark"`）。
- 官方组件样式：`@livekit/components-styles` 未装；目前靠 globals.css CSS 变量兜底，视觉已对齐（近黑 `#070707` + 亮青 `#1FD5F9` + 灰 mono）。

---

## 6. 官方文档索引（随时查询）

**Agents UI（前端）**
- 总览/安装：https://docs.livekit.io/frontends/agents-ui.md
- 音频可视化（prebuilt + expression/情绪）：https://docs.livekit.io/frontends/agents-ui/audio-visualizer.md 、https://docs.livekit.io/frontends/agents-ui/audio-visualizer/expression.md
- 聊天/转写组件：https://docs.livekit.io/frontends/agents-ui/chat.md
- 会话管理（useSession/messages）：https://docs.livekit.io/frontends/build/sessions.md
- token endpoint 契约：https://docs.livekit.io/frontends/build/authentication/endpoint.md
- Agent 状态：https://docs.livekit.io/frontends/build/agent-state.md
- 组件参考：AgentAudioVisualizerAura（…/reference/components/agents-ui/component/agent-audio-visualizer-aura.md）、AgentChatTranscript（…/agent-chat-transcript.md）、AgentSessionView_01（…/block/agent-session-view-01.md）、AgentSessionProvider（…/agent-session-provider.md）、Next.js token route（…/nextjs-api-token-route.md）

**Agents（后端）**
- Expressive mode：https://docs.livekit.io/agents/models/tts/expressive/
- Agent 框架总览：https://docs.livekit.io/agents.md
- Sessions（logic）：https://docs.livekit.io/agents/logic/sessions/

**源码/示例**
- 前端组件源码：https://github.com/livekit/components-js/tree/main/packages/shadcn
- Agents 框架/插件：https://github.com/livekit/agents
- 官方 starter：https://github.com/livekit-examples/agent-starter-react
- 官方 agents 页（视觉参考）：https://livekit.com/agents

---

## 7. 已执行/待办状态（实施记录）

- ✅ 全站统一 LiveKit 主题（近黑+亮青+灰 mono；StageHeader 顶栏无侧边栏）
- ✅ 主页舞台（遥测 MODEL 子行缩进、转写 AGENT 青/YOU 近白发光）
- ✅ 主管台活跃通话「挂断」按钮（POST /api/calls/{id}/hangup）
- ✅ 官方组件接入（Phase 1）：复制 `components/agents-ui/`（session-provider / react-shader-toy / agent-audio-visualizer-aura / agent-audio-visualizer-grid + hooks）；依赖 cva/clsx/tailwind-merge/motion/chroma-js；`lib/utils.ts`(cn)；grid 的 `bg-current/10`（v4 语法）已改用 `.lk-grid-cell-base`（globals.css）。
- ✅ 会话流官方化（Phase 2）：CallStudio 用 `TokenSource.custom`（createCall→token→`{serverUrl,participantToken}` 映射）+ `useSession` + `AgentSessionProvider` + `session.start()/end()`；保留 hangup→settle 业务流；浏览器 E2E `BROWSER_E2E_PASSED`。
- ✅ 可视化官方化（Phase 3）：官方 `AgentAudioVisualizerGrid`（点阵，官方 agents 页同款）替换自绘 canvas；`VoiceAgentInterface`（grid + mood 颜色）+ `hooks/use-mood-color.ts`（官方 11 色映射 + motion/chroma 平滑过渡）；主页麦克风用 `LocalAudioTrack` 喂官方多频段音量；已删除 `DotVisualizer.tsx`。
- ✅ 后端 mood 链路（Phase 4，Path B）：`agent.py` 的 instructions 追加「每句开头吐 `<expr type="expression" label="英文mood"/>`」规则；`AgentSession(tts_text_transforms=[...])` 追加 `_strip_expr_markup` 从 TTS 路径剥标签（转录路径框架自动发布 `lk.expression`）；`plugins/emotion.py` 扩成官方 11 类英文 mood（中文关键词→英文 label，normalize 兜底）；`SherpaSenseVoiceSTT` 不再一刀切洗掉 `<|HAPPY|>` 情绪标签（记录到 `last_emotion`，显示文本仍干净）。
- ✅ mood 确定性兜底 + 端到端验证：`ExprAwareLLM` 包装器强制前置 `<expr>` 标签（不依赖模型遵守指令）；真实接通实测前端 `useAgentExpression` 拿到 mood（calm），点阵颜色/文案随情绪驱动，转写干净。
- ✅ Qwen3-ASR/TTS sidecar 全链路（Phase 6）：`scripts/start_sidecars.sh` 一键起两个 sidecar（8787/8788）；`scripts/smoke_sidecars.py` 全绿（三语 ASR + 预置/克隆 TTS + 克隆音色回灌）；设置/人设页「克隆/试听」经 control-plane 代理调用 sidecar。
- ✅ A 线三语 E2E（Phase 7）：`tools/browser-e2e/trilingual.mjs` → `TRILINGUAL_E2E 3/3 PASSED`（zh/yue/en 各出 YOU 转写 + AGENT 回复；`LanguageState` 让回复语言随输入语言）。
- ✅ 关键修复记录：
  - Ollama 改用原生 `/api/chat` + `"think": false`（OpenAI 兼容端点不支持关 thinking，9B 回复慢且内容空）；`LLM_MAX_TOKENS` 默认 256，E2E 用 160。
  - `_chat_messages` 修复：`ChatContent` 文本部分是纯 `str`，旧代码把用户文本全部丢空。
  - control-plane 每请求独立 SQLAlchemy Session（原来共享 Session 并发踩踏 → 全部 500）；`create_turn` 幂等（并发同 turn_id 冲突回滚返回已存在行）。
  - Silero VAD `max_buffered_speech` 调至 15s（env 可改），假音频补前后静音。
- 🟡 供应商插件化（Phase 5，已评估/暂缓）：社区 `livekit-plugins-volcengine` 依赖 `livekit-agents<1.7`，与本项目 1.7.1 冲突，**暂不替换**自研 VolcanoTTS（Path B 已让 mood 生效，不依赖它）；MiniMax 官方插件（`livekit-plugins-minimax-ai`）、阿里社区插件（`livekit-plugins-aliyun`）按需再接；智谱 GLM-Realtime 自研包装（照 livekit-plugins-openai 模板）留作专项。
- ⏳ 未来：Tailwind v4 → `AgentSessionView_01`/`AgentChatTranscript`（官方聊天组件 v4-only）；SIP（livekit/sip）；硬件（Portal/ESP32）；`reports`/`settings` 真实数据。
- ⚠️ 已知环境问题：Docker 镜像源（轩辕镜像）限流 403，`docker compose build agent/web` 暂无法重建；Mac 开发机用宿主机 `.venv312` 直接跑 `python -m agent_runtime.main start`（editable 安装，代码即时生效）+ `scripts/start_sidecars.sh`。镜像源恢复后需重建 agent/web 镜像并恢复 `docker compose up -d agent`。
