#!/usr/bin/env bash
# verify_bundle.sh — hard gate before a release artifact is uploaded.
#
#   --staging               assert runtime staging + externalBin assets
#   --app <path.app>        assert the built macOS app bundle layout + size
#   --doctor                run bundled `bok.py doctor` with BOK_PACKAGED=1
#
# Any failure => non-zero exit; release.yml stops before upload.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/desktop/runtime"
BINARIES="$ROOT/desktop/src-tauri/binaries"
FAIL=0

note() { printf '  %s\n' "$*"; }
check() { # check <desc> <cmd...>
  local desc="$1"; shift
  if "$@"; then note "ok   $desc"; else note "FAIL $desc"; FAIL=1; fi
}

mode="${1:---staging}"

if [ "$mode" = "--staging" ]; then
  echo "==> verify staging (runtime + externalBin)"
  if [ -f "$RUNTIME/python/bin/python3" ] || [ -f "$RUNTIME/python/python.exe" ]; then
    note "ok   standalone python"
  else
    note "FAIL standalone python"; FAIL=1
  fi
  check "bline-node_modules" test -d "$RUNTIME/bline-node_modules"
  if compgen -G "$BINARIES/node-*" >/dev/null; then note "ok   node externalBin"; else note "FAIL node externalBin"; FAIL=1; fi
  if compgen -G "$BINARIES/livekit-server-*" >/dev/null; then note "ok   livekit externalBin"; else note "FAIL livekit externalBin"; FAIL=1; fi
  if [ "$(uname -s | cut -c1-6)" = "MINGW" ]; then
    check "llama-server.exe" test -f "$RUNTIME/llama/llama-server.exe"
    if compgen -G "$RUNTIME/llama/*.dll" >/dev/null; then note "ok   cudart dlls"; else note "FAIL cudart dlls"; FAIL=1; fi
  fi
fi

if [ "$mode" = "--app" ]; then
  APP="${2:?usage: verify_bundle.sh --app <path.app>}"
  echo "==> verify app bundle: $APP"
  check "app exists" test -d "$APP"
  RES="$APP/Contents/Resources"
  check "resources dir" test -d "$RES"
  PY=$(find "$RES" -path '*/runtime/python/bin/python3' -o -path '*/runtime/python/python.exe' 2>/dev/null | head -1)
  check "bundled python present" test -n "$PY"
  check "externalBin livekit-server" find "$RES" -maxdepth 2 -name 'livekit-server*' | grep -q .
  check "externalBin node" find "$RES" -maxdepth 2 -name 'node*' | grep -q .
  SIZE_MB=$(du -sm "$APP" | awk '{print $1}')
  note "app size: ${SIZE_MB}MB"
  if [ "$SIZE_MB" -gt 1330 ]; then note "FAIL size gate (>1330MB)"; FAIL=1; else note "ok   size gate <=1330MB"; fi
fi

if [ "$mode" = "--doctor" ]; then
  APP="${2:?usage: verify_bundle.sh --doctor <path.app>}"
  echo "==> run bundled doctor"
  RES="$APP/Contents/Resources"
  PY=$(find "$RES" -path '*/runtime/python/bin/python3' 2>/dev/null | head -1)
  CODE_ROOT=$(dirname "$(find "$RES" -path '*/tools/bok.py' 2>/dev/null | head -1)")
  check "code root found" test -n "$CODE_ROOT"
  check "bundle python found" test -n "$PY"
  if [ -n "$PY" ] && [ -n "$CODE_ROOT" ]; then
    BOK_PACKAGED=1 BOK_ROOT="$CODE_ROOT" BOK_RESOURCE_DIR="$RES" "$PY" "$CODE_ROOT/tools/bok.py" doctor || FAIL=1
  else
    FAIL=1
  fi
fi

if [ "$FAIL" -ne 0 ]; then
  echo "==> VERIFY BUNDLE FAILED"
  exit 1
fi
echo "==> VERIFY BUNDLE PASSED"
