#!/usr/bin/env bash
# 薄节点部署草版（spec §11.2；P0=手工档，P2 正式化含离线包）。
# 用法: install-node.sh --cp-url URL --node-token TOK [--dry-run] [--skip-models]
set -euo pipefail

CP_URL=""; NODE_TOKEN=""; DRY_RUN=0; SKIP_MODELS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cp-url) CP_URL="$2"; shift 2;;
    --node-token) NODE_TOKEN="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --skip-models) SKIP_MODELS=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done
[[ -n "$CP_URL" && -n "$NODE_TOKEN" ]] || { echo "--cp-url/--node-token required"; exit 2; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
steps=()
step() { steps+=("$*"); echo "[plan] $*"; [[ $DRY_RUN -eq 1 ]] || bash -c "$*"; }

step "echo '[1/5] 环境体检: GPU/驱动/磁盘'"
if command -v nvidia-smi >/dev/null 2>&1; then step "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"; else echo "[plan] 无 NVIDIA GPU —— mac-mlx 档继续"; fi
step "df -h \"$HOME\" | tail -1"

step "echo '[2/5] Python venv + 依赖'"
step "\"$REPO_ROOT/scripts/bootstrap.sh\""

if [[ $SKIP_MODELS -eq 0 ]]; then
  step "echo '[3/5] 模型下载（幂等续传）'"
  step "\"$REPO_ROOT/.venv312/bin/python\" \"$REPO_ROOT/tools/bok.py\" download"
fi

step "echo '[4/5] 节点注册 + 心跳/UI 配置注入'"
step "\"$REPO_ROOT/.venv312/bin/python\" \"$REPO_ROOT/tools/node_agent.py\" --cp-url \"$CP_URL\" --node-token \"$NODE_TOKEN\" --ui-dir \"$REPO_ROOT/apps/web/out\" --heartbeat-only --interval 1 & sleep 3; kill %1 2>/dev/null || true"

step "echo '[5/5] doctor 终检'"
step "\"$REPO_ROOT/.venv312/bin/python\" \"$REPO_ROOT/tools/bok.py\" doctor || true"

echo "共 ${#steps[@]} 步。$([[ $DRY_RUN -eq 1 ]] && echo '(dry-run 未执行)' || echo '完成。')"
