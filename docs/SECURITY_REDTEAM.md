# Bok Voice 安全红队自检：CP 认证/节点面攻击面清点

> 防御性红队交付（feat/p1-deploy，2026-09-15）。授权范围=本仓自有系统；
> 目标是对**我方自己的** CP 认证/节点鉴权面做攻击面清点 + 可自动化探针，
> 每条攻击面给出防线代码锚点（file:line）、残余风险与验收判据。
> 可自动化的判据编号 `[probe-N]` 与 `scripts/redteam_probes.py` 一一对应；
> 人工判据标 `[manual]`。
>
> **红线**：本档所有探针只允许打自起 CP 实例（默认宿主口 18015）或明确授权的
> 加固 CP；开发栈 :8000 与生产实例绝不作为探针目标。

---

## 1. 资产与信任边界

```
                          ┌───────────────────────────────────────────────────────┐
                          │                  云端 CP（:8000 / compose）             │
                          │                                                       │
  浏览器话务员 ────JWT───▶│  identity_gate（auth.py:239，第一注册=最内层）           │
  （管理台 SPA，           │   ├─ 业务面 /api/*：root/admin/user 三层 RBAC           │
   token 存 localStorage）│   │   （scoped_account / deny_cross_account /          │
                          │   │     require_role / deny_foreign_owner）            │
                          │   ├─ 机器通道：BOK_CP_TOKEN → state.machine 直通        │
  agent worker ──CP token▶│   │   （auth.py:253-256；agent 上报 turns/设置等）      │───▶ Supabase Postgres
  （外呼/同传执行端）       │   ├─ /api/nodes/register：license 闸（端点内自鉴权）     │    （业务库 + nodes/
                          │   └─ /api/nodes/heartbeat：node_token 自鉴权           │      node_licenses 表）
                          │                                                       │
  节点机 node-agent ─────▶│  license key（bokn_*，注册一次）→ node_token（心跳，     │
  （PyInstaller 单文件）    │  sha256 库存）+ 机器指纹（客户端自报 sha256）            │
                          └───────────────────────────────────────────────────────┘
```

四类资产与信任边界：

| 资产 | 所在边界 | 承载凭据 | 沦陷后果 |
| --- | --- | --- | --- |
| 云端 CP + Supabase 业务库 | 云（最内圈） | `BOK_JWT_SECRET` / `BOK_CP_TOKEN` / root 种子（全部 env 注入） | 全域失守（话术/对象库/账号体系） |
| 浏览器话务员会话 | 外圈（人） | 用户 JWT（8h TTL，`auth.py:33`） | 单人身份冒用，受三层 RBAC 收窄 |
| 节点机 | 最外圈（客户机房） | license key（一次性）→ node_token（状态文件 0600） | 单账号运行时数据 + 本机模型权重副本 |
| 机器通道持有方 | 脚本/CI/agent | `BOK_CP_TOKEN` | 万能钥匙面（见 A7） |

三条认证通道严格分离（`apps/control-plane/control_plane/auth.py:1-16` 模块注释）：
**用户 JWT**（root/admin/user）≠ **机器通道 `BOK_CP_TOKEN`** ≠ **节点 `node_token`**
（+注册期 license key 自证）。互不同源，单一通道泄露不自动放大为另两通道失守。

## 2. 攻击面清单

### A1①  register 裸注册（匿名/无许可直接入册节点）

- **攻击向量**：直接 `POST /api/nodes/register`（该路径在中间件豁免表内，
  `auth.py:38-50`——豁免的设计理由：license key 本身就是鉴权因子，与心跳的
  node_token 同构，中间件预拦会把只持 license 的节点挡死）。攻击者希望零凭据
  换到 node_token 入册节点、骗派发。
- **防线**：加固模式（`BOK_AUTH_REQUIRED=1` 或 `BOK_CP_TOKEN` 任一，
  `main.py:2068-2070` `node_license_required()` 单点判定）下端点内强制
  license 三查（`main.py:2019-2024` → `nodes_store.py:210-224`
  `validate_license_for_register`）：无 key/未知 key → 401；吊销 → 401；
  配额满且指纹不同 → 403。`tests/test_node_license.py:116-144` 钉死该流。
  本地双关全空=开放注册（单机形态零变化，`tests/test_node_license.py:109-113`）。
