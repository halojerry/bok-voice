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

# Use the standalone CPython directly as the runtime (no venv). A venv created
# from it records an absolute `pyvenv.cfg home` that breaks when the bundle is
# moved/installed on another machine. The standalone interpreter is fully
# relocatable (stdlib lives in ./lib/python3.12) and packages install into its
# own site-packages. This is the reliable self-contained runtime.
RUNTIME_PY="$PY_STANDALONE/bin/python3"

# --- Python ---------------------------------------------------------------
# Prefer a relocatable standalone CPython. This is the only way a bundled venv
# works on a machine without a system Python (no absolute /opt/hostedtoolcache).
if [ ! -x "$PY_STANDALONE/bin/python3" ]; then
  echo "==> [runtime] fetching python-build-standalone …"
  # Use a pinned, known-good install_only cpython-3.12 build. Dynamic GitHub
  # release-API resolution proved flaky in CI (resolved:<none>), so we hardcode
  # the per-platform asset URLs. These are verified reachable.
  ARCH="$(uname -m)"            # aarch64 / x86_64
  if [ "$ARCH" = "arm64" ]; then ARCH="aarch64"; fi
  case "$(uname -s)" in
    Darwin)
      URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260825/cpython-3.12.14%2B20260825-${ARCH}-apple-darwin-install_only.tar.gz"
      ;;
    Linux)
      URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260825/cpython-3.12.14%2B20260825-${ARCH}-unknown-linux-gnu-install_only.tar.gz"
      ;;
    MINGW64*|MSYS*)
      URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260825/cpython-3.12.14%2B20260825-x86_64-pc-windows-msvc-install_only.tar.gz"
      ;;
    *)
      echo "ERROR: unsupported OS for standalone python: $(uname -s)" >&2
      exit 1
      ;;
  esac
  echo "    resolved: ${URL:-<none>}"
  if [ -z "$URL" ] || ! curl -fsSL "$URL" -o "$RUNTIME/python.tar.gz"; then
    echo "ERROR: python-build-standalone download failed (no relocatable CPython); aborting." >&2
    exit 1
  fi
  mkdir -p "$PY_STANDALONE"
  tar -xzf "$RUNTIME/python.tar.gz" -C "$PY_STANDALONE" --strip-components=1
  python3 -c "import os,sys; os.remove(sys.argv[1]) if os.path.exists(sys.argv[1]) else None" "$RUNTIME/python.tar.gz" 2>/dev/null || true
  echo "    standalone python ready: $PY_STANDALONE/bin/python3"
fi

# A relocatable standalone CPython is REQUIRED. A venv created from the system
# framework Python (e.g. /Library/Frameworks/Python.framework) records absolute
# paths and breaks when the bundle is moved to another machine. So we must use
# the standalone interpreter, and KEEP it in the bundle (runtime/python).
BASE_PY="$(command -v python3)"
if [ -x "$PY_STANDALONE/bin/python3" ]; then
  BASE_PY="$PY_STANDALONE/bin/python3"
else
  echo "ERROR: no relocatable standalone python at $PY_STANDALONE/bin/python3" >&2
  exit 1
fi

# The project requires Python >= 3.11; refuse to build a runtime on an older
# interpreter so the bundled venv is guaranteed compatible.
if ! "$BASE_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "ERROR: need Python >= 3.11, got $($BASE_PY --version 2>&1)" >&2
  exit 1
fi

echo "==> [runtime] installing deps into standalone python ($RUNTIME_PY) …"
"$RUNTIME_PY" -m pip install --upgrade pip
echo "==> [runtime] installing control-plane + sidecar deps …"
"$RUNTIME_PY" -m pip install --no-cache-dir \
  -e packages/core -e packages/business-db -e packages/knowledge -e packages/observability \
  -e apps/control-plane -e "apps/agent[livekit]"
# Sidecar deps (ASR/TTS use fastapi+uvicorn+numpy etc. which are already pulled,
# but install their requirements to be safe for the platforms that need them).
for name in qwen3-asr-sidecar qwen3-tts-sidecar; do
  if [ -f "services/$name/requirements.txt" ]; then
    "$RUNTIME_PY" -m pip install --no-cache-dir -r "services/$name/requirements.txt" || \
      echo "    (warn) $name requirements install had issues; continuing"
  fi
done
# Pin transformers last so a single coherent version wins (ASR/Qwen-TTS both
# import fine across 4.57.3/4.57.6; a deterministic pin silences the resolver
# conflict and keeps the bundled venv reproducible).
"$RUNTIME_PY" -m pip install --no-cache-dir "transformers==4.57.6" || \
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
