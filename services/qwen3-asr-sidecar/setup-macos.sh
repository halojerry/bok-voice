#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="${QWEN3_ASR_VENV:-$ROOT/.venv}"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$ROOT/requirements.txt"
echo "Qwen3-ASR sidecar ready. Start with:"
echo "  QWEN3_ASR_MODEL=Qwen/Qwen3-ASR-0.6B $VENV/bin/uvicorn app:app --app-dir \"$ROOT\" --host 0.0.0.0 --port 8787"