- **残余风险**：部署者忘开加固开关（双 env 全空）时 register 回到开放档——
  这是刻意的本地形态兼容，不是漏洞，但云端必须以 compose `BOK_AUTH_REQUIRED`
  默认 1（`deploy/cloud/docker-compose.yml:31-32`）兜住。
- **验收判据**：`[probe-1]` 加固 CP 上裸注册必须 401。

### A2②  license key 暴力猜测

- **攻击向量**：对 register 端点枚举 `bokn_*` key。
- **防线**：key = `bokn_` + `secrets.token_urlsafe(24)`
  （`nodes_store.py:83-84`）≈ **192 bit 随机**，库内只存 sha256
  （`nodes_store.py:40-41, 98`），明文只在签发响应出现一次（
  `nodes_store.py:57-64` 出参永无 `key_hash`；`main.py:2089-2093` 清单端点不回显）。
  **无计时泄漏面说明**：`find_license`（`nodes_store.py:122-145`）先对攻击者
  提交的明文做 sha256 **再**查 hash 索引——比较发生在摘要域且经过雪崩，
  响应时间不携带正确 key 任何字节位的信息，不存在逐位/逐前缀的计时 oracle；
  192 bit 空间下枚举在数学上不可行。
- **残余风险**：register 无速率限制（与 A10 同族）——当前靠熵兜底；加固方向：
  对 401 频次按来源 IP 限速 + 审计采样。
- **验收判据**：`[probe-2]` 错 key 恒 401（多把随机 key 全部拒绝、无 200/403
  混入；响应耗时仅作信息性打印，不作断言）。

### A3③  node_token 窃取后跨机使用

- **攻击向量**：偷到节点状态文件 `~/.bok/node-state.json`（node-agent 落盘，
  `tools/node_agent.py:132-139`，chmod 0600）里的 node_token，拿到另一台机器
  （不同指纹）上直接心跳。
- **防线**：心跳三验（`nodes_store.py:301-354`）：token 有效 → license 仍
  active → **指纹与注册一致**；指纹不符 = 克隆/挪机检出 → `fingerprint_mismatch`
  **自动吊销该节点**（`nodes_store.py:330-335` `_revoke_node`）并落审计
  `node.denied.fingerprint_mismatch`（`main.py:2052-2054`）——小偷与原机
  **同死**（原指纹再用心跳也 401，`tests/test_node_license.py:68-79`）。
  原机恢复路径：同 (license, fingerprint) 重注册幂等复用 node_id 换新 token
  （`nodes_store.py:189-208` 不排除 revoked 行、`247-258` 换 token；
  `tools/node_agent.py:110-141` 探测 401 即自动重注册）。
- **残余风险**：指纹是**客户端自报**（见 §4 诚实边界）；窃取者若连原机指纹
  一起伪造（把 `fingerprint` 字段照抄），心跳可过——此时等同 A4 整机克隆场景。
- **验收判据**：`[probe-3]` 好 key+指纹 A 注册 200 → 同 token 换指纹 B 心跳
  401 → 再用指纹 A 心跳**也** 401（证明自动吊销已发生，不是只拒单次请求）。

### A4④  克隆 VM 整机复制（含状态文件）

- **攻击向量**：整盘克隆一台已注册节点（`/etc/machine-id`/序列号与
  `node-state.json` 的 token 一起复制）——两台机器指纹与 token 全同。
- **防线（诚实结论=这是已知残余）**：指纹相同 → 心跳三验全过，服务端
  **结构上无法区分** clone 与原机（`nodes_store.py:330-331` 只在指纹**不等**
  时触发）。当前设计把这里当作威慑边界而非绝对防线（§4）。
- **服务端可加固方向**（均不需动客户端）：
  1. 记录并比对每次心跳来源 IP/ASN——同指纹从异常地理/ASN 并发心跳即吊销+审计；
  2. 同 (license, fingerprint) 并发心跳频率异常检测（真正单机不会同时高频心跳）;
  3. 心跳携带的 metrics 时间窗一致性校验。
  落点都在 `nodes_store.heartbeat` 与 `main.py` 心跳端点，属后续轮。
- **验收判据**：`[manual]`（需多源 IP 环境才能自动化；当前探针只覆盖同指纹
  幂等复用不烧配额——`[probe-7]` 从反面验证配额闸：异指纹第二台 403）。

