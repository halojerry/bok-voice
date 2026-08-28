#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-/Volumes/OS/opt/homebrew/bin/python3.12}"
"$PY" -m venv .venv312
. .venv312/bin/activate
pip install -q --upgrade pip
pip install -q -e packages/core -e packages/business-db -e packages/knowledge -e apps/agent -e apps/control-plane -r requirements-dev.txt
echo "OK: source .venv312/bin/activate"
