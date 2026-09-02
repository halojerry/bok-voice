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
E2E_ONLY=yue .venv312/bin/python scripts/e2e_trilingual_livekit.py  # A-line E2E
cd apps/web && npm run build            # static web export
cd desktop && npx tauri build --bundles app   # macOS bundle
```

## Coding Style & Naming Conventions

- Python: PEP 8, 4-space indent, `from __future__ import annotations`, type hints on signatures; run `python -m compileall -q` after edits.
- TypeScript/React: match surrounding components; no formatter is enforced in CI.
- Naming: `snake_case` Python modules/functions, kebab-case services; model repo dirs use `owner--name` as declared in `tools/bok.py` `MODELS`.
- Bash scripts: `set -euo pipefail`.

## Testing Guidelines

- Python: pytest `tests/test_*.py` (`test_*` functions); Node: `node --test` under `services/realtime-translation/test`.
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
