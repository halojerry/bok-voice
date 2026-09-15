#!/usr/bin/env bash
# build_node_agent.sh — PyInstaller 打包 tools/node_agent.py 为单文件二进制。
#
# 产物：./dist/node-agent（dist/、build/ 均已 gitignore，构建产物不入库）。
# 构建 venv：./.venv-pyinstaller（与运行时 .venv312 隔离）。
# 用法：scripts/build_node_agent.sh [--clean]（--clean 透传给 pyinstaller，
#       清其缓存后重建，用于怀疑缓存污染时）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-/Volumes/OS/opt/homebrew/bin/python3.12}"
VENV="$ROOT/.venv-pyinstaller"

# --- 1) 独立构建 venv（不污染 .venv312） ---------------------------------
# node_agent.py 的模块级 import 面是纯 stdlib（argparse/json/sys/threading/
# time/urllib.request/dataclasses/pathlib），唯一非 stdlib 是懒加载的
# `import bok`（spec 已 exclude）—— 因此唯一要装的就是 pyinstaller 本身。
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> [node-agent] creating build venv at $VENV (python: $PY)"
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q "pyinstaller>=6.10,<7"

# --- 2) 打包 ---------------------------------------------------------------
EXTRA_ARGS=()
if [ "${1:-}" = "--clean" ]; then
  EXTRA_ARGS+=(--clean)
fi
echo "==> [node-agent] running pyinstaller (spec: scripts/node_agent.spec)"
"$VENV/bin/pyinstaller" scripts/node_agent.spec \
  --noconfirm --distpath dist --workpath build \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}

# --- 3) 验证：--help 退出 0 且输出含用法 ------------------------------------
# 命令替换的退出码即二进制的退出码；set -e 保证非 0 直接终止构建。
echo "==> [node-agent] verifying dist/node-agent --help"
HELP_OUT="$(./dist/node-agent --help)"
echo "$HELP_OUT"
printf '%s\n' "$HELP_OUT" | grep -q "usage" || {
  echo "ERROR: --help output missing usage text" >&2
  exit 1
}

# --- 4) 产物报告 -------------------------------------------------------------
echo "==> [node-agent] artifact:"
ls -lh dist/node-agent
echo "==> [node-agent] sha256:"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum dist/node-agent
else
  shasum -a 256 dist/node-agent
fi

# --- 5) 清理中间物 -----------------------------------------------------------
# build/ 是 PyInstaller 中间物，验证通过后即清；dist/ 保留在本地不入库
# （dist/ 与 build/ 均在 .gitignore）。
rm -rf build
echo "==> [node-agent] done (build/ cleaned, dist/node-agent kept locally)"
