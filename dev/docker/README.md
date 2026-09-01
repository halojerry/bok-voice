# 可选 Docker 开发栈（归档）

历史 Docker 编排（Postgres + LiveKit + Control Plane + Web + Agent）已从主流程移除：

- 开发统一走 `python tools/bok.py serve`（无 Docker，SQLite）
- CI 不再构建镜像
- 分发版全内嵌

如仍需容器化 Postgres/pgvector 做云路径开发，可在此目录手动使用：

```bash
cd dev/docker
docker compose up -d postgres
```

镜像构建与离线重建脚本已删除；本目录仅保留编排文件作为参考。
