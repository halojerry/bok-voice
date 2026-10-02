#!/usr/bin/env bash
# Bok CUDA 部署包 —— bok-node.service 的 ExecStart 包装（bootstrap.sh 渲染到
# <BOK_CUDA_HOME>/bin/）。职责只有一件：把 env 文件里的凭据/形态键翻译成
# tools/node_agent.py 的 argv——凭据不进单元文件、不进 ps 可见的静态文本。
# 本包装零密钥字面量；全部值来自 /etc/bok-cuda/bok-cuda.env（systemd 注入）。
set -euo pipefail

BOK_CUDA_HOME="${BOK_CUDA_HOME:-__BOK_CUDA_HOME__}"
REPO_ROOT="${BOK_REPO_ROOT:-$BOK_CUDA_HOME/repo}"
AGENT="$REPO_ROOT/tools/node_agent.py"

[[ -n "${BOK_CP_URL:-}" ]] || { echo "[bok-node] FAIL: env 文件缺 BOK_CP_URL（云 CP 基址）" >&2; exit 2; }
[[ -f "$AGENT" ]] || {
  echo "[bok-node] FAIL: 找不到 ${AGENT}。" >&2
  echo "  heartbeat-only 模式也需要 node_agent.py 本体——把仓库检出/解包到 \${BOK_REPO_ROOT}（当前=${REPO_ROOT}），或在 env 文件改 BOK_REPO_ROOT 指向已有检出。" >&2
  exit 2
}

args=(--cp-url "$BOK_CP_URL")
if [[ -n "${BOK_LICENSE_KEY:-}" ]]; then
  args+=(--license-key "$BOK_LICENSE_KEY")
elif [[ -n "${BOK_NODE_TOKEN:-}" ]]; then
  args+=(--node-token "$BOK_NODE_TOKEN")
else
  echo "[bok-node] FAIL: env 文件缺 BOK_LICENSE_KEY / BOK_NODE_TOKEN（二选一）" >&2
  exit 2
fi
[[ -n "${BOK_NODE_NAME:-}" ]] && args+=(--name "$BOK_NODE_NAME")

# 形态：0（缺省）=heartbeat-only；1=全栈（cmd_up 拉 CP/LiveKit/worker 全家，⚠未实测）。
if [[ "${BOK_NODE_FULL_STACK:-0}" == "1" ]]; then
  [[ -n "${BOK_UI_DIR:-}" ]] && args+=(--ui-dir "$BOK_UI_DIR")
  [[ -n "${BOK_UI_CP_URL:-}" ]] && args+=(--ui-cp-url "$BOK_UI_CP_URL")
  [[ -n "${BOK_LIVEKIT_URL:-}" ]] && args+=(--livekit-url "$BOK_LIVEKIT_URL")
else
  args+=(--heartbeat-only)
fi

PY="${BOK_NODE_PYTHON:-python3}"
exec "$PY" "$AGENT" "${args[@]}"
