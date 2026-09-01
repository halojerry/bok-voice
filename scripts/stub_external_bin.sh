#!/usr/bin/env bash
# stub_external_bin.sh — create placeholder externalBin files for the current
# target triple so `cargo test` / `cargo check` compile without the real
# runtime binaries. Release builds run build_runtime.sh first, which replaces
# these stubs with the actual livekit-server / node binaries.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/desktop/src-tauri/binaries"
mkdir -p "$BIN"

case "$(uname -s)" in
  MINGW*|MSYS*)
    T=x86_64-pc-windows-msvc
    EXE=.exe
    ;;
  Darwin)
    A="$(uname -m)"
    if [ "$A" = "arm64" ]; then T=aarch64-apple-darwin; else T=x86_64-apple-darwin; fi
    EXE=
    ;;
  *)
    A="$(uname -m)"
    if [ "$A" = "aarch64" ]; then T=aarch64-unknown-linux-gnu; else T=x86_64-unknown-linux-gnu; fi
    EXE=
    ;;
esac

for name in livekit-server node; do
  f="$BIN/${name}-${T}${EXE}"
  if [ ! -e "$f" ]; then
    touch "$f"
    echo "stub: $f"
  fi
done
