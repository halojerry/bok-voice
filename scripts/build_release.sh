#!/usr/bin/env bash
# build_release.sh — CI package pipeline (Linux/mac/Windows runners).
# Builds the web static export, runs the full test matrix, derives the icon
# set, then emits a `models.sha256.json` release manifest from `bok.py manifest`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> [1/5] build web (Next export)"
pushd apps/web >/dev/null
npm install
npm run build
popd >/dev/null

echo "==> [2/5] python tests"
python -m pytest -q

echo "==> [3/5] node tests (realtime-translation)"
pushd services/realtime-translation >/dev/null
npm install
npm test
popd >/dev/null

echo "==> [4/5] derive desktop icons"
python3 desktop/scripts/gen_icon.py
if command -v npx >/dev/null 2>&1; then
  (cd desktop && npm install && npx tauri icon src-tauri/icons/icon.png || true)
fi

echo "==> [5/5] model manifest"
python tools/bok.py manifest > "models.sha256.json"
echo "manifest rows: $(wc -c < models.sha256.json)"

echo "build_release done: $(pwd)/models.sha256.json"
