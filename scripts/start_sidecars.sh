#!/usr/bin/env bash
# Start the Qwen3-ASR (8787) and Qwen3-TTS (8788) sidecars on the host.
# Idempotent: skips services whose /health already answers.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$ROOT/data"
mkdir -p "$DATA_DIR"

ASR_PORT="${QWEN3_ASR_PORT:-8787}"
TTS_PORT="${QWEN3_TTS_PORT:-8788}"
ASR_VENV_PY="$ROOT/services/qwen3-asr-sidecar/.venv/bin/python"
TTS_VENV_PY="$ROOT/services/qwen3-tts-sidecar/.venv/bin/python"

for p in "$ASR_VENV_PY" "$TTS_VENV_PY"; do
  if [ ! -x "$p" ]; then
    echo "[sidecar] missing $p — run setup-macos.sh first" >&2
    exit 1
  fi
done

healthy() {
  curl -sf -m 2 "http://127.0.0.1:$1/health" >/dev/null 2>&1
}

launch() {
  local name="$1" port="$2" pidfile="$3" logfile="$4" py="$5"
  shift 5
  if healthy "$port"; then
    echo "[sidecar] $name already healthy on :$port"
    return 0
  fi
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "[sidecar] $name already running (pid $(cat "$pidfile"))"
    return 0
  fi
  echo "[sidecar] starting $name on :$port ..."
  nohup env "$@" "$py" -m uvicorn app:app \
    --app-dir "$ROOT/services/$name" \
    --host 0.0.0.0 --port "$port" \
    >>"$logfile" 2>&1 &
  echo $! > "$pidfile"
  echo "[sidecar] $name pid $(cat "$pidfile")  log=$logfile"
}

launch qwen3-asr-sidecar "$ASR_PORT" \
  "$DATA_DIR/sidecar-asr.pid" "$DATA_DIR/sidecar-asr.log" "$ASR_VENV_PY" \
  QWEN3_ASR_MODEL="$ROOT/data/models/qwen3-asr-0.6b" \
  QWEN3_ASR_BACKEND=transformers

launch qwen3-tts-sidecar "$TTS_PORT" \
  "$DATA_DIR/sidecar-tts.pid" "$DATA_DIR/sidecar-tts.log" "$TTS_VENV_PY" \
  QWEN3_TTS_PRESET_MODEL="$ROOT/data/models/qwen3-tts-1.7b-customvoice" \
  QWEN3_TTS_CLONE_MODEL="$ROOT/data/models/qwen3-tts-1.7b-base"

echo "[sidecar] done. Model loading takes 1-5 minutes; poll /health before running the smoke test."
