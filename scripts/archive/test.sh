#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
. .venv312/bin/activate
python -m pytest -q
