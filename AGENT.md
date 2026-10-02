# Bok Voice × LiveKit —— Agent 知识备忘（官方复用 & 决策记录）

> 本文档是「如何在 Bok Voice 里最大化复用官方 LiveKit 能力、少造轮子」的长期备忘。
> 任何后续会话/Agent 都可以先读本文件，再决定动手。最后更新：2026-09-25（当日审计过版：TTS 统一 MiniMax 后的供应商矩阵/版本号/已移除组件全面修正）。

---

## 0. 官方优先铁律（2026-09-23 立法）

**LiveKit 官方已有的组件和方法，优先用官方，禁止先手搓。**动手前必查官方事实——查询入口 = **LiveKit docs MCP**（`https://docs.livekit.io/mcp`，免费无 key；CLI 侧 `lk docs search` 同源）。

三档判据（2026-09-09 拍板，细则见 AGENTS.md「官方组件优先」条）：

1. **新功能先查官方**组件/官方模式：@agents-ui 走 shadcn registry 增量拉；框架能力（fallback adapter / test framework / tts_text_transforms / MCP toolset / RPC 等）先读官方文档再决定写不写。
2. **授权自定义层不算造轮子**：本地模型插件（STT/LLM/TTS sidecar）、业务工作台 UI、bok.py 编排、B 线业务语义——官方无对应物。
3. **实证分叉必须有档案**：确要偏离官方姿势的（尾部冻结重放 / FlowController 规则先行 / 回声守卫 / setSinkId / B 线双栏），动前必读 `.superpowers/sdd/2026-09-09-official-first/FINDINGS.md`；新增分叉须带 call-ID/测量级证据入档，写明「官方对应物是什么、为什么不能用」。

配套纪律：**每季度把分叉清单对官方文档过一遍**（用 docs MCP），给每处手搓归类——A=官方没有 / B=官方有但实测不行（附数据）/ C=官方有该用未用（=债）。官方追上来的（例：test-framework/JudgeGroup、FallbackAdapter、`text_transforms.replace()`）切官方；A/B 类继续养但档案要保持新鲜。

---

## 1. 项目与官方能力对照（结论速览）

| 层面 | 官方现成件 | Bok Voice 现状 | 决策 |
|---|---|---|---|
| 实时服务器 | `livekit/livekit-server` | 已用（**bok.py 直起内嵌 binary :7880**；Docker 运行时已退役，AGENTS.md 禁 Docker 运行路径） | 保持 |
| Agent 运行时 | `livekit.agents`（Python）：`AgentSession` / `Agent` / `WorkerOptions` / `cli` / `inference` / `stt` / `tts` 抽象 | `apps/agent/agent_runtime/agent.py` 已在用（本地 **1.8.2**，锁 <1.9；2026-09-25 复核） | 保持官方框架，只做 provider 插件 |
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
- `livekit-client@2.22.3`（^2.22.3，2026-09-25 复核）满足 peerDep；`TokenSource` / `TokenSource.endpoint|literal|custom` 均可用。
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
- **`plugins/emotion.py` 已扩成官方 11 类英文 mood 并接线**（§7 Phase 4 已实施；2026-09-25 复核不再是 stub）。
- （历史）SherpaSenseVoiceSTT 的 `<|HAPPY|>` 情绪标签 bug：该组件已整体移除——2026-09-25 复核 `providers/` 零 sherpa/SenseVoice 代码残留，此条仅存档。

---

## 4. 供应商接入矩阵（少造轮子）

