#!/usr/bin/env bash
# build_runtime.sh — assemble the self-contained runtime that ships in the app.
#
# Layout produced:
#   desktop/runtime/python/            standalone CPython + platform deps
#   desktop/runtime/llama/             Windows: llama-server.exe + cudart DLLs
#   desktop/runtime/bline-node_modules/  B-line worker deps (ws)
#   desktop/src-tauri/binaries/        externalBin: livekit-server, node
#                                      (<name>-<target-triple>[.exe])
#
# The staging dir is wiped first so cached/stale layouts (e.g. an old .venv)
# can never leak into a release bundle.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/desktop/runtime"
BINARIES="$ROOT/desktop/src-tauri/binaries"
cd "$ROOT"

echo "==> [runtime] cleaning staging dirs (avoid cache pollution)"
rm -rf "$RUNTIME"
mkdir -p "$RUNTIME"
rm -rf "$BINARIES"
mkdir -p "$BINARIES"

# --- Detect platform -------------------------------------------------------
case "$(uname -s)" in
  MINGW*|MSYS*)
    OS=win
    ARCH=x86_64
    TRIPLE=x86_64-pc-windows-msvc
    ;;
  Darwin)
    OS=mac
    ARCH="$(uname -m)"
    if [ "$ARCH" = "arm64" ]; then TRIPLE=aarch64-apple-darwin; else TRIPLE=x86_64-apple-darwin; fi
    ;;
  Linux)
    OS=linux
    ARCH="$(uname -m)"
    if [ "$ARCH" = "aarch64" ]; then TRIPLE=aarch64-unknown-linux-gnu; else TRIPLE=x86_64-unknown-linux-gnu; fi
    ;;
  *)
    echo "ERROR: unsupported OS: $(uname -s)" >&2
    exit 1
    ;;
esac
echo "==> [runtime] platform: $OS / $ARCH / $TRIPLE"

# --- Python standalone -----------------------------------------------------
PY_STANDALONE="$RUNTIME/python"
if [ "$OS" = "win" ]; then
  STD_PY="$PY_STANDALONE/python.exe"
else
  STD_PY="$PY_STANDALONE/bin/python3"
fi
RUNTIME_PY="$STD_PY"

if [ ! -x "$STD_PY" ]; then
  echo "==> [runtime] fetching python-build-standalone …"
  PY_VERSION="20260825"
  # python-build-standalone assets use `aarch64`, not `arm64`.
  PY_ARCH="$ARCH"
  [ "$PY_ARCH" = "arm64" ] && PY_ARCH=aarch64
  case "$OS" in
    mac) PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_VERSION}/cpython-3.12.14%2B${PY_VERSION}-${PY_ARCH}-apple-darwin-install_only.tar.gz" ;;
    linux) PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_VERSION}/cpython-3.12.14%2B${PY_VERSION}-${PY_ARCH}-unknown-linux-gnu-install_only.tar.gz" ;;
    win) PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_VERSION}/cpython-3.12.14%2B${PY_VERSION}-x86_64-pc-windows-msvc-install_only.tar.gz" ;;
  esac
  echo "    resolved: $PY_URL"
  curl -fsSL "$PY_URL" -o "$RUNTIME/python.tar.gz"
  mkdir -p "$PY_STANDALONE"
  tar -xzf "$RUNTIME/python.tar.gz" -C "$PY_STANDALONE" --strip-components=1
  rm -f "$RUNTIME/python.tar.gz"
  echo "    standalone python ready: $STD_PY"
fi

if [ ! -x "$STD_PY" ]; then
  echo "ERROR: no relocatable standalone python at $STD_PY" >&2
  exit 1
fi

echo "==> [runtime] installing deps into standalone python ($RUNTIME_PY) …"
"$RUNTIME_PY" -m pip install --upgrade pip
REQ="requirements-runtime-mac.txt"
EXTRA_INDEX=""
if [ "$OS" = "win" ]; then
  REQ="requirements-runtime-win.txt"
  EXTRA_INDEX="--extra-index-url https://download.pytorch.org/whl/cu124"
