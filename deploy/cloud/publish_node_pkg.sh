#!/usr/bin/env bash
# publish_node_pkg.sh — 操作员侧：把发版工件推到云 CP 工件卷（/app/downloads）。
#
# 推送内容（BOK_NODE_ARTIFACTS_DIR 布局，节点侧经 /api/nodes/downloads/ 下载）：
#   pkg/<version>/bok-node-<version>.tar.gz{,.sha256}     代码包（build_node_pkg.sh 产物）
#   pkg/latest/bok-node-latest.tar.gz{,.sha256}           装机引导别名（同版本副本）
#   runtime/<version>/runtime-<os>-<arch>-<version>.tar.gz{,.sha256}
#   runtime/latest-<os>-<arch>/runtime-latest.tar.gz{,.sha256}
#       per-OS 装机别名（bootstrap-node.sh 按 uname 选）；
#       runtime/latest/ 通用别名恒 win 优先（存量客户机队=Windows 的契约）
#   bootstrap/latest/{bootstrap-node.sh,install-node.sh,install-node.ps1}  装机入口脚本
#
# 命名契约：工件卷内**版本恒为无 v 形态**（node_agent 升级 URL 与 VERSION 文件
# 同口径）；手工打 v 前缀命名的工件会被归一存储。版本目录/文件名两态输入都收。
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

# 工件卷版本恒无 v（v0.4.0 -> 0.4.0）：node_agent perform_update 同款归一。
CANON="${VERSION#v}"

COMPOSE="docker compose"
$COMPOSE ps >/dev/null 2>&1 || { echo "docker compose 不可用或不在 deploy/cloud 目录" >&2; exit 1; }

push() { # push <src> <dest-in-volume>
  local src="$1" dst="$2"
  [ -f "$src" ] || { echo "缺工件: $src" >&2; exit 1; }
  echo "  -> $dst"
  $COMPOSE exec -T --user root cp mkdir -p "/app/downloads/$(dirname "$dst")"
  $COMPOSE cp "$src" "cp:/app/downloads/$dst"
}

echo "==> [publish] $VERSION (卷内归一为 $CANON) <- $ARTIFACTS"
# 代码包命名两态兼容：CI 产 <version>（tag 剥 v），手工产 v<version>——任一在位即可。
NODE_TGZ="$(ls "$ARTIFACTS/bok-node-$CANON.tar.gz" "$ARTIFACTS/bok-node-v$CANON.tar.gz" 2>/dev/null | head -1 || true)"
[ -n "$NODE_TGZ" ] || { echo "缺工件: bok-node-$CANON.tar.gz（或 bok-node-v$CANON.tar.gz）" >&2; exit 1; }
NODE_SHA="$NODE_TGZ.sha256"
push "$NODE_TGZ" "pkg/$CANON/bok-node-$CANON.tar.gz"
push "$NODE_SHA" "pkg/$CANON/bok-node-$CANON.tar.gz.sha256"
# latest 别名：装机引导固定拉 latest，升级走 commands 通道按版本走。
push "$NODE_TGZ" "pkg/latest/bok-node-latest.tar.gz"
push "$NODE_SHA" "pkg/latest/bok-node-latest.tar.gz.sha256"
# 运行时包（多平台）：版本目录 + per-OS latest 别名（bootstrap 按 uname 拉自己
# 平台，绝不跨平台误装）；文件名同样归一为无 v。
rt_seen=0
RT_FILES="$(ls "$ARTIFACTS"/runtime-*-"$CANON".tar.gz "$ARTIFACTS"/runtime-*-v"$CANON".tar.gz 2>/dev/null | grep -v '\.sha256$' | sort -u || true)"
while IFS= read -r rt; do
  [ -n "$rt" ] || continue
  rt_seen=1
  # 后缀归一：先剥 -v<版本>，再剥 -<版本>（两态输入都收敛到 runtime-<os>-<arch>）
  rt_base="$(basename "$rt" .tar.gz)"
  rt_base="${rt_base%-v$CANON}"; rt_base="${rt_base%-$CANON}"
  push "$rt"        "runtime/$CANON/$rt_base-$CANON.tar.gz"
  push "$rt.sha256" "runtime/$CANON/$rt_base-$CANON.tar.gz.sha256"
  push "$rt"        "runtime/latest-$rt_base/runtime-latest.tar.gz"
  push "$rt.sha256" "runtime/latest-$rt_base/runtime-latest.tar.gz.sha256"
done <<< "$RT_FILES"
if [ "$rt_seen" -eq 0 ]; then
  echo "  (无 runtime 工件，跳过——节点装机将缺运行时!)"
fi
# runtime 通用 latest 别名：存量 bootstrap（不认 per-OS 别名的旧脚本）与未知
# 平台兜底；客户机队全 Windows 期恒指 win 包。
WIN_RT="$(printf '%s\n' "$RT_FILES" | grep '/runtime-win' | head -1 || true)"
if [ -n "$WIN_RT" ]; then
  push "$WIN_RT"        "runtime/latest/runtime-latest.tar.gz"
  push "$WIN_RT.sha256" "runtime/latest/runtime-latest.tar.gz.sha256"
fi
# 装机入口脚本（latest 恒指当前仓版本；脚本本身无密钥，鉴权在下载端点）。
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
push "$ROOT/scripts/bootstrap-node.sh" "bootstrap/latest/bootstrap-node.sh"
push "$ROOT/scripts/install-node.sh"   "bootstrap/latest/install-node.sh"
push "$ROOT/scripts/install-node.ps1"  "bootstrap/latest/install-node.ps1"

echo "==> [publish] done. 验收:"
$COMPOSE exec -T --user root cp find /app/downloads -type f | sort
echo "    节点侧自检（任一节点机）: curl -fsSL -H 'Authorization: Bearer bokn_…' \$
      https://<云域名>/api/nodes/downloads/pkg/$CANON/bok-node-$CANON.tar.gz -o /dev/null -w '%{http_code}\\n'"