| 供应商 | 官方 SDK | LiveKit 插件 | 决策 |
|---|---|---|---|
| 豆包/火山 | 火山引擎 SDK | 社区 `livekit-plugins-volcengine`（全栈 STT/TTS/LLM/Realtime；TTS 带 emotion/emotion_scale、流式） | **用插件替换自研 `VolcanoTTS+volc_v3_protocol.py`** |
| MiniMax | MiniMax SDK | 官方 `livekit-plugins-minimax-ai`（TTS-only；voice_setting.emotion 9 种） | **2026-09-24 起自建 `MiniMaxTTS`（live WS bidi + pregen 罐头/垫话物化，livekit_plugins.py）为生产线**——A/B 线统一音色；官方插件按需再评估 |
| 阿里 Qwen | Qwen3-ASR sidecar（:8787） | **ASR=本地默认实现（mlx 8bit）**；Qwen3-TTS sidecar（:8788）2026-09-24 起降级为 bench/回退档——**生产线 TTS=MiniMax 云**（音色一致性） | 保持 |
| 智谱 GLM | zai SDK | 无 | **自研包装**（照 `livekit-plugins-openai` realtime 模板；GLM-4-Voice 端到端语音+情绪，价值最高） |
| 讯飞 | 星火 SDK | 无（情绪弱） | 可选/暂缓；接则照模板包装 |
| DeepSeek/Ollama | OpenAI 兼容 | 官方 `livekit-plugins-openai` 可自定义 base_url（Ollama=localhost:11434/v1） | 自研 OpenAI 兼容包装等价，可保留 |
| 本地 sherpa/SenseVoice | — | 无官方插件 | （已移除——2026-09-25 复核零代码残留） |

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
- ✅ 官方组件接入（Phase 1）：复制 `components/agents-ui/`（session-provider / react-shader-toy / agent-audio-visualizer-aura / agent-audio-visualizer-grid + hooks）；依赖 cva/clsx/tailwind-merge/motion/chroma-js；`lib/utils.ts`(cn)；grid 的 `bg-current/10`（v4 语法）曾降配为 `.lk-grid-cell-base`（globals.css），Tailwind 4 迁移已删该降配类、恢复官方写法。
- ✅ 会话流官方化（Phase 2）：CallStudio 用 `TokenSource.custom`（createCall→token→`{serverUrl,participantToken}` 映射）+ `useSession` + `AgentSessionProvider` + `session.start()/end()`；保留 hangup→settle 业务流；浏览器 E2E `BROWSER_E2E_PASSED`。
- ✅ 可视化官方化（Phase 3）：官方 `AgentAudioVisualizerGrid`（点阵，官方 agents 页同款）替换自绘 canvas；`VoiceAgentInterface`（grid + mood 颜色）+ `hooks/use-mood-color.ts`（官方 11 色映射 + motion/chroma 平滑过渡）；主页麦克风用 `LocalAudioTrack` 喂官方多频段音量；已删除 `DotVisualizer.tsx`。
- ✅ 舞台换装（2026-09-18 Light UI P3）：registry 重拉官方件；CallStudio 直用 `AgentAudioVisualizerAura`（`themeMode="light"`，mood 色经 `useMoodColor` 在 JSX 层接线，`VoiceAgentInterface` 包装层删除）+ 官方 `AgentControlBar`（`saveUserChoices={false}` 守 `bok.audio.*` 单轨、controls 裁至 mic）+ `StartAudioButton`；**挂断保真取舍：不用官方 AgentDisconnectButton**（其 onClick 后恒调 `session.end()`，与复合 leave()=end→hangup 上报→结算轮询双触发），挂断仍走既有「挂断」按钮。
- ✅ 后端 mood 链路（Phase 4，Path B）：`agent.py` 的 instructions 追加「每句开头吐 `<expr type="expression" label="英文mood"/>`」规则；`AgentSession(tts_text_transforms=[...])` 追加 `_strip_expr_markup` 从 TTS 路径剥标签（转录路径框架自动发布 `lk.expression`）；`plugins/emotion.py` 扩成官方 11 类英文 mood（中文关键词→英文 label，normalize 兜底）；`SherpaSenseVoiceSTT` 不再一刀切洗掉 `<|HAPPY|>` 情绪标签（记录到 `last_emotion`，显示文本仍干净）。
- ✅ mood 确定性兜底 + 端到端验证：`ExprAwareLLM` 包装器强制前置 `<expr>` 标签（不依赖模型遵守指令）；真实接通实测前端 `useAgentExpression` 拿到 mood（calm），点阵颜色/文案随情绪驱动，转写干净。
- ✅ Qwen3-ASR/TTS sidecar 全链路（Phase 6）：`scripts/start_sidecars.sh` 一键起两个 sidecar（8787/8788）；`scripts/smoke_sidecars.py` 全绿（三语 ASR + 预置/克隆 TTS + 克隆音色回灌）；设置/人设页「克隆/试听」经 control-plane 代理调用 sidecar。（2026-09-25 复核：start_sidecars.sh 已不存在，sidecar 现由 `bok.py serve` 统一托管。）
- ✅ A 线三语 E2E（Phase 7）：`TRILINGUAL_E2E 3/3 PASSED`（zh/cantonese/en 各出 YOU 转写 + AGENT 回复）。（2026-09-25 注：浏览器 E2E 已被 `scripts/e2e_trilingual_livekit.py` 取代；「LanguageState 回复语言随输入语言」为当时行为——2026-09-05 起 A 线改为**每通语言钉死**，见 AGENTS.md 语言三态条。）
- ✅ 关键修复记录：
  - Ollama 改用原生 `/api/chat` + `"think": false`（OpenAI 兼容端点不支持关 thinking，9B 回复慢且内容空）；`LLM_MAX_TOKENS` 默认 256，E2E 用 160。
  - `_chat_messages` 修复：`ChatContent` 文本部分是纯 `str`，旧代码把用户文本全部丢空。
  - control-plane 每请求独立 SQLAlchemy Session（原来共享 Session 并发踩踏 → 全部 500）；`create_turn` 幂等（并发同 turn_id 冲突回滚返回已存在行）。
  - Silero VAD `max_buffered_speech` 调至 15s（env 可改），假音频补前后静音。