fi
# shellcheck disable=SC2086
"$RUNTIME_PY" -m pip install --no-cache-dir $EXTRA_INDEX -r "$REQ"
# Non-editable project installs (no CI-absolute .pth files; bundled source is
# also reachable via PYTHONPATH).
"$RUNTIME_PY" -m pip install --no-cache-dir \
  packages/core packages/business-db packages/knowledge packages/observability \
  apps/control-plane "apps/agent[livekit]"

# --- Node (B-line worker) --------------------------------------------------
NODE_VERSION="${NODE_VERSION:-22.23.2}"
echo "==> [runtime] bundling Node ${NODE_VERSION}"
if [ "$OS" = "win" ]; then
  NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-win-x64.zip"
  curl -fsSL "$NODE_URL" -o "$RUNTIME/node.zip"
  (cd "$RUNTIME" && unzip -q node.zip && rm -f node.zip)
  cp "$RUNTIME/node-v${NODE_VERSION}-win-x64/node.exe" "$BINARIES/node-${TRIPLE}.exe"
  rm -rf "$RUNTIME/node-v${NODE_VERSION}-win-x64"
else
  NODE_ARCH="$ARCH"
  NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-darwin-${NODE_ARCH}.tar.gz"
  if [ "$OS" = "linux" ]; then
    NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.gz"
  fi
  curl -fsSL "$NODE_URL" -o "$RUNTIME/node.tar.gz"
  tar -xzf "$RUNTIME/node.tar.gz" -C "$RUNTIME"
  rm -f "$RUNTIME/node.tar.gz"
  if [ "$OS" = "mac" ]; then
    cp "$RUNTIME/node-v${NODE_VERSION}-darwin-${NODE_ARCH}/bin/node" "$BINARIES/node-${TRIPLE}"
    rm -rf "$RUNTIME/node-v${NODE_VERSION}-darwin-${NODE_ARCH}"
  else
    cp "$RUNTIME/node-v${NODE_VERSION}-linux-${NODE_ARCH}/bin/node" "$BINARIES/node-${TRIPLE}"
    rm -rf "$RUNTIME/node-v${NODE_VERSION}-linux-${NODE_ARCH}"
  fi
  chmod +x "$BINARIES/node-${TRIPLE}"
fi

# --- LiveKit server (official binary where available) ---------------------
bash "$ROOT/scripts/build_livekit.sh" "$OS" "$TRIPLE" "$BINARIES"

# --- llama.cpp (Windows only) ----------------------------------------------
if [ "$OS" = "win" ]; then
  echo "==> [runtime] downloading llama.cpp CUDA 12.4 + cudart"
  LLAMA_DIR="$RUNTIME/llama"
  mkdir -p "$LLAMA_DIR"
  LLAMA_TAG="b10733"
  BASE="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}"
  curl -fsSL "${BASE}/llama-${LLAMA_TAG}-bin-win-cuda-12.4-x64.zip" -o "$RUNTIME/llama-cuda.zip"
  curl -fsSL "${BASE}/cudart-llama-bin-win-cuda-12.4-x64.zip" -o "$RUNTIME/llama-cudart.zip"
  (cd "$LLAMA_DIR" && unzip -qo "$RUNTIME/llama-cuda.zip" && unzip -qo "$RUNTIME/llama-cudart.zip")
  rm -f "$RUNTIME/llama-cuda.zip" "$RUNTIME/llama-cudart.zip"
  find "$LLAMA_DIR" -iname "llama-server.exe" -exec mv {} "$LLAMA_DIR/llama-server.exe" \;
  ls -la "$LLAMA_DIR" | head -20
fi

# --- B-line node_modules ---------------------------------------------------
echo "==> [runtime] installing realtime-translation node deps …"
if command -v npm >/dev/null 2>&1; then
  (cd services/realtime-translation && npm ci --no-audit --no-fund)
  cp -R services/realtime-translation/node_modules "$RUNTIME/bline-node_modules"
else
  echo "    npm missing — node_modules will be resolved from repo (dev path)"
fi

echo "==> [runtime] done. runtime/ ="
du -sh "$RUNTIME" 2>/dev/null || true
echo "==> [runtime] externalBin/ ="
ls -la "$BINARIES"
