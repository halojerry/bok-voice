#!/usr/bin/env bash
# 节点一键装机引导（thin-node SaaS 客户 GPU 节点侧）——设计为可 curl|bash。
#
# 干什么：前置检查 → clone 仓库（幂等，已有则复用/可选更新）→ 转发全部参数给
#        仓库内 scripts/install-node.sh（环境体检/venv/模型下载/license 注册/心跳探活）。
#
# 一条龙（客户机房照抄，三处尖括号必填）：
#   curl -fsSL https://raw.githubusercontent.com/halojerry/bok-voice/main/scripts/bootstrap-node.sh | bash -s -- \
#     --cp-url https://<云域名> \
#     --license-key bokn_<我们签发的key> \
#     --livekit-url ws://<本机内网IP>:7880
#
# 参数（除下述三个外全部透传给 install-node.sh，如 --skip-models/--dry-run）：
#   --target DIR     仓库落盘位置（默认 ~/bok-voice；已存在且含 install-node.sh 即复用）
#   --update         复用既有仓库时先 git pull --ff-only（升级节点；默认不动）
#   --repo URL       仓库地址（默认官方公开仓；镜像/私服可换）
#
# 出网要求：仅需 443 出站（GitHub clone + 云 CP + 模型下载）；无任何入站要求。
set -euo pipefail

TARGET="${BOK_NODE_TARGET:-$HOME/bok-voice}"
REPO="${BOK_NODE_REPO:-https://github.com/halojerry/bok-voice.git}"
UPDATE=0
FORWARD=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2;;
    --repo) REPO="${2:-}"; shift 2;;
    --update) UPDATE=1; shift;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) FORWARD+=("$1"); shift;;
  esac
done

say() { printf '\033[1;32m[node-bootstrap]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[node-bootstrap] 失败:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 前置检查（报告性，不阻断——install-node.sh 会再细查）----
command -v git >/dev/null || die "未找到 git（apt install git / yum install git）"
if command -v nvidia-smi >/dev/null; then
  say "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
  say "未检出 NVIDIA GPU——mac-mlx 档继续（CUDA 节点请先装驱动）"
fi
say "磁盘余量: $(df -h "$HOME" | tail -1 | awk '{print $4}')（模型权重 ~30-60GB，不足先扩）"

# ---- 仓库：clone 或复用（幂等）----
if [[ -f "$TARGET/scripts/install-node.sh" ]]; then
  say "复用既有仓库 $TARGET$([[ $UPDATE -eq 1 ]] && echo '（--update：先拉取）')"
  if [[ $UPDATE -eq 1 ]]; then
    git -C "$TARGET" pull --ff-only || die "git pull 失败（有本地改动？）——手动处理或删目录重装"
  fi
else
  say "clone $REPO -> ${TARGET}（--depth 1）"
  mkdir -p "$(dirname "$TARGET")"
  git clone --depth 1 "$REPO" "$TARGET" || die "clone 失败——检查出网/换 --repo 镜像地址"
fi

# ---- 转发给正式安装器（其内含 --dry-run/--skip-models/license 注册/心跳探活）----
say "进入 install-node.sh（参数：${FORWARD[*]:-无}）"
exec bash "$TARGET/scripts/install-node.sh" --repo-root "$TARGET" "${FORWARD[@]+"${FORWARD[@]}"}"