- 🟡 供应商插件化（Phase 5，已评估/暂缓）：社区 `livekit-plugins-volcengine` 依赖 `livekit-agents<1.7`，与本项目版本面冲突（当时对 1.7.1 结论；本项目现已 1.8.2，该依赖未随版复核）；**暂不替换**自研 TTS（2026-09-24 起生产线=自建 MiniMaxTTS，Path B mood 链路不依赖火山）；MiniMax 官方插件（`livekit-plugins-minimax-ai`）、阿里社区插件（`livekit-plugins-aliyun`）按需再接；智谱 GLM-Realtime 自研包装（照 livekit-plugins-openai 模板）留作专项。
- ⏳ 未来：Tailwind v4 → `AgentSessionView_01`/`AgentChatTranscript`（官方聊天组件 v4-only）。**已探明（2026-09-04）**：registry 依赖闭包除 `@agents-ui/*` 外还需 `@/components/ui/{button,toggle,bubble,marker,message-scroller}`（这些**不在 shadcn 主 registry**，`npx shadcn add` 会 404）——迁移 v4 时需一并自建这些 ui 原语或升级 shadcn 基础件；`AgentTrackControl` 的设备选择不覆盖系统原生输出（`lib/audio.ts` 永久自研；Tauri 壳 2026-09-17 退役后=浏览器 setSinkId，Safari 回退系统默认）。SIP（livekit/sip）；硬件（Portal/ESP32）；`reports`/`settings` 真实数据。
- ✅ 已解决（2026-08-30）：`scripts/rebuild_images_offline.sh` 从现有镜像派生本地基础镜像后离线重建 agent/web/control-plane，Docker 化全链路恢复；`docker compose up -d --force-recreate agent web control-plane` 后 zh E2E 1/1 PASSED。Dockerfile 增加 `ARG BASE_IMAGE` 支持离线构建。镜像源正常时仍可直接 `docker compose up -d --build`。
