# Bok Voice 云端控制面镜像（thin node SaaS 形态）：CP API + 管理台静态站。
#
# 两段式：
#   1) web-build  — node:22-alpine 跑 Next.js 静态导出（apps/web → out/）
#   2) runtime    — python:3.12-slim 装 CP 运行时，并把静态站拷到 /app/web-out
#      （control_plane/main.py 启动时目录存在即挂到 "/"，见 BOK_WEB_STATIC_DIR）
#
# 注意：镜像**不含** apps/agent —— agent runtime 跑在客户节点 GPU 机器上，
# 云端 CP 不带（apps/control-plane 的依赖也不会传递引入它）。

# ---------- Stage 1: 管理台静态导出 ----------
FROM node:22-alpine AS web-build
WORKDIR /src/apps/web

# 依赖层：lockfile 不变即命中缓存（node_modules/.next/out 已被 .dockerignore 挡在上下文外）
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web/ ./
RUN npm run build

# ---------- Stage 2: CP 运行时 ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BOK_WEB_STATIC_DIR=/app/web-out \
    VAULT_ROOT=/app/data/vault

WORKDIR /app

# 本地包源码与 CP 源码：单条 pip 命令安装，pip 解析 `bok-voice-*` 依赖时直接命中
# 这些本地目录（仓库未发 PyPI 版）。apps/agent 不在此列——见文件头说明。
COPY packages/ /app/packages/
COPY apps/control-plane/ /app/apps/control-plane/
RUN pip install \
        /app/packages/core \
        /app/packages/business-db \
        /app/packages/knowledge \
        /app/packages/observability \
        /app/apps/control-plane

# 管理台静态站（Stage 1 产物；trailingSlash 导出，StaticFiles(html=True) 直接可服务）
COPY --from=web-build /src/apps/web/out/ /app/web-out/

# 非 root 运行。审计 JSONL 落 $HOME/Library/Application Support/BokVoice；知识库
# vault 默认 /app/data/vault（startup 会 mkdir）——两处都先备好属主，否则
# appuser 下 startup 即 PermissionError 崩（2026-09-15 容器冒烟实证）。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p "/home/appuser/Library/Application Support/BokVoice" \
    && mkdir -p /app/data/vault \
    && chown -R appuser:appuser /home/appuser /app/data
USER appuser

EXPOSE 8000

# 云端编排可用（纯标准库，镜像内无 curl）：
#   healthcheck: /health（startup 段的 DB 迁移/种子完成后才会返回 200）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]

# 与 tools/bok.py 同口径（仅 --host 由 127.0.0.1 改为 0.0.0.0，容器内需对外可达）
CMD ["python", "-m", "uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8000"]
