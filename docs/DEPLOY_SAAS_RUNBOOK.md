# SaaS 三方部署 Runbook（云=运营方 / 客户 GPU 节点 / 话务员内网浏览器）

> 拓扑与边界详见 `docs/RUNTIME_TOPOLOGY.md` §0「分发型拓扑」；本文只讲**怎么装**。
> 三方各一条一键命令：云=`deploy/cloud/install.sh`，节点=`bootstrap-node.sh`（bash）/
> `install-node.ps1 -Fetch`（Windows），话务员=零安装开浏览器。
>
> **2026-09-17 修订**：①交付链全面去 GitHub 化——客户从装机到升级只见云 CP 域名，
> 仓库已转私有；②节点=Windows 主路径（license 流）；③Tauri 桌面壳退役，UI 纯浏览器；
④新增 commands 远程更新通道（云端发版 → 节点心跳领单 → 自更新）。

## 0. 部署顺序（依赖关系，不可倒置）

```
① 云端起 CP（我们，一次性）
        │  产出：云域名 + root 账号
        ▼
② 发版工件就位（每次发版）：build → GitHub Release → publish 推云 CP
        ▼
③ 每客户：建账号 + 签发节点 license（我们）
        │  交付客户三样：云域名 / license key（bokn_*） / 装机命令
        ▼
④ 客户机房：一条命令装机（license 注册 + 心跳 online）
        │  产出：节点内网地址（UI :3000 / LiveKit :7880）
        ▼
⑤ 话务员：浏览器开 http://<节点内网IP>:3000，用我们发的账号登录
```

数据流向：**媒体不出内网**（话务员 WebRTC ↔ 节点 LiveKit），业务数据出站到云
（账号/话术/对象/turns/审计），节点**零入站**要求（只需 443 出站到云域名）。
客户对 GitHub 零依赖：代码包/运行时包/装机脚本全部从云 CP 鉴权下载。

---

## ① 云端（我们·一次性）

前置：VPS（1C1G 起步）+ 宝塔 Docker 套件（或任意 docker compose 环境）+ Supabase
项目连接串（**session pooler 5432 端**，格式见 `deploy/cloud/.env.example`）。
**仓库转私有后镜像随私有**——`docker login ghcr.io` 先做（README §1 第 4 条）。

一键：

```bash
cd deploy/cloud
./install.sh --database-url 'postgresql+psycopg://postgres.REF:PWD@aws-0-REGION.pooler.supabase.com:5432/postgres'
```

脚本自动：生成密钥（JWT/CP token/root 密码，`.env` 0600，重跑不覆盖）→
`docker compose up` → 轮询 `/health`（首启含 Supabase 幂等迁移建表，无需手工 SQL）
→ 三项验收（health ok / 静态站豁免非 401 / root 登录真 JWT）→ 打印节点装机一条龙
（**零 GitHub**：脚本与包都从本 CP 域名拉）。

验收判据（脚本内建，失败即红）：
- `curl :$PORT/health` → `"ok":true`
- 裸 `GET /` → 非 401（登录页可达）
- root 登录 → 返回三段式 JWT

深度文档（HTTPS 反代/宝塔面板安全/升级回滚/备份/GHCR 私有镜像登录/搬运法）：
`deploy/cloud/README.md`。**生产必须 HTTPS**（宝塔网站反代 127.0.0.1:8000 + 证书），
节点与浏览器都按域名访问。

## ② 发版工件（每次发版）

打包机（或 CI release.yml 自动）：

```bash
scripts/build_node_pkg.sh <版本>      # 代码包 tar.gz + sha256（~5MB）
scripts/build_runtime_pkg.sh <版本>   # 预构建运行时包（python/livekit/node[/win llama]）
```

CI（tag `v*`）自动跑同样两步并把产物挂 GitHub Release（**私有仓=私有资产**，
客户不可见）。操作员在云主机 `deploy/cloud/` 内一键推到工件卷：

```bash
./publish_node_pkg.sh <版本>          # pkg/ runtime/ bootstrap/ 三个前缀，latest 别名自动维护
```

## ③ 每客户开通（我们）

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

## ④ 客户 GPU 节点（一键；**Windows 主路径**）

前置：能出公网 443（云域名 + 模型下载 HF，中国网络可配 `HF_ENDPOINT` 镜像）；
CUDA 节点先装好驱动（`nvidia-smi` 有输出）；磁盘余量 ≥ 模型体积（~30-60GB）。

**Windows（管理员 PowerShell）——默认交付形态**：

```powershell
curl -fsSL -H "Authorization: Bearer bokn_<key>" `
  https://<云域名>/api/nodes/downloads/bootstrap/latest/install-node.ps1 -o install-node.ps1
.\install-node.ps1 -CpUrl https://<云域名> -LicenseKey bokn_<key> `
  -LivekitUrl ws://<本机内网IP>:7880 -Fetch -InstallService
```

`-Fetch` 自举：从 CP 拉代码包（sha256 校验）+ 预构建运行时包 → 解到
`%USERPROFILE%\bok-voice` → venv/模型/license 注册/心跳探活/doctor；
`-InstallService` 装完即注册 Task Scheduler 常驻（SYSTEM + 开机自起 +
RestartOnFailure 崩溃拉回，内部=`bok.py prod install --node-agent`）。

**Linux（bash 节点）**：

```bash
curl -fsSL -H "Authorization: Bearer bokn_<key>" \
  https://<云域名>/api/nodes/downloads/bootstrap/latest/bootstrap-node.sh | bash -s -- \
  --cp-url https://<云域名> \
  --license-key bokn_<key> \
  --livekit-url ws://<本机内网IP>:7880
