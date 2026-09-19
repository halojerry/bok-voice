#!/usr/bin/env bash
# build_runtime_pkg.sh — 打预构建运行时包（python/livekit/node[/win llama]，
# 交付链去 GitHub 化：客户节点从云 CP 拉运行时，绝不直连 GitHub release）。
#
# 前置：先跑 scripts/build_runtime.sh 组装 <root>/runtime（本脚本只打包不组装——
# 组装含 GitHub 上游下载，只发生在我们 CI/打包机，产物经 CP 分发）。
#
# 产物（默认 release-artifacts/）：
#   runtime-<os>-<arch>-<version>.tar.gz{,.sha256}
#
# 用法: build_runtime_pkg.sh <version> [--runtime DIR]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-}"
[ -n "$VERSION" ] || { echo "usage: build_runtime_pkg.sh <version>" >&2; exit 2; }
RUNTIME="${3:-$ROOT/runtime}"
if [ "${2:-}" = "--runtime" ]; then RUNTIME="${3:-$ROOT/runtime}"; fi
[ -d "$RUNTIME/python" ] || { echo "runtime 未组装（先跑 scripts/build_runtime.sh）: $RUNTIME" >&2; exit 1; }

case "$(uname -s)" in
  MINGW*|MSYS*) OS=win;  ARCH=x86_64 ;;
  Darwin)       OS=mac;  ARCH="$(uname -m)" ;;
  Linux)        OS=linux; ARCH="$(uname -m)" ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

OUT_DIR="${BOK_PKG_OUT:-$ROOT/release-artifacts}"
mkdir -p "$OUT_DIR"
tarball="$OUT_DIR/runtime-$OS-$ARCH-$VERSION.tar.gz"
echo "==> [runtime-pkg] packing $tarball (logs excluded)"
tar -czf "$tarball" -C "$RUNTIME" --exclude='./logs' .

# GitHub Release 单资产上限 2GB——超限在打包机就炸（错误可归因），别等到
# CI publish 步 422 才发现（v0.4.0 linux CUDA 全家桶实证）。1.9GB 留传输余量。
SIZE_BYTES="$(wc -c < "$tarball" | tr -d ' ')"
if [ "$SIZE_BYTES" -gt 1900000000 ]; then
  echo "!! runtime 包 ${SIZE_BYTES} 字节 > 1.9GB 护栏（GitHub 单资产 2GB 上限）" >&2
  echo "   拆法：重依赖移节点侧装（参照 requirements-runtime-linux.txt 基础面/" >&2
  echo "   -cuda.txt 节点侧两分法），勿打超限包。" >&2
  rm -f "$tarball"
  exit 1
fi

HASH="$(sha256sum "$tarball" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$tarball" | cut -d' ' -f1)"
printf '%s  %s\n' "$HASH" "$(basename "$tarball")" > "$tarball.sha256"

echo "==> [runtime-pkg] done:"
ls -lh "$tarball" "$tarball.sha256"
echo "    下一步: deploy/cloud/publish_node_pkg.sh $VERSION 推到云 CP"
