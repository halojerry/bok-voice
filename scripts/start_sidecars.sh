#!/usr/bin/env bash
# Start the Qwen3-ASR (8787) and Qwen3-TTS (8788) sidecars on the host.
# Idempotent: skips services whose /health already answers.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$ROOT/data"
mkdir -p "$DATA_DIR"

ASR_PORT="${QWEN3_ASR_PORT:-8787}"
TTS_PORT="${QWEN3_TTS_PORT:-8788}"
LLM_PORT="${MLX_LLM_PORT:-1235}"
ASR_VENV_PY="$ROOT/services/qwen3-asr-sidecar/.venv/bin/python"
TTS_VENV_PY="$ROOT/services/qwen3-tts-sidecar/.venv/bin/python"
LLM_VENV_PY="$ROOT/services/llm-mlx/.venv/bin/python"
MODELS_HOME="${LMSTUDIO_MODELS_DIR:-$HOME/.lmstudio/models}"

ASR_MODEL="${QWEN3_ASR_MODEL:-$MODELS_HOME/aufklarer/Qwen3-ASR-1.7B-MLX-8bit}"
ASR_BACKEND="${QWEN3_ASR_BACKEND:-mlx}"
TTS_PRESET="${QWEN3_TTS_PRESET_MODEL:-$MODELS_HOME/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit}"
TTS_CLONE="${QWEN3_TTS_CLONE_MODEL:-$MODELS_HOME/mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit}"
TTS_BACKEND="${QWEN3_TTS_BACKEND:-mlx}"
LLM_MODEL="${MLX_LLM_MODEL:-$MODELS_HOME/huihui-ai/Huihui-Qwen3.5-9B-abliterated-mlx-4bit}"

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
  QWEN3_ASR_MODEL="$ASR_MODEL" \
  QWEN3_ASR_BACKEND="$ASR_BACKEND" \
  QWEN3_ASR_DEVICE="${QWEN3_ASR_DEVICE:-cpu}"

launch qwen3-tts-sidecar "$TTS_PORT" \
  "$DATA_DIR/sidecar-tts.pid" "$DATA_DIR/sidecar-tts.log" "$TTS_VENV_PY" \
  QWEN3_TTS_PRESET_MODEL="$TTS_PRESET" \
  QWEN3_TTS_CLONE_MODEL="$TTS_CLONE" \
  QWEN3_TTS_BACKEND="$TTS_BACKEND"

if [ ! -x "$LLM_VENV_PY" ]; then
  echo "[llm] missing $LLM_VENV_PY — run: python3 -m venv services/llm-mlx/.venv && services/llm-mlx/.venv/bin/pip install mlx-lm uvicorn fastapi" >&2
else
  launch_llm() {
    local port="$1" pidfile="$2" logfile="$3"
    if curl -sf -m 2 "http://127.0.0.1:$port/v1/models" >/dev/null 2>&1; then
      echo "[llm] mlx_lm already healthy on :$port"
      return 0
    fi
    echo "[llm] starting mlx_lm server on :$port ..."
    nohup env "$LLM_VENV_PY" -m mlx_lm server \
      --model "$LLM_MODEL" \
      --port "$port" \
      --chat-template-args '{"enable_thinking":false}' \
      --log-level WARNING \
      >>"$logfile" 2>&1 &
    echo $! > "$pidfile"
    echo "[llm] mlx_lm pid $(cat "$pidfile") log=$logfile"
  }
  launch_llm "$LLM_PORT" "$DATA_DIR/llm-mlx.pid" "$DATA_DIR/llm-mlx.log"
fi

echo "[sidecar] done. backends: asr=$ASR_BACKEND tts=$TTS_BACKEND llm=mlx_lm. Poll /health before running the smoke test."
