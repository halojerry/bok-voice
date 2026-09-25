# Bok Voice 两机部署（A 厂商云 × B 客户一体机）

两台机器的产品化拓扑：**A = 厂商云**（管理控制面 + MiniMax 云 TTS 中继 + 更新
分发），**B = 客户现场一体机**（全栈 all-in-one：本机 CP、LiveKit、agent/B 线
worker、本地模型，并向客户局域网提供运营台）。两机之间只走一条 WireGuard 隧道，
B 主动外拨——客户现场路由器**零端口映射、零入站**。

本目录两份 nginx 配置：`nginx-b.conf`（B 机局域网入口）、`nginx-a-relay.conf`
（A 机管理面 + TTS 中继）。分发型拓扑的代码侧契约见
`docs/RUNTIME_TOPOLOGY.md` §0；云端 CP 容器化跑法见 `deploy/cloud/`（A 机的
管理 CP 直接复用那套 compose/install）。

## 1. 拓扑总览

```
                         ┌─────────────────────────────────────┐
                         │  A：厂商云（公网 VPS）                │
                         │                                     │
                         │  管理 CP :8000（deploy/cloud 镜像）  │
                         │    ↑ 经 nginx /bok-cp/ 暴露          │
                         │    （root 控制台/节点注册表/commands/ │
                         │      更新分发 /api/nodes/*）          │
                         │  MiniMax 中继（nginx /minimax/）     │
                         │    ↳ https://api.minimax.cn          │
                         │      云凭据只存在于 A 机 nginx        │
                         │      snippet（root:0600）            │
                         └──────────────┬──────────────────────┘
                                        │ WireGuard（UDP 51820）
                                        │ B 主动外拨 + PersistentKeepalive 25
                                        │ → 客户现场无需任何入站端口/映射
                                        │ 心跳·TTS 中继·更新分发全走隧道
 ┌────────────────┐                    │
 │ 客户现场局域网   │        ┌───────────┴─────────────────────────┐
 │                │        │ B：客户一体机（全栈 all-in-one）       │
 │ 运营者浏览器 ───┼──HTTPS─┤  nginx :443（自签证书，本目录 nginx-b）│
 │ 工作台/主管台/   │  443   │   /        → 静态运营台 out/          │
 │ 同传台/话术工作台 │        │   /api/    → 本机 CP :8000            │
 └────────────────┘        │   /ws /rtc → LiveKit 信令 :7880       │
                           │  LiveKit :7880 信令 / 7881tcp 7882udp │
                           │  agent worker + interp fwd/rev ×3     │
                           │  ASR :8787 / TTS :8788 / LLM :1235-36 │
                           │  SIP 外呼：outbound-register 模式      │
                           │  （主动向运营商注册，NAT 后零入站端口）  │
                           └─────────────────────────────────────┘
```

> **SIP 说明**：外呼 trunk 用 **outbound-register 模式**（CP 设置页 P1.5
> 「注册成 outbound trunk」）——一体机主动向运营商注册，呼叫经注册通道回落，
> 客户现场 NAT 后不需要为 SIP 开任何入站端口。

## 2. 证书

### B 机（客户局域网）：自签

生成命令已写在 `nginx-b.conf` 文件头注释（openssl 一行，SAN 按一体机实际
局域网 IP 替换）。**浏览器豁免**（自签证书首次访问会拦截）：

- 单台运营机临时用：Chrome/Edge 地址栏报错页直接输入 `thisisunsafe` 放行；
  或点「高级 → 继续前往」。
- 正经做法（推荐）：把 `bok-appliance.crt` 装进运营机的受信任根
  （macOS 钥匙串「系统」→ 永远信任；Windows certmgr 受信任的根证书颁发机构），
  全浏览器免红锁。
- iOS Safari 无豁免输入法，必须装描述文件信任证书。

### A 机（厂商云）：Let's Encrypt（推荐）

```bash
sudo certbot --nginx -d <A 机域名>        # 自动改写 nginx 证书路径并配续期
```

必须用真域名 + Let's Encrypt 的原因：B 机 agent worker 的
httpx/websockets 客户端对中继做标准证书校验——A 机若自签，B 机还得手工装
信任链，凭空多一个现场故障点。没域名才退自签（并记得把 A 机证书装进 B 机
系统信任）。

## 3. WireGuard（B 外拨，A 免入站）

方向铁律：**B 主动外拨 A**，A 是被动的监听端。客户现场路由器不用做任何
端口映射/DMZ；NAT 保活靠 B 侧 `PersistentKeepalive = 25`。

要点（密钥一律 `wg genkey` 现场生成，**不落仓库、不进任何脚本**）：

