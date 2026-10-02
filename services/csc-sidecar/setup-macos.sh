#!/usr/bin/env bash
set -euo pipefail

# Bok CSC sidecar（MacBERT4CSC 中文受限纠错，端口 8792）
#
# 用途：给 zh 普通话转写做一次保守的等长错字纠正（方按→方案 / 赔尝→赔偿…）。
#       只接 lang=zh；粤语在服务端直接拒（模型碰粤语必漂移，见 app.py docstring）。
# 端口：8792（只绑 127.0.0.1）。
# 依赖体积：torch（macOS arm64 wheel 自带 MPS 后端）约 2GB 磁盘 + transformers/
#       fastapi/uvicorn 数十 MB；模型权重 102M（首启从 HF 下载到 app-data 缓存）。
#       默认装带 MPS 的 torch（arm64 wheel 自带）——比 CPU 快（实测 p50 ~15ms）。

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="${CSC_VENV:-$ROOT/.venv}"
PY="${CSC_PYTHON:-python3.12}"
command -v "$PY" >/dev/null 2>&1 || PY=python3

"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$ROOT/requirements.txt"

echo "CSC sidecar ready (MacBERT4CSC, zh-only, :8792)."
echo "Quick check without downloading the model:"
echo "  CSC_DISABLE_LOAD=1 $VENV/bin/python $ROOT/selftest.py   # 只验守卫链纯函数 + health"
echo "Self-test with real model (reuses HF cache if HF_HOME points at one):"
echo "  $VENV/bin/python $ROOT/selftest.py"
echo "Start (127.0.0.1 only — call transcripts never leave the machine):"
echo "  $VENV/bin/uvicorn app:app --app-dir \"$ROOT\" --host 127.0.0.1 --port 8792"
