#!/usr/bin/env bash
# build_release.sh — CI package pipeline (Linux/mac/Windows runners).
# Builds the web static export, runs tests (unless SKIP_TESTS=1, used by the
# release workflow because ci.yml already ran them on the same commit), derives
# the icon set, then emits a `models.sha256.json` release manifest.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> [1/5] build web (Next export)"
pushd apps/web >/dev/null
npm ci
npm run build
popd >/dev/null

if [ "${SKIP_TESTS:-0}" = "1" ]; then
  echo "==> [2/5] python tests — SKIPPED (SKIP_TESTS=1)"
  echo "==> [3/5] node tests — SKIPPED (SKIP_TESTS=1)"
else
  echo "==> [2/5] python tests"
  python -m pytest -q tests/

  echo "==> [3/5] node tests (realtime-translation)"
  pushd services/realtime-translation >/dev/null
  npm ci
  npm test
  popd >/dev/null
fi

echo "==> [4/5] derive desktop icons"
python3 desktop/scripts/gen_icon.py
if command -v npx >/dev/null 2>&1; then
  (cd desktop && npm ci && npx tauri icon src-tauri/icons/icon.png || true)
fi

echo "==> [5/5] model manifest"
python tools/bok.py manifest > "models.sha256.json"
echo "manifest rows: $(wc -c < models.sha256.json)"

echo "build_release done: $(pwd)/models.sha256.json"
