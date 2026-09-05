# Repository Guidelines

Contributor guide for **Bok Voice**, a local-first voice customer-service assistant (A-line) and real-time interpretation workbench (B-line). Read [RUNTIME_TOPOLOGY.md](docs/RUNTIME_TOPOLOGY.md) before changing runtime behavior, and [AGENT.md](AGENT.md) for LiveKit decisions. S2S（端到端语音模型）评估结论与试点路线见 [docs/S2S_ROADMAP.md](docs/S2S_ROADMAP.md)——粤语输出当前无本地 S2S 可用，勿轻言换架构。

## Project Structure & Module Organization

- `apps/agent` — LiveKit agent runtime (VAD → ASR → LLM → TTS orchestration and providers).
- `apps/control-plane` — FastAPI API (:8000): objects, personas, knowledge, templates, calls, audit, tokens.
- `apps/web` — Next.js static export (Tauri-hosted UI).
- `packages/` — shared Python: core models, SQLite repository, knowledge, observability.
- `services/` — local sidecars: Qwen3-ASR (:8787), Qwen3-TTS (:8788), realtime-translation worker (:8790), LiveKit server config.
- `desktop/` — Tauri shell and bundled runtime assembly.
- `tools/bok.py` — single orchestrator: `serve`, `status`, `down`, `doctor`, `download`.
- `tests/`, `services/realtime-translation/test`, `scripts/` — Python/Node suites and CI helpers.

## Build, Test, and Development Commands

```bash
./scripts/bootstrap.sh                  # create .venv312 + install Python deps
./scripts/test.sh                       # full pytest suite
cd services/realtime-translation && npm ci && npm test   # B-line Node tests
cd desktop/src-tauri && cargo test      # Rust shell tests
python tools/bok.py serve               # start the full local stack
python tools/bok.py status | down | doctor --packaged
python tools/bok.py prod install        # generate launchd plist units (KeepAlive auto-restart)
python tools/bok.py prod status         # official health surface (server GET / + worker :8081/worker)
E2E_ONLY=cantonese .venv312/bin/python scripts/e2e_trilingual_livekit.py  # A-line E2E
cd apps/web && npm run build            # static web export
cd desktop && npx tauri build --bundles app   # macOS bundle
```

## Coding Style & Naming Conventions

- Python: PEP 8, 4-space indent, `from __future__ import annotations`, type hints on signatures; run `python -m compileall -q` after edits.
- TypeScript/React: match surrounding components; no formatter is enforced in CI.
- Naming: `snake_case` Python modules/functions, kebab-case services; model repo dirs use `owner--name` as declared in `tools/bok.py` `MODELS`.
- Bash scripts: `set -euo pipefail`.

## Language / Terminology Rules（跨层约束）

- **粤语规范值 = 小写 `cantonese`，全时空唯一拼写**，语言三态只有 `zh / cantonese / en`。全栈（DB、web、TS 类型、prompt 分支、`LanguageState`、voice-map 键、`speaker_cantonese`）一律 `cantonese`；旧拼写已存量清零（CP 启动迁移 `deps.py build_engine()` 是唯一兼容点），运行时代码**不留任何别名分支**。
- **防复发门禁**：`tests/test_cantonese_terminology.py` 扫描全部跟踪源文件，旧拼写只允许出现在白名单单点，新增即测试失败——字段单轨化，杜绝双轨技术债。
- **外部系统真字面量不改（门禁白名单内）**：Wikipedia 域名 `zh-yue.wikipedia.org`、SenseVoice 输出标签 `YUE`（入内即归一 cantonese）、Volcano dialect 枚举、MiniMax 音色 ID `Cantonese_*`。音色 id 值是不透明标识符，不属语言字段。
- **每通对话语言固定（A 线，取代逐轮语言跟随）**：粤语通话全程粤语、中文全程中文、英文全程英文，中途不切换。会话装配时一次钉死三方：ASR hint 恒钉通话语言（`_call_language` 人设→对象→zh；`PinnedLanguageState`+`pin_language=True` 三语全钉，zh 也下发 `Chinese`；设置 `asr.language_mode=fixed`+显式 `language` 仍优先，**mode=auto 在 A 线=钉到本通语言，不是滞回跟随**——LanguageState 滞回/sticky 机制已在 A 线退役，B 线不变）、LLM【用户语言】规则装配时 `set_user_language` 一次字节静态整通（逐轮钩子不碰语言）、TTS 音色+MiniMax `language_boost`（zh→Chinese、cantonese→Chinese,Yue、en→English，env 按通话注入）按通话语言固定。sidecar 传 `language=cantonese` 等规范名（mlx 大小写不敏感回填模型 config）消除 auto 误判（啱唔啱→难唔难）。

