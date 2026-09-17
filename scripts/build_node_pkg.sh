#!/usr/bin/env bash
# build_node_pkg.sh — 打节点代码包（交付链去 GitHub 化，2026-09-17）。
#
# 产物（默认 release-artifacts/）：
#   bok-node-<version>.tar.gz       代码包（git 追踪文件 + 注入 VERSION）
#   bok-node-<version>.tar.gz.sha256
#
# 包内布局：bok-node-<version>/…（node_agent.perform_update 接受带/无顶层前缀）。
# 不含：runtime/、.venv*、模型、app-data、__pycache__、未跟踪本地文件——
# 运行时由 build_runtime_pkg.sh 单独打包，模型由节点装机时从 HF 拉。
#
# 用法: build_node_pkg.sh <version>（或 NODE_PKG_VERSION=… / 已有 VERSION 文件）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-${NODE_PKG_VERSION:-}}"
if [ -z "$VERSION" ] && [ -f "$ROOT/VERSION" ]; then
  VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
fi
[ -n "$VERSION" ] || { echo "usage: build_node_pkg.sh <version>" >&2; exit 2; }

OUT_DIR="${BOK_PKG_OUT:-$ROOT/release-artifacts}"
STAGE="$(mktemp -d)/bok-node-$VERSION"
mkdir -p "$STAGE"
trap 'rm -rf "$(dirname "$STAGE")"' EXIT

echo "==> [node-pkg] staging tracked files -> $STAGE"
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # git archive=干净快照（尊重 .gitignore，无本地脏文件泄进包）。
  git -C "$ROOT" archive --format=tar --prefix="bok-node-$VERSION/" HEAD \
    | tar -xf - -C "$(dirname "$STAGE")"
else
  # 非 git 目录（离线打包机兜底）：复制排除重物。
  echo "    (non-git source: exclude-list copy)"
  tar -cf - -C "$ROOT" \
    --exclude='./.git' --exclude='./runtime' --exclude='./.venv*' \
    --exclude='./node_modules' --exclude='*/node_modules' \
    --exclude='./release-artifacts' --exclude='./apps/web/out' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='./.zcode' \
    --exclude='./.superpowers' --exclude='./decks' --exclude='./data' \
    . | tar -xf - -C "$STAGE"
fi
printf '%s\n' "$VERSION" > "$STAGE/VERSION"

mkdir -p "$OUT_DIR"
tarball="$OUT_DIR/bok-node-$VERSION.tar.gz"
echo "==> [node-pkg] packing $tarball"
tar -czf "$tarball" -C "$(dirname "$STAGE")" "bok-node-$VERSION"

# 统一格式：<hex>  <basename>（node_agent.perform_update 取首段比对）。
HASH="$(sha256sum "$tarball" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$tarball" | cut -d' ' -f1)"
printf '%s  %s\n' "$HASH" "$(basename "$tarball")" > "$tarball.sha256"

echo "==> [node-pkg] done:"
ls -lh "$tarball" "$tarball.sha256"
echo "    下一步: deploy/cloud/publish_node_pkg.sh $VERSION 推到云 CP"
