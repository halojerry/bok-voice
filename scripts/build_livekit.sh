#!/usr/bin/env bash
# build_livekit.sh — produce the native livekit-server binary into the Tauri
# externalBin staging dir (src-tauri/binaries/<name>-<target-triple>).
#
# Windows/Linux: official release zip.
# macOS: no official binary is published, so prefer the Homebrew bottle and
# fall back to compiling from source with Go.
set -euo pipefail

OS="${1:-}"
TRIPLE="${2:-}"
BINARIES="${3:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LK_VERSION="${LIVEKIT_VERSION:-v1.13.6}"

if [ -z "$OS" ] || [ -z "$TRIPLE" ] || [ -z "$BINARIES" ]; then
  echo "usage: build_livekit.sh <mac|win|linux> <target-triple> <binaries-dir>" >&2
  exit 1
fi
mkdir -p "$BINARIES"

case "$OS" in
  win)
    OUT="$BINARIES/livekit-server-${TRIPLE}.exe"
    if [ ! -x "$OUT" ]; then
      echo "==> [livekit] downloading official Windows binary $LK_VERSION"
      curl -fsSL "https://github.com/livekit/livekit/releases/download/${LK_VERSION}/livekit_${LK_VERSION#v}_windows_amd64.zip" -o "$ROOT/desktop/runtime/lk.zip"
      (cd "$ROOT/desktop/runtime" && unzip -qo lk.zip && rm -f lk.zip)
      find "$ROOT/desktop/runtime" -iname "livekit-server.exe" -exec cp {} "$OUT" \;
      chmod +x "$OUT"
    fi
    echo "    livekit-server: $OUT ($(ls -lh "$OUT" | awk '{print $5}'))"
    ;;
  mac)
    OUT="$BINARIES/livekit-server-${TRIPLE}"
    if [ ! -x "$OUT" ]; then
      if command -v brew >/dev/null 2>&1 && brew install livekit >/dev/null 2>&1; then
        echo "==> [livekit] using Homebrew bottle $LK_VERSION"
        LK_BIN="$(command -v livekit-server)"
        cp "$LK_BIN" "$OUT"
      else
        echo "==> [livekit] Homebrew unavailable — compiling from source"
        SRC="${TMPDIR:-/tmp}/livekit-src-$LK_VERSION"
        rm -rf "$SRC"
        git clone --depth 1 --branch "$LK_VERSION" https://github.com/livekit/livekit.git "$SRC"
        (cd "$SRC" && go build -o "$OUT" ./cmd/server)
        rm -rf "$SRC"
      fi
      chmod +x "$OUT"
    fi
    echo "    livekit-server: $OUT ($(ls -lh "$OUT" | awk '{print $5}'))"
    ;;
  linux)
    OUT="$BINARIES/livekit-server-${TRIPLE}"
    if [ ! -x "$OUT" ]; then
      echo "==> [livekit] downloading official Linux binary $LK_VERSION"
      curl -fsSL "https://github.com/livekit/livekit/releases/download/${LK_VERSION}/livekit_${LK_VERSION#v}_linux_amd64.tar.gz" -o "$ROOT/desktop/runtime/lk.tar.gz"
      (cd "$ROOT/desktop/runtime" && tar -xzf lk.tar.gz && rm -f lk.tar.gz)
      find "$ROOT/desktop/runtime" -iname "livekit-server" -exec cp {} "$OUT" \;
      chmod +x "$OUT"
    fi
    echo "    livekit-server: $OUT ($(ls -lh "$OUT" | awk '{print $5}'))"
    ;;
  *)
    echo "ERROR: unsupported OS $OS" >&2
    exit 1
    ;;
esac