### A5⑤  license 吊销时效

- **攻击向量**：客户欠费/违规，运营吊销 license 后，其名下节点是否还能继续
  心跳存活、骗派发。
- **防线**：`revoke_license` 一刀切——license 置 revoked **且名下全部未吊销
  节点立即置 revoked**（`nodes_store.py:147-182`，SQL 分支单条 UPDATE）；
  节点下一次心跳（默认间隔 60s，`nodes_store.py:18`）即 401
  `license_revoked` 并再次确认吊销（`nodes_store.py:315-327`），审计
  `node.license_revoked`（`main.py:2103-2105`）。node-agent 侧探测 401 →
  自动重注册 → 注册闸再被「license revoked」401 挡死，复活不可能。
- **残余风险**：最坏窗口=一个心跳间隔（≤60s，在线判定窗 180s
  `nodes_store.py:18-19`）；已在本机跑着的栈不会被子进程杀死（吊销是准入
  面不是 kill 面）——彻底断电走运维通道。
- **验收判据**：`[probe-4]` root 吊销 license → 名下节点心跳立即 401。

### A6⑥  JWT 伪造 / 弱密钥

- **攻击向量**：①无密钥手搓 token 冒充 root；②用泄露的弱 `BOK_JWT_SECRET`
  离线签发任意角色 token；③算法混淆（HS→none/RS 切换）。
- **防线**：签名校验算法**钉死单值** `HS256`（`auth.py:32`，decode 传
  `algorithms=[_JWT_ALGO]`，`auth.py:117`——none/RS 混淆结构性不可行）；
  任何解码失败一律统一 401 不泄露原因（`auth.py:111-129`）；CP startup
  **fail-closed**：`BOK_AUTH_REQUIRED=1` 而无任何密钥 → 拒绝启动
  （`main.py:200-204`），签发无密钥同样 503（`auth.py:92-94`）。
  部署面 ≥32 字节门禁：compose 把 `BOK_JWT_SECRET` 设为 `:?` 必填守卫 +
  生成指引 `openssl rand -hex 32`（`deploy/cloud/docker-compose.yml:34-35`，
  32 字节=256 bit 熵）；`.env` 缺文件 compose 直接起不来
  （`docker-compose.yml:24-28` fail-fast）。
- **残余风险**：≥32 字节是**生成/部署指引**，运行时无长度校验——手填
  `secret123` 能启动（HS256 离线爆破可行）；加固方向：startup 增加
  `len(secret)>=32` 硬闸。`BOK_JWT_SECRET` 未设时回落 `BOK_CP_TOKEN`
  （`auth.py:68-69`）——两值同源时机器钥匙泄露=JWT 全可伪造，部署时必须
  两值独立（compose 分开必填即为此）。
- **验收判据**：`[probe-5]` 用错误密钥签的 HS256 token（claims 齐全、未过期、
  role=root）访问 `/api/users` 必须 401。`[manual]` 抽查生产 env 两密钥独立
  且由 `openssl rand -hex 32` 生成。

### A7⑦  `BOK_CP_TOKEN` 万能钥匙面

- **攻击向量**：该值等价于越过全部用户门禁：identity_gate 对同值请求打
  `state.machine` 直通（`auth.py:253-256`），且 `require_role` 对机器通道
  放行（`auth.py:190-191`）——持有者可直呼 root 专属面（license 签发/审计/
  设置）。泄露=全绕过。
- **防线（缓解非消除）**：部署面 env 注入不落盘——compose 从 `.env` 装配
  （`docker-compose.yml:24-28, 36-37`），仓库零硬编码（二进制扫描见 A9）；
  与用户 JWT 不同源，浏览器会话泄露不波及它；轮换=改 env 重启即可。
  机器通道语义是**有意设计**（agent worker 分布式部署必须带同值，见
  AGENTS.md 三层 RBAC 节），不能删除只能收敛。
- **残余风险**：持有面=每台 agent worker 的 env + CI secret；任一节点 env
  泄露即等价 root。加固方向：机器通道降权为独立 role（只放行业务上报端点）、
  按节点签发独立 machine token。
- **验收判据**：`[probe-8]` 正确 CP token 直通 root 专属 license 面（200）+
  错误值 401（确认门在，不是裸奔）。

### A8⑧  静态站 GET 豁免不泄露数据

