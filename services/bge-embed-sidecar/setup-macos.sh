#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="${BGE_EMBED_VENV:-$ROOT/.venv}"
PY="${BGE_EMBED_PYTHON:-python3.12}"
command -v "$PY" >/dev/null 2>&1 || PY=python3

"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$ROOT/requirements.txt"
echo "BGE embed sidecar ready. Start with (127.0.0.1 only — data policy):"
echo "  $VENV/bin/uvicorn app:app --app-dir \"$ROOT\" --host 127.0.0.1 --port 8789"
