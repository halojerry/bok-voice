#!/usr/bin/env bash
# 离线重建 agent / control-plane / web 镜像（不依赖 docker hub / 第三方镜像源）。
#
# 背景：registry 镜像源（轩辕等）限流时，docker compose build 无法拉基础镜像。
# 本脚本从现有镜像派生本地基础镜像（python:3.12-slim / node:22-alpine），
# 再用 Dockerfile 的 ARG BASE_IMAGE 完成标准构建；pip/PyPI 需要可达（build isolation）。
#
# 用法：./scripts/rebuild_images_offline.sh   （完成后 docker compose up -d --force-recreate）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! docker image inspect voice-assistant-agent >/dev/null 2>&1; then
  echo "[offline] missing voice-assistant-agent image to derive the python base" >&2
  exit 1
fi
if ! docker image inspect voice-assistant-web >/dev/null 2>&1; then
  echo "[offline] missing voice-assistant-web image to derive the node base" >&2
  exit 1
fi

cat > .base-python.Dockerfile <<'EOF'
FROM voice-assistant-agent
RUN rm -rf /app/apps /app/packages /app/data && mkdir -p /app
EOF
cat > .base-node.Dockerfile <<'EOF'
FROM voice-assistant-web
RUN rm -rf /app && mkdir -p /app
EOF

echo "[offline] deriving local bases python:3.12-slim / node:22-alpine ..."
docker build -t python:3.12-slim -f .base-python.Dockerfile .
docker build -t node:22-alpine -f .base-node.Dockerfile .

echo "[offline] building control-plane ..."
docker build -f Dockerfile.control-plane --build-arg BASE_IMAGE=python:3.12-slim -t voice-assistant-control-plane .
echo "[offline] building agent ..."
docker build -f Dockerfile.agent --build-arg BASE_IMAGE=python:3.12-slim -t voice-assistant-agent .
echo "[offline] building web ..."
docker build -f Dockerfile.web --build-arg BASE_IMAGE=node:22-alpine -t voice-assistant-web .

rm -f .base-python.Dockerfile .base-node.Dockerfile
echo "[offline] done. 现在执行: docker compose up -d --force-recreate agent web control-plane"
