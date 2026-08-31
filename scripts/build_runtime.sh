#!/usr/bin/env bash
# build_runtime.sh — assemble the self-contained Python/Node/LiveKit runtime
# that gets bundled into the desktop app (desktop/runtime/).

# Goal: the packaged app runs on a clean machine with NO system Python/Node.
# To make the Python venv relocatable we prefer a "python-build-standalone"
# interpreter (copied in wholesale). If that download fails we fall back to a
# venv created from the CI Python — usable for dev/demo, not guaranteed to be
# relocatable, but code paths still resolve through the same runtime/ paths.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/desktop/runtime"
cd "$ROOT"

mkdir -p "$RUNTIME"

PY_STANDALONE="$RUNTIME/python"
VENV="$RUNTIME/.venv"

# --- Python ---------------------------------------------------------------
# Prefer a relocatable standalone CPython. This is the only way a bundled venv
# works on a machine without a system Python (no absolute /opt/hostedtoolcache).
if [ ! -x "$PY_STANDALONE/bin/python3" ]; then
  echo "==> [runtime] fetching python-build-standalone …"
  # Resolve the latest install_only cpython-3.12 asset for the current OS/arch
  # via the GitHub releases API (avoids hardcoding a build tag that may not exist).
  ARCH="$(uname -m)"            # aarch64 / x86_64
  # python-build-standalone uses "aarch64" for Apple Silicon's "arm64".
  if [ "$ARCH" = "arm64" ]; then ARCH="aarch64"; fi
  OS_NAME="unknown-linux-gnu"
  case "$(uname -s)" in
    Darwin) OS_NAME="apple-darwin" ;;
    Linux)  OS_NAME="unknown-linux-gnu" ;;
    MINGW64*|MSYS*) OS_NAME="pc-windows-msvc" ;;
  esac
  TARGET="${ARCH}-${OS_NAME}"
  export PB_TARGET="${TARGET}"
  URL="$(curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" 2>/dev/null \
    | python3 -c '
import json, os, sys
target = os.environ.get("PB_TARGET", "")
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for a in d.get("assets", []):
    u = a.get("browser_download_url", "")
    # Prefer non-stripped, non-debug install_only builds for the target arch/OS.
    if target in u and "cpython-3.12" in u and "install_only" in u and "stripped" not in u and "debug" not in u:
        print(u); break
' || true)"
  echo "    resolved: ${URL:-<none>}"
  if [ -n "$URL" ] && curl -fsSL "$URL" -o "$RUNTIME/python.tar.gz"; then
    mkdir -p "$PY_STANDALONE"
    tar -xzf "$RUNTIME/python.tar.gz" -C "$PY_STANDALONE" --strip-components=1
    python3 -c "import os,sys; os.remove(sys.argv[1]) if os.path.exists(sys.argv[1]) else None" "$RUNTIME/python.tar.gz" 2>/dev/null || true
    echo "    standalone python ready: $PY_STANDALONE/bin/python3"
  else
    echo "    standalone download failed — falling back to system python venv (dev path)"
    PY_STANDALONE=""
  fi
fi

BASE_PY="$(command -v python3)"
if [ -x "$PY_STANDALONE/bin/python3" ]; then
  BASE_PY="$PY_STANDALONE/bin/python3"
fi

# The project requires Python >= 3.11; refuse to build a runtime on an older
# interpreter so the bundled venv is guaranteed compatible.
if ! "$BASE_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "ERROR: need Python >= 3.11, got $($BASE_PY --version 2>&1)" >&2
  exit 1
fi

echo "==> [runtime] creating relocatable venv ($BASE_PY) …"
if [ ! -d "$VENV" ]; then
  "$BASE_PY" -m venv "$VENV"
fi
VENV_PY="$VENV/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$VENV/Scripts/python.exe"

"$VENV_PY" -m pip install --upgrade pip
echo "==> [runtime] installing control-plane + sidecar deps …"
"$VENV_PY" -m pip install --no-cache-dir \
  -e packages/core -e packages/business-db -e packages/knowledge -e packages/observability \
  -e apps/control-plane -e "apps/agent[livekit]"
# Sidecar deps (ASR/TTS use fastapi+uvicorn+numpy etc. which are already pulled,
# but install their requirements to be safe for the platforms that need them).
for name in qwen3-asr-sidecar qwen3-tts-sidecar; do
  if [ -f "services/$name/requirements.txt" ]; then
    "$VENV_PY" -m pip install --no-cache-dir -r "services/$name/requirements.txt" || \
      echo "    (warn) $name requirements install had issues; continuing"
  fi
done
# Pin transformers last so a single coherent version wins (ASR/Qwen-TTS both
# import fine across 4.57.3/4.57.6; a deterministic pin silences the resolver
# conflict and keeps the bundled venv reproducible).
"$VENV_PY" -m pip install --no-cache-dir "transformers==4.57.6" || \
  echo "    (warn) transformers pin had issues; continuing"

# --- Node (B-line worker) --------------------------------------------------
echo "==> [runtime] installing realtime-translation node deps …"
if [ -n "${npm_config_user_agent:-}" ] || command -v npm >/dev/null 2>&1; then
  # Install into the realtime-translation dir, then mirror node_modules into
  # the runtime so the packaged worker resolves deps from a known path.
  (cd services/realtime-translation && npm install --no-audit --no-fund >/dev/null 2>&1 || true)
  python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$RUNTIME/bline-node_modules" 2>/dev/null || true
  cp -R services/realtime-translation/node_modules "$RUNTIME/bline-node_modules" 2>/dev/null \
    || echo "    (warn) could not copy node_modules"
else
  echo "    npm missing — node_modules will be resolved from repo (dev path)"
fi

# --- LiveKit server binary (compiled from source; no official macOS asset) ---
bash "$ROOT/scripts/build_livekit.sh"

echo "==> [runtime] done. runtime/ ="
du -sh "$RUNTIME" 2>/dev/null || true
