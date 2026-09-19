# Bok Voice 云端控制面部署（宝塔 / Docker Compose）

本目录把云侧控制面（CP API + 管理台静态站）以**单容器**形态跑在任何有 Docker 的
主机上，业务数据存 Supabase Postgres。镜像由 CI（`.github/workflows/cloud-image.yml`）
构建推送到 GHCR：`ghcr.io/halojerry/bok-voice:latest`。

> 镜像**不含** agent runtime（VAD→ASR→LLM→TTS 管线跑在客户节点 GPU 机器上）。
> 云端只管：账号/对象/话术/知识/通话记录/审计/LiveKit token 签发。
> 拓扑见 `docs/RUNTIME_TOPOLOGY.md` §0「分发型拓扑」。

## 1. 前置准备

1. **VPS**：一台国内可访问 Supabase 的云主机（1C1G 起步够用，CP 是纯 IO 型 API）。
2. **宝塔面板**：安装宝塔并装好 **Docker 套件**（软件商店 → Docker，含
   docker compose 插件；命令行 `docker compose version` 能出版本号即可）。
3. **Supabase 项目**：拿到数据库连接串（见 `.env.example` 里 `DATABASE_URL` 的
   格式说明——**必须用 session pooler 的 5432 端**）。
4. **GHCR 私有镜像登录（仓库转私有的必做，2026-09-17 起）**：源码仓已转私有，
   镜像随仓库变私有——拉取前先登 GHCR（GitHub → Settings → Developer personal
   access tokens 造一个 `read:packages` token）：
   ```bash
   echo "<GH_PAT>" | docker login ghcr.io -u <github用户名> --password-stdin
   ```
   漏了这步 `docker compose up` 会 `denied`。**搬运法同样要先 login**。
5. **GHCR 拉取慢（国内常见）**，两种处理任选：
   - 宝塔 Docker 设置里配置**镜像加速**，把 `ghcr.io` 加入加速/代理列表；
   - 或本地/中转机手动搬运后 retag（同样先 login）：
     ```bash
     docker pull ghcr.io/halojerry/bok-voice:latest
     docker tag  ghcr.io/halojerry/bok-voice:latest bok-voice:local
     docker save bok-voice:local | gzip > bok-voice.tar.gz
     # scp 到 VPS 后：
     docker load < bok-voice.tar.gz
     docker tag  bok-voice:local ghcr.io/halojerry/bok-voice:latest
     ```
     retag 回原名即可，compose 无需改动。

## 2. 导入并启动（两条路任选）

先把仓库 `deploy/cloud/` 目录整体传到 VPS（宝塔文件管理器上传或
`scp -r deploy/cloud root@<vps>:/www/bok-cloud/`）。

**路 A：宝塔面板（Docker → Compose 项目）**
- 新建 Compose 项目，项目目录选 `deploy/cloud/`（面板会识别其中的
  `docker-compose.yml`）；
- 先在文件管理器里 `cp .env.example .env` 并填好（密钥生成命令见模板注释）；
- 点「部署/启动」。后续看日志、重启都在面板里操作。

> ⚠ 面板的「部署/重建」按钮等价裸 `docker compose up`——`.env` 里 `DATABASE_URL`
> 保持池器**域名**形态时，无 IPv6 出口的主机会 crash-loop（§9 铁律）。面板用户
> 稳妥姿势：填 `.env` 时把 `DATABASE_URL` 的 host 段**直接写成 IPv4**（与
> `up.sh` 的 `BOK_SUPABASE_IP` 同值），此后面板重启/重建都安全；或改走路 B，
> 重启/重拉一律 `./up.sh`。

**路 B：SSH 命令行（推荐，输出更直观）**

```bash
cd /www/bok-cloud            # 即 deploy/cloud 目录
cp .env.example .env
vim .env                     # 填 DATABASE_URL / BOK_JWT_SECRET / BOK_CP_TOKEN / root 种子
./up.sh                      # 起容器（重启/重拉唯一入口,见 §9 铁律）
```

## 3. 首次启动：数据库自动建齐，无需手工跑 SQL

CP 启动时 `build_engine()`（`apps/control-plane/control_plane/deps.py`）会做
**幂等迁移**：`create_all` 建齐全部业务表 + 逐表幂等补列 + 语言值数据迁移 +
垫话罐头种子——对空 Supabase 库直接跑即可，**不要在 Supabase SQL 编辑器手工建表**。
表已存在时全部跳过，重复重启零副作用。

看迁移进度：

```bash
docker compose logs -f cp
# 见到 uvicorn 的 "Application startup complete." 即就绪（首次迁移秒级完成）
# 附带出现 "[deps] vector schema skipped" 属正常：无 pgvector 时向量段
# best-effort 跳过；Supabase 自带 pgvector，会建成 knowledge_chunks 向量表
```

## 4. 健康验证

```bash
# ① 存活（豁免鉴权，裸 curl 即可）
curl -s http://127.0.0.1:${BOK_CP_PORT:-18010}/health
# 期望：{"ok":true,"service":"bok-voice-control-plane"}

# ② root 种子成功 + 登录链路（返回 JSON 里应有 token 字段）
curl -s -X POST http://127.0.0.1:${BOK_CP_PORT:-18010}/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<BOK_ROOT_USERNAME>","password":"<BOK_ROOT_PASSWORD>"}'

# ③ 管理台静态站已随镜像就位（机器通道身份取页面）
curl -s -H "Authorization: Bearer <BOK_CP_TOKEN>" \
     -o /dev/null -w '%{http_code}\n' http://127.0.0.1:${BOK_CP_PORT:-18010}/
# 期望：200
```

