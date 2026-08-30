#!/usr/bin/env bash
# Stop the Qwen3-ASR / Qwen3-TTS sidecars started by start_sidecars.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

for name in asr tts; do
  pidfile="$ROOT/data/sidecar-$name.pid"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      echo "[sidecar] stopped $name (pid $pid)"
    else
      echo "[sidecar] $name pid $pid not running"
    fi
    rm -f "$pidfile"
  fi
done
