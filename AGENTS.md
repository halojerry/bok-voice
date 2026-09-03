# Repository Guidelines

Contributor guide for **Bok Voice**, a local-first voice customer-service assistant (A-line) and real-time interpretation workbench (B-line). Read [RUNTIME_TOPOLOGY.md](docs/RUNTIME_TOPOLOGY.md) before changing runtime behavior, and [AGENT.md](AGENT.md) for LiveKit decisions.

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

- **粤语规范值 = 小写 `cantonese`**，语言三态只有 `zh / cantonese / en`。全栈（DB、web、TS 类型、prompt 分支、`LanguageState`、voice-map 键、`speaker_cantonese`）一律用 `cantonese`，**新代码永不产出 `yue`**；旧 `yue` 仅在运行时入口（`_normalize_lang`/`_classify_spoken_language`/`_collapse_voice_map` 等）作**只读别名**归一。
- **供应商/资源字面量不改**：MiniMax 音色 ID `Cantonese_*`、Wikipedia 域名 `zh-yue.wikipedia.org`、SenseVoice 标签 `YUE`、sherpa 模型目录 `zh-en-ja-ko-yue`、Volcano dialect `yue`、`yue.wav` 等音频文件名、克隆音色 id（`acceptance-yue`…）。
- ASR 会话语言为粤语时给 sidecar 传 `language=cantonese`（mlx 大小写不敏感回填模型 config 规范名 `Cantonese`），消除 auto 误判成普通话（啱唔啱→难唔难）。

## Architecture Boundaries & Runtime Rules

- **话术分步推进**：`apps/agent/agent_runtime/flow.py` 的 `FlowController` 是唯一推进引擎（`detect_whatsapp_signal`/`should_auto_advance`/`decide_advance`/judge）。agent 每轮 `on_user_turn_completed`：语言锚定 → WhatsApp detect → rule_verdict+auto_advance → 模糊轮背景 LLM judge（fire-and-forget，`_judge_inflight` 防叠）。改动推进逻辑先读 `tests/test_flow_controller.py`。
- **LLM system 顺序（KV-cache 关键，勿打乱）**：`ContextState.render_instruction_prefix()`（稳定指令：用户语言规则/回复节奏/应答准则/话术总览/当前步）+ 人设 base（`_instructions`+facts）在前，`render_context_tail()`（每轮变的知识/联网/记忆）垫最后 → token0 前缀逐轮字节不变，命中 mlx_lm prompt KV-cache。**不要把会变的检索段插进稳定段中间**（前缀一断整段重 prefill）。
- **RAG 门控**：绑了分步话术（`flow_ctrl.has_steps`）的封闭流程默认**不做知识库/联网检索**（单对象只上话术），`_context_rag_enabled()` 判定；`CONTEXT_RAG=1` 强制开。
- **VAD/endpointing 基线不可压缩**：`min_silence=0.45` / endpointing `min_delay=0.35`/`max_delay=1.2` / `min_speech=0.15`。离线式 ASR 从停嘴到转写回来需 ~0.5-1.2s，端点判定太紧会让轮次在转写返回前提交 → 回复被丢、agent 哑火（曾为此回滚）。真降延迟走 ASR 本身/流式 interim，勿压端点判定。改这三处默认值要同步 agent 环境默认 + `repository.default_settings` + web `EMPTY_FORM`。
- **DB 迁移**在 `apps/control-plane/control_plane/deps.py` `build_engine()` 幂等段（补列 + 数据迁移），启动时自动跑；不要在别处手写迁移。

## Testing Guidelines

- Python: pytest `tests/test_*.py` (`test_*` functions); Node: `node --test` under `services/realtime-translation/test`.
- 改完 Python 跑 `python -m compileall -q apps packages services tools scripts`；web 改动跑 `cd apps/web && npx tsc --noEmit && npm run build`。
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
- 本地 LLM 是 **mlx_lm server**（非 vLLM，Mac 上 vLLM 跑不了），prefill ~0.6k token/s 架构硬墙；`bok.py` 已带 `--prompt-cache-size 128`。评估模型用 `mlx_lm server` 的 OpenAI 兼容端点（:1235），request model 必须填真实模型路径。
- 延迟调试读 agent.log 打点：`QWEN3_ASR_HINT`/`ASR_MS`/`LLM_TTFT_MS`/`LLM_FIRST_SENT_MS`/`TTS_FIRST_AUDIO_MS`；`scripts/measure_latency.py`/`scripts/measure_prompt.py` 可复测。
- MiniMax 凭据存设置 DB `tts.api_key`（勿提交仓库），`/tmp/mmkey` 是本地测试临时档。
