#!/usr/bin/env bash
# 节点一键装机引导（thin-node SaaS 客户 GPU 节点侧）——从云 CP 自举，零 GitHub。
#
# 本脚本由云 CP 工件卷分发（publish_node_pkg.sh 推到 bootstrap/latest/），客户
# 链路只依赖云域名：装机命令由云侧 install.sh / runbook 生成，形如：
#
#   curl -fsSL -H "Authorization: Bearer bokn_<key>" \
#     https://<云域名>/api/nodes/downloads/bootstrap/latest/bootstrap-node.sh | bash -s -- \
#     --cp-url https://<云域名> \
#     --license-key bokn_<key> \
#     --livekit-url ws://<本机内网IP>:7880
#
# 干什么：拉代码包+sha256 校验 → 解到目标目录（可选拉预构建运行时包）→ 移交
#        scripts/install-node.sh（venv/模型下载/license 注册/心跳探活/doctor）。
# 出网要求：仅需 443 出站（云 CP 域名 + 模型下载 HF，可配 HF_ENDPOINT 镜像）；
# 无任何入站要求；仓库上游（GitHub）对客户不可见。
set -euo pipefail

die() { printf '\033[1;31m[node-bootstrap] 失败:\033[0m %s\n' "$*" >&2; exit 1; }
say() { printf '\033[1;32m[node-bootstrap]\033[0m %s\n' "$*"; }

TARGET="${BOK_NODE_TARGET:-$HOME/bok-voice}"
CP_URL=""
CRED=""          # license key 或 node token（二选一；都是 Bearer 下载凭据）
WITH_RUNTIME=1
FORWARD=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2;;
    --cp-url) CP_URL="${2:-}"; shift 2;;
    --license-key) CRED="${2:-}"; FORWARD+=("--license-key" "$2"); shift 2;;
    --node-token) CRED="${2:-}"; FORWARD+=("--node-token" "$2"); shift 2;;
    --livekit-url) FORWARD+=("--livekit-url" "$2"); shift 2;;
    --skip-models) FORWARD+=("--skip-models"); shift;;
    --no-runtime) WITH_RUNTIME=0; shift;;
    -h|--help) sed -n '2,22p' "$0"; exit 0;;
    *) die "未知参数: $1";;
  esac
done
[[ -n "$CP_URL" ]] || die "--cp-url 必填"
[[ -n "$CRED" ]] || die "--license-key 或 --node-token 必填（装机凭据=下载凭据）"

BASE="${CP_URL%/}/api/nodes/downloads"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch() { # fetch <path> <dest-file>
  curl -fsSL -H "Authorization: Bearer $CRED" "$BASE/$1" -o "$2" \
    || die "下载失败: $BASE/$1（凭据无效/吊销/配额满或网络不通）"
}

# ---- 前置检查（报告性，不阻断——install-node.sh 会再细查）----
if command -v nvidia-smi >/dev/null 2>&1; then
  say "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1)"
else
  say "未检出 NVIDIA GPU——CPU 档继续（CUDA 节点请先装驱动）"
fi
say "磁盘余量: $(df -h "$HOME" | tail -1 | awk '{print $4}')（模型权重 ~30-60GB，不足先扩）"

# ---- 代码包：latest 别名 + sha256 校验 ----
say "拉取代码包 pkg/latest …"
fetch "pkg/latest/bok-node-latest.tar.gz" "$TMP/pkg.tar.gz"
fetch "pkg/latest/bok-node-latest.tar.gz.sha256" "$TMP/pkg.sha256"
EXPECTED="$(cut -d' ' -f1 < "$TMP/pkg.sha256" | tr 'A-Z' 'a-z')"
if command -v sha256sum >/dev/null 2>&1; then ACTUAL="$(sha256sum "$TMP/pkg.tar.gz" | cut -d' ' -f1)"
else ACTUAL="$(shasum -a 256 "$TMP/pkg.tar.gz" | cut -d' ' -f1)"; fi
[[ "$ACTUAL" == "$EXPECTED" ]] || die "sha256 校验失败（expected $EXPECTED, got $ACTUAL）"

STAGE="$TMP/x"
mkdir -p "$STAGE" "$TARGET"
tar -xzf "$TMP/pkg.tar.gz" -C "$STAGE"
# 包内顶层目录归一（bok-node-<version>/ 前缀 或 打包根都接受）。
SRC="$STAGE"
FIRST="$(ls -A "$STAGE" | head -1)"
if [ -d "$STAGE/$FIRST" ]; then SRC="$STAGE/$FIRST"; fi
[ -f "$SRC/tools/node_agent.py" ] || die "代码包布局异常（缺 tools/node_agent.py）"
say "代码包 -> $TARGET（覆盖式更新，runtime/ 与本地数据保留）"
cp -R "$SRC/." "$TARGET/"

# ---- 预构建运行时包（可选；per-OS 别名优先，绝不跨平台误装）----
# 别名由 publish_node_pkg.sh 按工件名落卷：runtime/latest-<os>-<arch>/。
# 通用 runtime/latest 恒为 win 优先（存量客户机队契约），仅未知平台兜底——
# 已识别平台拿不到本平台包时宁可不装（架构不符的运行时比没有更糟）。
if [[ $WITH_RUNTIME -eq 1 ]]; then
  RT_ALIAS=""
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)  RT_ALIAS="latest-mac-arm64" ;;
    Darwin-x86_64) RT_ALIAS="latest-mac-x86_64" ;;
    Linux-x86_64)  RT_ALIAS="latest-linux-x86_64" ;;
    Linux-aarch64) RT_ALIAS="latest-linux-aarch64" ;;
    MINGW*|MSYS*)  RT_ALIAS="latest-win" ;;
  esac
  fetched=""
  if [[ -n "$RT_ALIAS" ]]; then
    if curl -fsSL -H "Authorization: Bearer $CRED" \
        "$BASE/runtime/$RT_ALIAS/runtime-latest.tar.gz" -o "$TMP/rt.tar.gz"; then
      fetched="$RT_ALIAS"
    else
      say "本平台运行时包 runtime/$RT_ALIAS 未发布——跳过（不落其他平台包，以免架构不符）；操作员 publish 后重跑"
    fi
  elif curl -fsSL -H "Authorization: Bearer $CRED" \
      "$BASE/runtime/latest/runtime-latest.tar.gz" -o "$TMP/rt.tar.gz"; then
    fetched="latest"
  else
    say "运行时包未发布（runtime/latest 404）——需要全栈时由操作员 publish 后重跑"
  fi
  if [[ -n "$fetched" ]]; then
    say "拉取运行时包 runtime/$fetched …"
    mkdir -p "$TARGET/runtime"
    tar -xzf "$TMP/rt.tar.gz" -C "$TARGET/runtime"
  fi
fi

# ---- 转发给正式安装器（venv/模型/license 注册/心跳探活/doctor）----
say "进入 install-node.sh（参数：${FORWARD[*]:-无}）"
exec bash "$TARGET/scripts/install-node.sh" --repo-root "$TARGET" --cp-url "$CP_URL" \
  "${FORWARD[@]+"${FORWARD[@]}"}"