- **攻击向量**：auth-on 下 SPA 静态文件（含登录页）对 GET/HEAD 全放行
  （`auth.py:246-248`；BOK_CP_TOKEN 中间件同款 `main.py:127-128`）——攻击者
  希望从静态面扒到特权数据。
- **防线**：豁免面**只有非 `/api` 的 GET/HEAD**；全部特权数据都在 `/api/*`
  后面过门禁（`main.py:3366-3373` 静态挂载排在全部 API 路由之后，`/api/*`
  与 `/health` 不受影响）。裸 GET `/api/users` 无凭据 → identity_gate 401
  （`auth.py:257-262`）。豁免的存在理由：云端 auth-on 曾把登录页自己 401、
  B4 登录流程整站不可达（AGENTS.md「静态站 GET 豁免」节，
  `tests/test_node_license.py:158-165` 钉死）。
- **相邻观察（诚实记录）**：`/docs`、`/openapi.json` 在豁免表内
  （`auth.py:46-48`）——匿名可读 API schema（只有端点形状，无数据）；CORS
  `allow_origins=["*"]`（`main.py:98-103`）——本 API 凭据走 Authorization
  头而非 Cookie，浏览器跨域不可自动附带、也不可读取响应（非凭据模式），
  现状不构成可利用面；收紧属锦上添花。
- **验收判据**：`[probe-6]` 裸 GET `/` 不得 401（登录页可达）**且**裸 GET
  `/api/users` 必须 401（数据不出豁免面）。

### A9⑨  二进制静态密钥扫描（PyInstaller 产物）

- **攻击向量**：分发到客户机房的 `dist/node-agent`（onefile，7-8MB）可被
  `strings`/pyinstxtractor 解包——希望从中提取硬编码 license/token 常量，
  一次逆向全网复用。
- **防线**：`tools/node_agent.py` 源码**零硬编码密钥**——license key 由
  `--license-key` 参数传入、node_token 来自注册响应并落状态文件
  （`tools/node_agent.py:96-141`），模块级 import 面=纯 stdlib
  （`docs/NODE_PACKAGING.md`「import 面分析」）。因此产物 strings 扫不出
  `bokn_` 前缀或任何密钥常量。**诚实边界**：零静态密钥 ≠ 防解包——
  PyInstaller 产物可被解出源码字节码，本判据只断言「无可提取密钥」。
- **残余风险**：字段名（`node_token`/`license_key` 作 JSON key、argparse 用法
  文本）合法存在于产物——扫描模式必须匹配**密钥值形态**（`bokn_` 前缀、
  「字段名+长字面量赋值」）而非裸字段名，否则误报。
- **验收判据**：`[probe-9]`（`--scan-binary <path>`，独立参数）对产物扫
  `bokn_[A-Za-z0-9_-]{16,}`、`node_token|license_key … ["'=]<24+ 位字面量>`
  等密钥值模式，命中=FAIL。

### A10⑩  心跳端点暴力 token 猜测

- **攻击向量**：`/api/nodes/heartbeat` 在豁免表（`auth.py:40-41`，节点凭
  node_token 自鉴权、无法带 JWT/CP token），可被匿名刷 token 枚举。
- **防线**：token = `secrets.token_urlsafe(32)`（256 bit，
  `nodes_store.py:234`），校验=先 sha256 再按 hash 等值查
  （`nodes_store.py:309-312`，摘要域比较无计时 oracle）；错 token 一律
  401 且**不刷审计**（`main.py:2050-2051` 注释：普通凭据错误只 401，
  防心跳重试刷屏；只有 fingerprint_mismatch/license_revoked 落
  `node.denied.*`）。
- **残余风险（如实标注）**：**无速率限制/无锁定**——端点可被匿名高频打。
  256 bit 空间使命中概率可忽略，但请求本身是廉价 DoS 面。加固方向：
  按 IP 的 429 限速；同源连续 401 采样审计；`/docs` 出豁免表（A8 相邻项）。
- **验收判据**：`[manual]`（现网网关层限速配置核查）。探针不做高频打点
  （不对己方服务做可用性攻击）。

### A11⑪  审计面（安全事件可告警）