> **已知缺口（2026-09-15 实测）**：`BOK_AUTH_REQUIRED=1` 时浏览器直接打开 `/`
> 会拿到 `401 {"detail":"unauthorized"}`——`identity_gate` 的豁免名单只含
> API 精确路径（`/health`、`/api/auth/login` 等），**管理台静态页尚未豁免**，
> 登录页 HTML 本身被拦。API 侧完全不受影响（上面的 ②③ 可全绿），管理台
> 页面访问待后端在豁免表补上静态路径后恢复；修复前如需人工看板，可临时用
> 带 token 的 curl 或本机桌面形态。此缺口已记录，勿在网关层绕过鉴权"修"它。

容器自带 HEALTHCHECK（镜像内置，`docker compose ps` 的 STATUS 列可见
`healthy/unhealthy`）；不健康时 `restart: unless-stopped` 会自动拉起。

## 5. 升级

```bash
cd /www/bok-cloud
docker compose pull          # 拉新 latest（或改 .env 里 BOK_CP_IMAGE 钉版本）
./up.sh                      # 重建容器；vault 卷与 Supabase 数据都保留
docker image prune -f        # 清旧镜像层（可选）
```

升级后回到 §4 复验 `/health`。回滚 = 把 `.env` 里 `BOK_CP_IMAGE` 钉到旧
`sha-xxxxxxx` tag（GHCR 每次发布都留）再 `./up.sh`。

## 6. 宝塔面板自身的安全（必读）

宝塔面板权限极大，**面板端口绝不能公网裸奔**：

- 面板端口改成非常见端口（面板设置→面板端口），并启用**安全入口**（随机路径）；
- 管理密码用强随机串，开面板登录限定（Google 验证/密码错误锁定）；
- 有固定办公网的话，在云厂商安全组里把面板端口**限源 IP** 白名单；
- 对外只需要放行：SSH（限源更佳）、80/443（网站反代）。**8000 不用对公网开**，
  反代走本机回环即可（见 §7）。

## 7. HTTPS（宝塔网站反代）

1. 宝塔 → 网站 → 添加站点（域名填管理台域名，纯静态、不建 FTP/PHP）；
2. 站点设置 → SSL → Let's Encrypt 申请证书，开「强制 HTTPS」；
3. 站点设置 → 反向代理 → 目标 URL `http://127.0.0.1:18010`（缺省口；改过
   `BOK_CP_PORT` 就填改后的），发送域名 `$host`。

注意：
- **LiveKit WebSocket 升级**：反代需放行 `Upgrade`/`Connection` 头（宝塔反代
  模板默认含，若手工改过 nginx 配置留意别删）；
- 管理台是纯静态 SPA + 同源 `/api`，反代整站到 CP 一个上游即可，无需拆路由。

## 8. 备份

| 数据 | 在哪 | 怎么备 |
|---|---|---|
| 业务数据（对象/话术/通话/账号/审计） | Supabase Postgres | Supabase Dashboard 自动备份/`pg_dump`（走 pooler 连接串） |
| 知识库 vault（人设参考音频、KB 源文件） | 宿主 docker 卷 `bok-cloud_cp-vault` | 见下方命令，建议每日 cron |
| `.env` | `/www/bok-cloud/.env` | 含全部密钥，改一份离线加密保存 |

```bash
# vault 卷一键备份（容器在跑也可执行）
docker run --rm -v bok-cloud_cp-vault:/data -v /www/backup:/backup \
  alpine tar czf /backup/bok-vault-$(date +%F).tar.gz -C /data .
```

## 9. 常用运维命令速查

```bash
docker compose ps              # 容器状态 + 健康
docker compose logs -f cp      # 跟日志
./up.sh                        # 重启/重拉唯一入口（幂等施加 DATABASE_URL 池器 IP 覆盖
                               #   [现场解析优先,漂移自动跟随] + 端口 18010；
                               #   可透传 compose 参数，如 ./up.sh --force-recreate）
docker compose down            # 停止并删容器（vault 卷保留；down 不吃 DATABASE_URL，可裸跑）
docker compose down -v         # ⚠ 连 vault 卷一起删（人设参考音频会丢，慎用）
```

> **铁律（2026-09-19 crash-loop 事故）**：`docker compose restart cp` 只在容器
> 定义不变时可用；任何 `up`（含 `--force-recreate`）**必须走 `./up.sh`**——裸 up 会
> 把 DATABASE_URL 回退 `.env` 的 IPv6-only 池器域名，容器 crash-loop（实案
> restarts=8）。详见 `docs/DEPLOY_SAAS_RUNBOOK.md`「重启/重拉铁律」。

> **管理台对外地址**：`.env` 配 `BOK_CP_PUBLIC_URL`（话务员浏览器可达的 CP 地址，
> 如 `https://cp.example.com` 或 `http://IP:18010`）——CP 启动自动写
> `runtime-config.js` 供管理台运行时取址；**不配则管理台回落构建期默认
> `127.0.0.1:8000`，云端口/域名形态下登录必 401 弹回**（2026-09-19 模拟拓扑
> 实测缺口）。