| 配置项 | A（厂商云） | B（一体机） |
|---|---|---|
| 监听 | `ListenPort = 51820`（云安全组放行 UDP 51820） | 无需公网监听 |
| Endpoint | 无 | `Endpoint = <A 机公网IP>:51820` |
| PersistentKeepalive | 不需要 | **25**（每 25s 出一个保活包维持 NAT 映射） |
| AllowedIPs | B 的隧道地址/32（如 `10.66.66.2/32`） | `10.66.66.0/24`（走隧道赴 A） |
| 防火墙后效 | A 的 443 可再加一条「仅允许 B 隧道地址」收敛 | B 无直连公网出口，出网一律经隧道 |

验证：B 机 `ping 10.66.66.1`（A 的隧道地址）通，即隧道就绪；A 侧
`wg show` 看 B 的 `latest handshake` 在 25s×2 内跳动。

## 4. nginx 落位与生效

```bash
# B 机
sudo cp deploy/onprem/nginx-b.conf /etc/nginx/conf.d/bok-b.conf
sudo nginx -t && sudo systemctl reload nginx

# A 机（先把 MiniMax 凭据 snippet 建好——步骤见 nginx-a-relay.conf 文件头注释，
# 凭据值只进那个 root:0600 文件，任何仓库/脚本/会话记录都不落字面量）
sudo cp deploy/onprem/nginx-a-relay.conf /etc/nginx/conf.d/bok-a-relay.conf
sudo nginx -t && sudo systemctl reload nginx
```

两份配置都含 `map` 指令，必须放 **http 级 include 目录**（conf.d/）；放进
server 级 include 会直接 `nginx -t` 失败。A 机的管理 CP 容器化跑法（compose
+ `.env`）照抄 `deploy/cloud/`，`BOK_CP_PUBLIC_URL` 填
`https://<A 机域名>/bok-cp`。

## 5. env 接线表