## Architecture Boundaries & Runtime Rules

- **官方契约优先**：除知识库/对象/话术业务域 + 本地模型插件 + B 线双栏字幕 + Tauri 设备层外，全部对齐 LiveKit 官方栈（详见 `docs/DEV_TOOLS.md` + `AGENT.md` 决策记录）。查官方用 docs MCP `https://docs.livekit.io/mcp`（免费无 key）。**别重造官方轮子**（LLM 客户端/转写落库/崩溃补位等先查官方姿势）。
- **话术分步推进**：`apps/agent/agent_runtime/flow.py` 的 `FlowController` 是唯一推进引擎（`detect_whatsapp_signal`/`should_auto_advance`/`decide_advance`/judge）。agent 每轮 `on_user_turn_completed`：WhatsApp detect → rule_verdict+auto_advance → 模糊轮背景 LLM judge（fire-and-forget，`_judge_inflight` 防叠）；语言已整通固定，钩子不做任何语言处理。改动推进逻辑先读 `tests/test_flow_controller.py`。
- **LLM system 顺序（KV-cache 关键，勿打乱；2026-09-05 二次实证修订）**：请求组装=`render_instruction_prefix()`（整场静态：语言规则/节奏/准则/话术总览/**对象档案**）+ 人设 base 作 system；**易变尾部（当前步/记忆/CONTEXT_RAG 时的知识联网）采用「跨轮纯追加」：每条 user 消息首次出现时拼上当时的尾部并由 `ContextState._applied_tails` 账本冻结，此后每轮原样重放（ContextAwareLLM）**。铁律=上一轮请求必须是下一轮的**严格前缀**（mlx_lm LRUPromptCache 只复用严格前缀；旧「尾部只拼最后一条 user、下轮即剥」会在中途分叉→cached 恒=system 锚点、历史每轮全量重 prefill——2026-09-05 INFO+指纹实证后改为账本冻结重放）。首轮预热用「真实开场白文本作 assistant 轮」的 turn-1 同构形状（`_build_prefix_prewarm_messages`），system 串联必须复刻 to_provider_format 的 `\n` join。历史截断是摊销式（超 2×max_turns 才截回 max_turns，`LLM_HISTORY_TURNS=8`）。会话首轮冷 prefill 由 `LLM_PREFIX_PREWARM=1` 真 system 预热吸收。`LLM_TTFT_MS` 日志带 `cached=N/M` 命中读数。
- **知识=沉淀非逐轮注入**：提示词只含对象信息+话术+session 记忆。知识/联网检索**默认全关**（`_context_rag_enabled()` 无参默认 False，`CONTEXT_RAG=1` 才开且只渲染进尾部 `rag_enabled` 段；`WEB_SEARCH` 默认 0）。沉淀知识经 `ContextState.set_object_brief()`（≤2 行×150 字，显式换行分界、多句整行截断不丢字段）在装配时渲染为静态【对象档案】入前缀。
- **VAD/endpointing 基线（2026-09-05 句号级提交）**：`min_silence=0.45` / endpointing `min_delay=0.25`/`max_delay=0.6` / `min_speech=0.15`；A 线 `turn_detection=stt`（STT 句末 `END_OF_SPEECH` 提交，说话中按句成轮，LLM/TTS 与说话重叠——≤1s 通路）。当年压端点致哑火=轮次在离线 ASR final 前提交；现提交结构性等待 STT FINAL 且句级路径 FINAL 即句文，三语 E2E 实证 0 丢转写。**回退须成对**：`TURN_DETECTION=` 置空回 EOT 档 + `QWEN3_ASR_SENTENCE_COMMIT=0`（B 线 interp env 已强制 0，否则句级 FINAL 叠进停嘴 FINAL 重复转写）；**kill-switch 档 endpointing min_delay 自动回 ≥0.35（`_endpointing_delays_from_env` 强制，无需手动）**——0.25 只对 stt 句级提交校准过，未校准 EOT 配 0.25 早提交截断粤语（p6 实证）。句级保护：≥6 字/无 ≥2 连续字母数字串/双窗稳定/1.5s 限流/小数点含窗口末尾不劈句；真实音频滑窗句间只出逗号 → VAD 停嘴（≥0.45s 微停顿）也是句边界源（`QWEN3_ASR_SENTENCE_PAUSE_TRIGGER` 默认 1）。改这三处默认值要同步 agent 环境默认 + `repository.default_settings` + web `EMPTY_FORM`。
- **打断 = 官方误打断自愈组合**：`interruption.min_duration=0.6` + `resume_false_interruption=True` + `false_interruption_timeout=1.0`（真插话 0.6s 让位；1s 内无转写=噪声误打断，AI 自动从暂停处续讲）。旧 1.2s 高门槛已废（压住真插话）。`INTERRUPT_MIN_DURATION`/`RESUME_FALSE_INTERRUPTION`/`FALSE_INTERRUPTION_TIMEOUT` env 可回退。**心跳钩子（`_arm/_disarm/_fire/_on_agent_state/_on_user_state` 与 `session.on` 注册）必须先于 `session.start`/开场白**——开场白播完的 listening 转换发生在注册前会令首段沉默 arm 失效（2026-09-06 审查 P1）；开火护栏抽成纯函数 `_nudge_should_fire`（test_heartbeat.py）。
- **VAD 微停顿短尾不补发**（打断自噬，2026-09-06）：pause-commit 已发句级 FINAL 后，<6 字短尾（「係。」）不再补发第二条 FINAL——否则新用户轮会 interrupt 掉生成中未出声的回复（「每问无答」根因之一）；≥6 字照发。
- **ASR 流式 partial + 抢跑**：sidecar mlx 后端 `QWEN3_ASR_STREAM=1`（默认）每 ~700ms（`QWEN3_ASR_PARTIAL_MS`，旧 400）滑窗出 partial、窗口 ≤12s、**FINAL 后停发**（削 GPU 租户）；agent `Qwen3ASRLiveSTT` 发 `INTERIM_TRANSCRIPT`（字幕）+ 稳定前缀（首 ≥6 字、增长 ≥4 字才发）→`PREFLIGHT_TRANSCRIPT`（语言不匹配抑制 `QWEN3_ASR_PREFLIGHT_LANG_GATE`，每通语言固定后基本闲置）；停嘴增量 finish（partial 拼接+数字串/接缝保护回退整句，WhatsApp 零降级）；en finish 走 `English` hint。抢跑 `PREEMPTIVE_GENERATION=1`、`PREEMPTIVE_MAX_RETRIES=3`（官方对抖动转写建议，勿调大——被杀投机请求在 mlx 批内残留挤占下个 prefill）；`PREEMPTIVE_TTS=0`（实测句级提交下零收益，MiniMax 按字计费白烧）。**推进轮不错步**：步骤推进/REFUSE 时向 turn_ctx 落步骤标记触发抢跑快照失效重建。
- **DB 迁移**在 `apps/control-plane/control_plane/deps.py` `build_engine()` 幂等段（补列 + 数据迁移），启动时自动跑；不要在别处手写迁移。

## Testing Guidelines

- Python: pytest `tests/test_*.py` (`test_*` functions); Node: `node --test` under `services/realtime-translation/test`.
- 改完 Python 跑 `python -m compileall -q apps packages services tools scripts`；web 改动跑 `cd apps/web && npx tsc --noEmit && npm run build`。
- **术语门禁**：`tests/test_cantonese_terminology.py` 是全仓测试的一部分（新增 `yue` 字面量即失败）。
- E2E needs the running stack and local models. **Never fake-green**: A-line E2E must use the real `/api/token` (`E2E_SELF_TOKEN=1` is debug-only).
- Merge gate: pytest, `npm test`, `cargo test`, and `scripts/verify_bundle.sh` (`--staging`, `--app`, `--doctor`, one mode per run) all green; `doctor --packaged` must report `token endpoint: ok (real JWT)`.

## Commit & Pull Request Guidelines

- Conventional commits with a scope (`fix(desktop):`, `ci(release):`, …); body states root cause and verification evidence.
- One logical change per commit.
- Update `docs/RUNTIME_TOPOLOGY.md` / `docs/REPO_MAP.md` whenever ports, paths, or data flow change.
- **Do not tag or release until full local acceptance passes** (project policy; releases are CI-gated and user-verified).

## Operational Constraints

- Never reintroduce Ollama, Docker, or CosyVoice runtime paths (removed by design).
- Bundled app resources are read-only: SQLite, vault, `tts-data`, logs, and metrics always live in app-data.
- **dev 栈 vs 桌面包版本断层**：`python tools/bok.py serve` 的开发栈用仓库 `runtime/`（symlink→`desktop/runtime`，gitignored）+ PYTHONPATH 指向仓库 `apps/`，改代码 `bok.py down && serve` 即生效；但 `/Applications/BokVoice.app`（桌面包）跑的是打包进 `_up_/` 的旧代码。测试改动先确认跑的是哪个。
- 本地 LLM 是 **mlx_lm server**（非 vLLM，Mac 上 vLLM 跑不了），prefill ~0.6k token/s 架构硬墙；A 线 :1235（`--prompt-cache-size 128 --prompt-cache-bytes 6GB --prefill-step-size 1024`，dev `--log-level INFO` 看 Prompt processing progress），B 线翻译走 :1236 **Hy-MT2 专用 MT server**（`MODELS["mt"]`，缺模型自动跳过 B 线回退 :1235；`StatelessMTLLM` 官方模板逐句无状态）。**永不给请求加 seed/draft**（会关 continuous batching）。评估模型用 OpenAI 兼容端点，request model 必须填真实模型路径。
- **MiniMax TTS 运行规约**：凭据存设置 DB `tts.api_key`（勿提交仓库，`/tmp/mmkey` 是本地测试临时档）；**venv 无系统 CA——`SSL_CERT_FILE`（certifi）已由 bok.py 固化进全部 worker env**，手起进程要自带；classic WS 默认+keep-warm 池（`MINIMAX_WS_POOL=1`，握手 216-648ms 全离关键路径），`MINIMAX_WS_MODE=bidi` 实验档（服务端攒句/cancel 不拆连接，探针不优于 classic，留作 B 线同传基建）；A 线默认音色兜底 `Cantonese_crisp_news_anchor_vv2`（三语通用，空音色防 beep）；A 线 2.8-hd / B 线 turbo 按 `MINIMAX_MODEL` 分进程注入。链内 `TTS_FIRST_AUDIO_MS` 含「等 LLM 首句文本」时间，别当纯合成延迟读。**卡死自愈看门狗**：task_started 后 `MINIMAX_FIRST_AUDIO_TIMEOUT_S`（默认 6s）无首包 → 断开重连+重发已发文本（日志 `MINIMAX_TTS_STALL`）——MiniMax 云端偶发 >8s 无首包是「每问无答」的现行真凶（2026-09-06 实证）。音色有效清单以 `apps/web/lib/minimax-voices.ts` 为唯一数据源（新 id 须先 `/api/tts/preview` 验 200；EN 旧 `male/female_english_speaker` 已实测 2054 移除）。
- 延迟调试读 agent.log 打点：`QWEN3_ASR_HINT`/`ASR_MS`/`LLM_TTFT_MS`（含 cached=N/M）/`LLM_FIRST_SENT_MS`/`TTS_FIRST_AUDIO_MS`；`scripts/measure_latency.py`/`scripts/measure_prompt.py` 可复测；历史预算表在 `.superpowers/sdd/2026-09-04-mt-minimax-latency/`（p4-p7 为 ≤1s 攻坚实测档案）。
- **KV-cache/缓存诊断（2026-09-06 起）**：`BOK_LLM_MSG_DEBUG=1` 打逐请求逐消息 sha1 指纹（定位前缀分叉消息）；`scripts/llm_cache_report.py <worker.log>` 出 cached 分布汇总；档案在 `.superpowers/sdd/2026-09-05-llm-cache/FINDINGS.md`（三根因+锚点封顶开放项=mlx_lm 上游 LRU 段键可查性异常，影响每轮历史增量 0.1-0.5s）。
- **回声/AEC**：浏览器采集默认已开 `echoCancellation/noiseSuppression/autoGainControl`（livekit-client 默认预设，勿关）；扬声器外放+失效输出设备会造成 AI 自闻自打断（sink 失败已自动清档回默认输出，日志 `Failed to set sink id` 出现即说明存档设备已失效）。实测/演示**建议戴耳机**；agent 侧 ai_coustics 降噪为可选后手（需授权），未接。
