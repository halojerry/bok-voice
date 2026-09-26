#!/usr/bin/env bash
# Bok CUDA 部署包 —— 回滚脚本（bootstrap.sh + systemd 装载的对称卸载）。
# 用法:
#   ./uninstall.sh [--home DIR] [--purge-models] [--purge-env] [--keep-units]
#
# 缺省动作（保守回滚）:
#   1. systemctl disable --now 三单元 + 删 /etc/systemd/system 单元 + daemon-reload
#   2. 删安装目录里的 venv / units / bin（含渲染产物）
#   3. **保留**模型目录与 /etc/bok-cuda/bok-cuda.env——模型重下几十 G、env 里有
#      凭据，误删代价高；确要清掉用 --purge-models / --purge-env 显式声明。
set -uo pipefail

BOK_CUDA_HOME="${BOK_CUDA_HOME:-$HOME/bok-cuda}"
PURGE_MODELS=0; PURGE_ENV=0; KEEP_UNITS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) BOK_CUDA_HOME="${2:-}"; shift 2;;
    --purge-models) PURGE_MODELS=1; shift;;
    --purge-env) PURGE_ENV=1; shift;;
    --keep-units) KEEP_UNITS=1; shift;;   # 只停服务不删单元文件（临时下线用）
    -h|--help) sed -n '2,12p' "$0"; exit 0;;
    *) echo "未知参数: $1" >&2; exit 2;;
  esac
done
BOK_CUDA_HOME="${BOK_CUDA_HOME%/}"

UNITS=(sglang-a.service sglang-judge.service bok-node.service)
SYS_DIR="/etc/systemd/system"

echo "[uninstall] 1/3 停止并卸载 systemd 单元..."
for u in "${UNITS[@]}"; do
  if systemctl list-unit-files "$u" --no-legend 2>/dev/null | grep -q .; then
    systemctl disable --now "$u" 2>/dev/null || true
    if (( KEEP_UNITS == 0 )) && [[ -f "$SYS_DIR/$u" ]]; then
      rm -f "$SYS_DIR/$u"
      echo "  removed $SYS_DIR/$u"
    fi
  else
    echo "  skip ${u}（未安装）"
  fi
done
systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed 2>/dev/null || true
# 通知 systemd 单元文件已消失（避免残留 cached 状态）。
systemctl daemon-reload >/dev/null 2>&1 || true

echo "[uninstall] 2/3 清安装目录（${BOK_CUDA_HOME}）：venv / units / bin"
rm -rf "$BOK_CUDA_HOME/venv" "$BOK_CUDA_HOME/units" "$BOK_CUDA_HOME/bin"
rmdir "$BOK_CUDA_HOME" 2>/dev/null || true   # 非空（还有 models/env 渲染件）时留目录

if (( PURGE_MODELS == 1 )); then
  echo "[uninstall] --purge-models：删模型目录（重下要几十 G，确认是你要的）"
  rm -rf "$BOK_CUDA_HOME/models"
else
  echo "[uninstall] 模型目录保留：$BOK_CUDA_HOME/models（--purge-models 显式删除）"
fi

echo "[uninstall] 3/3 env 文件"
if (( PURGE_ENV == 1 )); then
  rm -f /etc/bok-cuda/bok-cuda.env
  rmdir /etc/bok-cuda 2>/dev/null || true
  echo "  removed /etc/bok-cuda/bok-cuda.env"
else
  echo "  保留 /etc/bok-cuda/bok-cuda.env（内含凭据；--purge-env 显式删除）"
fi

# 不碰的清单（故意列出防误删）：BOK_REPO_ROOT 指向的仓库检出、~/.bok/node-state.json
# （license token 状态文件——重装同机注册会复用 node_id，留着无害）、系统 driver/CUDA。
echo "[uninstall] OK：回滚完成。仓库检出与 ~/.bok/node-state.json 未动（重装可复用）。"