| 变量 / 配置 | 在哪台 | 值 | 作用 / 注意 |
|---|---|---|---|
| `MINIMAX_BASE_URL` | B 机 agent/interp worker | `https://<A 机地址>/minimax/v1/t2a_v2` | HTTP 整段合成兜底。**代码把该值当完整端点 URL 原样 POST**（`providers/livekit_plugins.py` `_endpoint()`），不是裸 host——别只填到 `/v1` |
| `MINIMAX_WS_URL` | B 机同上 | `wss://<A 机地址>/minimax/ws/v1/t2a_v2` | bidi 持久连接主路径（默认 WS 模式）。代码自动补 `_bidi` 后缀，这里**不要**自带；漏配它 bidi 会直连 MiniMax 且无凭据 → 401 |
| `MINIMAX_API_KEY` | B 机 worker | **留空** | 铁律：云凭据永不落 B。A 机 nginx snippet 统一注入并覆盖同名头 |
| `MINIMAX_REGION` | B 机 worker | 不设或随意 | 走中继后无意义（端点已被 BASE_URL/WS_URL 钉死） |
| `runtime-config.js` 的 `cpUrl` | B 机 `/opt/bok/web/out/` | `https://<B 机局域网地址>` | 运营台 `apiBase()` 第一优先读它 → 同源 `/api/` 由 nginx 反代到本机 CP :8000。**必须写完整 URL**——空串是 falsy 会被忽略、回落构建期默认 127.0.0.1:8000（浏览器解析到运营机自己，必坏） |
| node_agent `--cp-url` | B 机 node_agent | `https://<A 机地址>/bok-cp` | 注册/心跳/commands/**更新分发** → A 管理面（`/api/nodes/*`，license/node_token 鉴权拉更新包） |
| node_agent `--license-key`（或 env `BOK_LICENSE_KEY`） | B 机 node_agent | 现场授权码 | A 管理面节点注册凭据 |
| node_agent `--ui-dir` | B 机 node_agent | **不要传** | 传了会按 `--cp-url` **覆写 runtime-config.js**——B 机的 UI 会被劫去调 A 管理面 CP。B 的运营台由 nginx 直接托管 out/，UI 配置手工维护（上一行） |
| `BOK_CP_TOKEN` | B 机全栈 env | 本机机器通道钥匙 | B worker ↔ **B 本机** CP 的机器通道（auth-on 下 turns/QA/设置上报必需；与 A 机无涉） |
| `BOK_AUTH_REQUIRED=1` + `BOK_JWT_SECRET` | B 机本机 CP | 按现场生成 | 局域网内也建议开（三层 RBAC；同值 token 分发到 B 机 worker） |
| `BOK_AUTH_REQUIRED=1` / `BOK_JWT_SECRET` / 节点工件目录 | A 机管理 CP | `deploy/cloud/.env` 同款 | root 种子/机器通道/更新分发工件（`publish_node_pkg.sh` 的推送对端） |
| CP 设置里的 LiveKit 地址 | B 机本机 CP | `wss://<B 机局域网地址>` | 通话 token 的 `serverUrl` 经 nginx `/rtc` 反代出（443 单口入） |

## 6. 防火墙

**B 一体机（客户现场）**

| 方向 | 端口 | 来源 | 用途 |
|---|---|---|---|
| 入 | 443/tcp | 客户局域网 | nginx：运营台 + API + LiveKit 信令（唯一 Web 入口） |
| 入 | 7882/udp（7881/tcp 备援） | 客户局域网 | LiveKit WebRTC **媒体直连**（不经 nginx，见 nginx-b.conf 注释） |
| 入 | WG 隧道内 | 仅 A 的隧道地址 | A 侧远程运维（可选，默认 A 无需向 B 主动发起任何连接） |
| 出 | 51820/udp → A | — | 唯一出网通道：心跳/TTS 中继/更新全走隧道；**不给 B 直连公网出口** |

**A 厂商云**

| 方向 | 端口 | 来源 | 用途 |
|---|---|---|---|
| 入 | 443/tcp | 公网（建议收敛：办公网 IP + B 隧道地址） | `/bok-cp/` 管理面 + `/minimax/` TTS 中继 |
| 入 | 22/tcp | 办公网 IP | SSH 运维 |
| 入 | 51820/udp | B 的公网出口 IP | WireGuard |

## 7. 验证清单

```bash
# A 机：管理面通（应回 {"ok":true,...}）
curl -s https://<A 机域名>/bok-cp/health
# A 机：中继路径通（MiniMax 对空 POST 回 4xx 即证明反代+凭据注入链路就绪；
# 回 401/无 Authorization 字样错误=snippet 没生效或没 include）
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<A 机域名>/minimax/v1/t2a_v2

# B 机：静态站 + API 反代 + 信令
curl -sk https://<B 机地址>/ | head -3            # 运营台 HTML
curl -sk https://<B 机地址>/api/settings | head -3  # auth-on 时 401 也算通（证明反代到了 CP）
```

最后端到端：运营机浏览器开 `https://<B 机地址>/` → 登录 → 建一通测试通话
（走 `/rtc` 信令 + 7882 媒体），听筒里 TTS 出声即证明 A 中继全链贯通。

## 8. 已知边界（部署前对齐）

1. **LiveKit 媒体假设同网可达**：浏览器 → 7882/udp 直连。客户局域网做了
   AP 隔离/跨网段时 UDP 不通，需要给 LiveKit 配 TURN（超出本目录范围，现场
   网络条件确认后再加）。
2. **`/minimax/` 开放中继风险**：A 机 443 若公网全开，中继可被任意人烧云
   配额——按 `nginx-a-relay.conf` 注释打开 allow/deny 或用防火墙收敛到
   WireGuard 网段（需现场 WG 网段定下来后填）。
3. **B 机 node_agent 的 UI 注入（F1/F2 补丁后姿势）**：可以带 `--ui-dir`——
   但必须同时 `--ui-cp-url http://127.0.0.1:8000`（B 本地工作 CP）；
   `--cp-url` 仍是 A 管理面（注册/心跳/更新）。runtime-config.js 由
   node_agent 自动写：cpUrl=B 本地、registryUrl=A（nodes 页/吊销/远程日志
   走 A）。**漏传 --ui-cp-url = 话务员 UI 被指去 A 的管理面**（登录都过不了
   ——A 的库里没有站点账号）。若不用 node_agent 托管 UI（nginx 直托管静态
   产物），runtime-config.js 手工维护，内容同上。
4. 本目录 nginx 配置未在本仓 CI 做语法门禁；改动后务必在目标机
   `nginx -t` 再 reload。
5. **吊销执行链（F4 注记，2026-09-24）**：A 侧点吊销后，对 B 的**通话面**
   没有直接拦截（B 的 /api/token 查本地 NodeStore 查不到 A 注册的节点=
   保守放行）——真正的执行链是：A 停发心跳应答 + 经注册命令通道下发停机
   指令 → node_agent 吊销熔断 `exit(0)` → `bok-node-agent.service` 是
   `Restart=on-failure`（tools/systemd_units.py，2026-09-20「勿改 always」
   契约）→ 单元保持 dead → node_agent 拉起的全栈随之熄火。**前提：B 的
   全栈必须由 node_agent 管**（appliance 形态的默认姿势）；若现场把 worker
   拆成独立单元绕开 node_agent，吊销就只剩 A 断中转/断更新两个阀门——
   不要拆。
