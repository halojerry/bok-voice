#!/usr/bin/env bash
# build_livekit.sh — produce a native `livekit-server` into desktop/runtime/.
#
# LiveKit publish no macOS release binary (only linux/windows tarballs), so a
# self-contained macOS build must compile the server from source with Go. On
# Windows/Linux we can usually reuse the official release asset, but building
# from source is the single reliable cross-platform path, so this script
# compiles for the current runner everywhere.
#
# Requires `go` on PATH (add `actions/setup-go` before calling this in CI).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/desktop/runtime"
LK_VERSION="${LIVEKIT_VERSION:-v1.13.6}"
LK_OUT="$RUNTIME/livekit-server"
LK_OUT_WIN="$RUNTIME/livekit-server.exe"

mkdir -p "$RUNTIME"

compile_from_source() {
  echo "==> [livekit] compiling livekit-server $LK_VERSION from source …"
  command -v go >/dev/null 2>&1 || { echo "    (warn) go not found on PATH; skipping source build" >&2; return 1; }
  echo "    go: $(go version)"

  SRC="${TMPDIR:-/tmp}/livekit-src-$LK_VERSION"
  python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$SRC" 2>/dev/null || true

  git clone --depth 1 --branch "$LK_VERSION" https://github.com/livekit/livekit.git "$SRC" || {
    echo "    (warn) git clone livekit failed" >&2; return 1;
  }

  (cd "$SRC" && go build -o "$LK_OUT" ./cmd/server) || {
    echo "    (warn) go build livekit failed" >&2; return 1;
  }
  chmod +x "$LK_OUT" 2>/dev/null || true
  python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$SRC" 2>/dev/null || true
  echo "    livekit-server: $LK_OUT ($(ls -lh "$LK_OUT" | awk '{print $5}'))"
}

if [ ! -x "$LK_OUT" ]; then
  compile_from_source || {
    echo "    (warn) source build unavailable/失败 — app will run B-line only"
  }
else
  echo "==> [livekit] livekit-server already present"
fi