```

脚本自动：拉代码包+校验 → 解到 `~/bok-voice`（可选拉运行时包）→ 环境体检 →
venv+依赖 → 模型下载（`--skip-models` 可跳）→ **license 注册**（同机重装/重启
幂等复用 node_id 不烧配额）→ 心跳探活 → UI 配置注入（`runtime-config.js` 写入
cpUrl/livekitUrl）。

> **`--livekit-url` 必须填本机内网 IP**（如 `ws://192.168.1.10:7880`），不能留默认
> 127.0.0.1——否则只有坐在节点机器上的人能通话，其他内网话务员连不上媒体。这是
> 装机最常漏的参数。

常驻：Windows 走 `-InstallService`（schtasks）；Linux 用 systemd 同义单元
（`bok.py prod install` 的 launchd 档 mac 可用；Linux systemd 模板后续轮）。
验收（我们侧）：`GET /api/nodes`（root JWT）见该节点 `online` 且 `version`=当前版本。

## ⑤ 话务员（内网浏览器·零安装）

- 打开 `http://<节点内网IP>:3000`——**node_agent 内嵌静态托管**（`:3000` 随
  `--ui-dir` 自动起），UI 与语音走内网，登录与数据走云（`runtime-config.js` 已注入）；
- 用我们发的账号登录（无 token 不会静默匿名——云端 auth-on 形态必须登录）；
- 权限由客户的 admin 在「员工」页按人配置（8 键目录，报表默认关可授予）；
- 输出设备切换仅 Chromium 内核支持（setSinkId）；Safari 回退系统默认。

## ⑥ 远程升级（commands 通道，2026-09-17）

```
发版（②）→ publish 推工件 → root 对目标节点下发指令：
POST https://<云域名>/api/nodes/<node_id>/commands
     {"action":"update","version":"<新版本>"}
     # 有在途通话默认 409 拒发；force=true 显式强更
→ 节点下一次心跳领单 → 自执行：CP 拉包 + sha256 校验 + 覆盖代码树 +
  pip 重装（runtime python）+ 停栈 + exit(75) → 守护拉起新版
→ 完成判定：节点心跳上报 version=<新版本> → CP 自动关单（done）
```

- 台账/舰队版本面板：`GET /api/nodes`（version + pending_commands）、
  `GET /api/nodes/{id}/commands`。
- 白名单动作：`update{version}` / `restart` / `shutdown`——**绝不推任意 shell**；
  全程审计 `node.command_queued`。
- 回滚 = 对同节点再发一条 `update <旧版本>`（工件仍在卷里即可）。

升级节点（旧法，仍可用）：重跑 ④ 装机命令（自举覆盖代码树、保留 runtime/ 数据）。

## 验收与排障对照

| 症状 | 先查 |
|---|---|
| 节点 register 401 | license key 错/吊销/配额满——重新签发；或时间不同步 |
| 节点 online 但话务员无声 | `--livekit-url` 是否填了内网 IP（非 127.0.0.1） |
| 话务员打不开 ：3000 | node_agent 是否带 `--ui-dir` 跑（托管随 `--ui-dir` 自动起）；端口被占看 node-agent 日志 `ui serve skipped` |
| 云 CP /health 不就绪 | 容器日志 `docker compose logs cp`——多为 DATABASE_URL 不通（pooler 串/出网） |
| 云主机 compose up `denied` | 仓库转私有后没 `docker login ghcr.io`（README §1.4） |
| 登录页 401 | 镜像版本 < v0.2.0（静态站豁免修复前）；升级镜像 |
| 心跳 401 + 审计 `node.denied.fingerprint_mismatch` | token 被挪机/克隆——原机重跑装机命令幂等复活 |
| update 409 | 节点有在途通话——等打完，或 force=true 显式强更 |
| update 后版本没变 | 心跳是否恢复（exit(75) 后守护拉回）；台账 status=failed 看 result；pip 重装失败重发一条 update |
| 工件下载 401 | 下载凭据=license key 或 node_token（Bearer）；确认没拿 root JWT 当下载凭据 |
| 红队残余与加固方向 | `docs/SECURITY_REDTEAM.md` |

## 边界（诚实面）

- **SIP 电话外呼是二期**：`scripts/deploy_sip_edge.sh` 已备，启用前置=trunk 供应商 +
  客户站点 LiveKit 公网档 + 8kHz 窄带复验（门禁结论在 `.superpowers` 调研档案）。
  纯 WebRTC 坐席不受影响。
- **节点磁盘上有 Python 源码**（节点要跑 agent/LLM 栈）：客户「找不到上游」达成
  （仓库私有+交付链零 GitHub），拿机器的人可读 .py；Nuitka 编译混淆=P2。
- **schtasks RestartOnFailure 有限 3 次**（≠ launchd 无限 KeepAlive）：update 的
  exit(75) 拉回依赖它，3 次用尽需人工；Linux systemd 模板后续轮。
- **copytree 覆盖不删除新版已移除的旧文件**（残留物不在运行导入路径，风险≈0；
  全量干净换目录受 Windows 目录锁限制，P2）。
- 模型下载走 HuggingFace（非 GitHub）；中国网络用 `HF_ENDPOINT` 指镜像。
