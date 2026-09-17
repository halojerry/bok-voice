#!/usr/bin/env bash
# publish_node_pkg.sh — 操作员侧：把发版工件推到云 CP 工件卷（/app/downloads）。
#
# 推送内容（BOK_NODE_ARTIFACTS_DIR 布局，节点侧经 /api/nodes/downloads/ 下载）：
#   pkg/<version>/bok-node-<version>.tar.gz{,.sha256}     代码包（build_node_pkg.sh 产物）
#   pkg/latest/bok-node-latest.tar.gz{,.sha256}           装机引导别名（同版本副本）
#   runtime/<version>/runtime-<os>-<arch>-<version>.tar.gz{,.sha256}
#   bootstrap/latest/{bootstrap-node.sh,install-node.sh,install-node.ps1}  装机入口脚本
#
# 用法（云主机、deploy/cloud 目录内，需 .env）：
#   ./publish_node_pkg.sh <version> [--artifacts DIR]      # 缺省 release-artifacts/
# 前置：先在打包机跑 build_node_pkg.sh / build_runtime_pkg.sh，把产物拿来本机。
set -euo pipefail

cd "$(dirname "$0")"
VERSION="${1:-}"
[ -n "$VERSION" ] || { echo "usage: publish_node_pkg.sh <version> [--artifacts DIR]" >&2; exit 2; }
shift || true
ARTIFACTS="release-artifacts"
if [ "${1:-}" = "--artifacts" ]; then ARTIFACTS="${2:?--artifacts 缺值}"; fi

COMPOSE="docker compose"
$COMPOSE ps >/dev/null 2>&1 || { echo "docker compose 不可用或不在 deploy/cloud 目录" >&2; exit 1; }

push() { # push <src> <dest-in-volume>
  local src="$1" dst="$2"
  [ -f "$src" ] || { echo "缺工件: $src" >&2; exit 1; }
  echo "  -> $dst"
  $COMPOSE exec -T --user root cp mkdir -p "/app/downloads/$(dirname "$dst")"
  $COMPOSE cp "$src" "cp:/app/downloads/$dst"
}

echo "==> [publish] $VERSION <- $ARTIFACTS"
# 代码包命名两态兼容：CI 产 <version>（tag 剥 v），手工产 v<version>——任一在位即可。
NODE_TGZ="$(ls "$ARTIFACTS/bok-node-$VERSION.tar.gz" "$ARTIFACTS/bok-node-v$VERSION.tar.gz" 2>/dev/null | head -1 || true)"
[ -n "$NODE_TGZ" ] || { echo "缺工件: bok-node-$VERSION.tar.gz（或 bok-node-v$VERSION.tar.gz）" >&2; exit 1; }
NODE_SHA="$NODE_TGZ.sha256"
push "$NODE_TGZ"        "pkg/$VERSION/$(basename "$NODE_TGZ")"
push "$NODE_SHA"        "pkg/$VERSION/$(basename "$NODE_SHA")"
# latest 别名：装机引导固定拉 latest，升级走 commands 通道按版本走。
push "$NODE_TGZ"        "pkg/latest/bok-node-latest.tar.gz"
push "$NODE_SHA"        "pkg/latest/bok-node-latest.tar.gz.sha256"
for rt in "$ARTIFACTS"/runtime-*-"$VERSION".tar.gz; do
  [ -e "$rt" ] || { echo "  (无 runtime 工件，跳过——节点装机将缺运行时!)"; break; }
  rt_base="$(basename "$rt")"
  push "$rt"                            "runtime/$VERSION/$rt_base"
  push "$rt.sha256"                     "runtime/$VERSION/$rt_base.sha256"
done
# runtime latest 别名（bootstrap 固定拉 runtime/latest/runtime-latest.tar.gz）。
if ls "$ARTIFACTS"/runtime-*-"$VERSION".tar.gz >/dev/null 2>&1; then
  FIRST_RT="$(ls "$ARTIFACTS"/runtime-*-"$VERSION".tar.gz | head -1)"
  push "$FIRST_RT"        "runtime/latest/runtime-latest.tar.gz"
  push "$FIRST_RT.sha256" "runtime/latest/runtime-latest.tar.gz.sha256"
fi
# 装机入口脚本（latest 恒指当前仓版本；脚本本身无密钥，鉴权在下载端点）。
# 本脚本位于 deploy/cloud/，repo 根在两级之上（实跑踩坑：上跳一层落到
# deploy/scripts → 缺文件退出，pkg/runtime 已进卷但 bootstrap 缺失，2026-09-18）。
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
push "$ROOT/scripts/bootstrap-node.sh" "bootstrap/latest/bootstrap-node.sh"
push "$ROOT/scripts/install-node.sh"   "bootstrap/latest/install-node.sh"
push "$ROOT/scripts/install-node.ps1"  "bootstrap/latest/install-node.ps1"

echo "==> [publish] done. 验收:"
$COMPOSE exec -T --user root cp find /app/downloads -type f | sort
echo "    节点侧自检（任一节点机）: curl -fsSL -H 'Authorization: Bearer bokn_…' \$
      https://<云域名>/api/nodes/downloads/pkg/$VERSION/bok-node-$VERSION.tar.gz -o /dev/null -w '%{http_code}\\n'"
