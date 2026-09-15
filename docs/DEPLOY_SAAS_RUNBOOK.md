# SaaS 三方部署 Runbook（云=运营方 / 客户 GPU 节点 / 话务员内网浏览器）

> 拓扑与边界详见 `docs/RUNTIME_TOPOLOGY.md` §0「分发型拓扑」；本文只讲**怎么装**。
> 三方各一条一键命令：云=`deploy/cloud/install.sh`，节点=`scripts/bootstrap-node.sh`，
> 话务员=零安装开浏览器。

## 0. 部署顺序（依赖关系，不可倒置）

```
① 云端起 CP（我们，一次性）
        │  产出：云域名 + root 账号
        ▼
② 每客户：建账号 + 签发节点 license（我们）
        │  交付客户三样：云域名 / license key / 装机命令
        ▼
③ 客户机房：bootstrap-node.sh 一键装机（license 注册+心跳 online）
        │  产出：节点内网地址（UI :3000 / LiveKit :7880）
        ▼
④ 话务员：浏览器开 http://<节点内网IP>:3000，用我们发的账号登录
```

数据流向：**媒体不出内网**（话务员 WebRTC ↔ 节点 LiveKit），业务数据出站到云
（账号/话术/对象/turns/审计），节点**零入站**要求（只需 443 出站）。

---

## ① 云端（我们·一次性）

前置：VPS（1C1G 起步）+ 宝塔 Docker 套件（或任意 docker compose 环境）+ Supabase
项目连接串（**session pooler 5432 端**，格式见 `deploy/cloud/.env.example`）。

一键：

```bash
cd deploy/cloud
./install.sh --database-url 'postgresql+psycopg://postgres.REF:PWD@aws-0-REGION.pooler.supabase.com:5432/postgres'
```

脚本自动：生成密钥（JWT/CP token/root 密码，`.env` 0600，重跑不覆盖）→
`docker compose up` → 轮询 `/health`（首启含 Supabase 幂等迁移建表，无需手工 SQL）
→ 三项验收（health ok / 静态站豁免非 401 / root 登录真 JWT）→ 打印节点装机一条龙。

验收判据（脚本内建，失败即红）：
- `curl :$PORT/health` → `"ok":true`
- 裸 `GET /` → 非 401（登录页可达）
- root 登录 → 返回三段式 JWT

深度文档（HTTPS 反代/宝塔面板安全/升级回滚/备份/GHCR 拉取慢的搬运法）：
`deploy/cloud/README.md`。**生产必须 HTTPS**（宝塔网站反代 127.0.0.1:8000 + 证书），
节点与浏览器都按域名访问。

## ② 每客户开通（我们）

```bash
# 建客户 admin（账号=数据边界，B1-B4 权限体系内）
curl -X POST https://<云域名>/api/users -H "Authorization: Bearer <root JWT>" \
  -H 'Content-Type: application/json' \
  -d '{"username":"<客户管理员工号>","password":"<强密码>","role":"admin","account_id":"acc-<客户>"}'

# 签发节点 license（max_nodes=客户 GPU 盒数；key 明文只出现这一次）
curl -X POST https://<云域名>/api/nodes/licenses -H "Authorization: Bearer <root JWT>" \
  -H 'Content-Type: application/json' -d '{"max_nodes":2,"account_id":"acc-<客户>","note":"<客户名>"}'
```

交付客户三样：**云域名、license key（`bokn_...`）、装机命令**（下条原文照抄填空）。

## ③ 客户 GPU 节点（客户机房·一键）

前置：能出公网 443（GitHub + 云域名 + 模型下载）；CUDA 节点先装好驱动
（`nvidia-smi` 有输出）；磁盘余量 ≥ 模型体积（~30-60GB）。

```bash
curl -fsSL https://raw.githubusercontent.com/halojerry/bok-voice/main/scripts/bootstrap-node.sh | bash -s -- \
  --cp-url https://<云域名> \
  --license-key bokn_<我们发的key> \
  --livekit-url ws://<本机内网IP>:7880
```

脚本自动：clone 仓库（幂等复用）→ 环境体检（GPU/磁盘）→ venv+依赖 → 模型下载
（`--skip-models` 可跳过）→ **license 注册**（同机重装/重启幂等复用 node_id 不烧配额）
→ 心跳探活 → UI 配置注入（`runtime-config.js` 写入 cpUrl/livekitUrl）。

> **`--livekit-url` 必须填本机内网 IP**（如 `ws://192.168.1.10:7880`），不能留默认
> 127.0.0.1——否则只有坐在节点机器上的人能通话，其他内网话务员连不上媒体。这是
> 装机最常漏的参数。

常驻（装完后续跑，非 `--heartbeat-only` 即拉起全栈含话务员 UI）：

```bash
nohup ~/bok-voice/.venv312/bin/python ~/bok-voice/tools/node_agent.py \
  --cp-url https://<云域名> --license-key bokn_<key> \
  --livekit-url ws://<本机内网IP>:7880 \
  --ui-dir ~/bok-voice/apps/web/out --interval 60 >/dev/null 2>&1 &
```

（license 流 token 落 `~/.bok/node-state.json`（0600）自动复用；systemd/launchd
模板后续轮提供。）

验收（我们侧）：`GET /api/nodes`（root JWT）见该节点 `online`。
升级节点：重跑 bootstrap 加 `--update`。

## ④ 话务员（内网浏览器·零安装）

- 打开 `http://<节点内网IP>:3000`（节点本地托管 UI，`runtime-config.js` 已注入云地址——
  UI 与语音走内网，登录与数据走云）；
- 用我们发的账号登录（无 token 不会静默匿名——云端 auth-on 形态必须登录）；
- 权限由客户的 admin 在「员工」页按人配置（8 键目录，报表默认关可授予）。

Windows 节点（如需）：`git clone` 仓库后 `powershell scripts/install-node.ps1 -CpUrl ... -LicenseKey ...`。

---

## 验收与排障对照

| 症状 | 先查 |
|---|---|
| 节点 register 401 | license key 错/吊销/配额满——重新签发；或时间不同步 |
| 节点 online 但话务员无声 | `--livekit-url` 是否填了内网 IP（非 127.0.0.1） |
| 云 CP /health 不就绪 | 容器日志 `docker compose logs cp`——多为 DATABASE_URL 不通（pooler 串/出网） |
| 登录页 401 | 镜像版本 < v0.2.0（静态站豁免修复前）；升级镜像 |
| 心跳 401 + 审计 `node.denied.fingerprint_mismatch` | token 被挪机/克隆——原机重跑装机命令幂等复活 |
| 红队残余与加固方向 | `docs/SECURITY_REDTEAM.md` |

## 边界（诚实面）

- **SIP 电话外呼是二期**：`scripts/deploy_sip_edge.sh` 已备，启用前置=trunk 供应商 +
  客户站点 LiveKit 公网档 + 8kHz 窄带复验（门禁结论在 `.superpowers` 调研档案）。
  纯 WebRTC 坐席不受影响。
- 节点交付目前依赖 GitHub 可达（仓库公开）；离线单文件包是 P2（Nuitka 计划）。