- **攻击向量**：（防御视角）上述攻击发生时，运营侧是否**知道**。
- **防线**：节点面安全事件全部落审计——`node.registered`
  （`main.py:2034-2038`，含指纹前缀）、`node.license_created`
  （`main.py:2082-2085`）、`node.license_revoked`（`main.py:2103-2105`）、
  **`node.denied.fingerprint_mismatch` / `node.denied.license_revoked`**
  （`main.py:2052-2054`，克隆/吊销事件）——审计双写 JSONL + 业务库
  （`main.py:243-251` audit tap），`/api/audit` 可查询。登录失败亦有
  `auth.login_failed`（`main.py:708-710`）。
- **残余风险**：普通 401 不落审计（防刷屏的有意取舍，A10）；告警消费端
  （对 `node.denied.*` 建监控/告警规则）属运维面，当前为人工动作。
- **验收判据**：`[manual]` 在加固环境触发一次克隆（用他机指纹心跳）后于
  `/api/audit` 查到 `node.denied.fingerprint_mismatch`；告警规则接线待接。

## 3. 探针索引（`scripts/redteam_probes.py`）

| 编号 | 攻击面 | 判据 | 依赖 |
| --- | --- | --- | --- |
| probe-1 | A1① | 裸注册 → 401 | 加固 CP |
| probe-2 | A2② | 错 key 恒 401（8 把随机 key） | 加固 CP |
| probe-3 | A3③ | 异指纹心跳 401 → 自动吊销 → 原指纹也 401 | 加固 CP + root |
| probe-4 | A5⑤ | root 吊销 license → 心跳 401 | 加固 CP + root |
| probe-5 | A6⑥ | 错密钥伪造 JWT → `/api/users` 401 | 加固 CP |
| probe-6 | A8⑧ | 裸 GET `/` 非 401 且裸 GET `/api/users` 401 | 加固 CP |
| probe-7 | A4④/配额 | max_nodes=1 已占 → 异指纹注册 403 | 加固 CP + root |
| probe-8 | A7⑦ | 正确 `BOK_CP_TOKEN` 直通 license 面 / 错误值 401 | 加固 CP + `--cp-token` |
| probe-9 | A9⑨ | 产物 strings 扫密钥值模式，命中=FAIL | `--scan-binary <path>` |

运行方式：

```bash
# 自起加固 CP（宿主口 18015 + 临时 sqlite + 随机 root），跑完全部探针即清理：
.venv312/bin/python scripts/redteam_probes.py --self-host [--scan-binary dist/node-agent]

# 对既有加固 CP 跑（不spawn实例）：
.venv312/bin/python scripts/redteam_probes.py --base-url https://cp.example.com \
    --root-user root --root-pass '***' [--cp-token '***']

# 退出码：全 PASS=0；任一 FAIL=1（SKIP 不计失败）。
```

探针**只打加固 CP**（auth-on）。对未加固 CP 跑 probe-1 得 200 即 FAIL——
这正是探针要抓的回归（云端忘开 `BOK_AUTH_REQUIRED`）。

## 4. 诚实边界（必读）

1. **机器码鉴权=威慑不是 DRM**。指纹在节点本机自报
   （`tools/node_agent.py:37-77`），改过客户端的人可以抹掉指纹上报、照抄
   原机指纹或直接 patch 二进制跳过校验。防线的真实落点全在**服务端**：
   签发（root 专属）、配额、吊销、异指纹克隆检出、审计——目标是抬高滥用
   成本与**可检出性**，不是防绝对逆向。PyInstaller 产物可被解包（A9），
   Nuitka/混淆是后续可选升级，不改变这一性质。
2. **真正的 IP（知识产权）不在节点上**：云端话术模板/对象库/账号体系在
   CP+Supabase 后面（JWT 门禁 + 三层 RBAC），不随节点分发；节点沦陷的
   暴露面=**单账号运行时数据**（该节点的 turns/通话数据、状态文件里的
   node_token）+ 节点本机的模型权重副本（本地全栈形态权重必然落节点盘，
   这是本地优先架构的既定取舍，不是本鉴权面能解决的）。
3. **心跳/注册豁免与无速率限制是已知取舍**（A2/A10）：靠 192/256 bit 熵
   兜底，探针不做可用性攻击；限速与锁定是明确的加固方向。
4. **本档与探针是自检工具**：PASS 只证明「已知防线在位」，不证明「无其他
   漏洞」；对未授权系统跑探针不被允许。
