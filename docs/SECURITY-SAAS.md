# SaaS 安全姿态（W④/W⑧ 收口配套，2026-10-09）

多租户 SaaS 形态的安全基线：**客户只见我们的域名**（CP + 静态台 + LiveKit），全部模型流量在服务端；**非 root 零厂商字面量**（UI/请求体/响应体/静态产物四层全零，门禁见 `tests/test_vendor_literal_hygiene.py` + CI 产物 grep）；**到期=整站续费页**（CP `identity_gate` 逐请求重估，见 W②）。本文=部署面与运维面的清单式基线。

## 1. 公网暴露面（断言清单）

公网**只许**三个入口，其余一律内网绑定——部署后 `ss -tlnp` 核对：

| 入口 | 端口 | 说明 |
|---|---|---|
| CP | :8000 | `/api/*` + 静态台托管（`BOK_AUTH_REQUIRED=1` 强制，见下） |
| web 静态产物 | :443（经 nginx/云 LB） | `apps/web/out/`（客户档，`npm run build` 产物） |
| LiveKit | :7880 (+TURN) | WebRTC 信令/媒体；API key/secret 只在服务端 env |

**必须内网**（compose `network_mode`/不声明 `ports:`）：agent worker :8081-8083、ASR/TTS sidecar :8787/:8788、csc :8792、laya :8791、LLM :1234-1239、queue_proxy、Redis、Postgres/Supabase pooler、node_agent :3000（云形态不部署）。

- 启动 fail-closed（W⑥-3）：非回环 bind 且 `BOK_AUTH_REQUIRED≠1` → CP 拒启（`BOK_INSECURE_PUBLIC_BIND=1` 显式认账才放行——实验室档专用）。
- 静态产物双构建：客户档 `npm run build`（桩页，零厂商字面量）；平台控制台 `npm run build:platform` → `platform-out/` 部署到**独立入口**（内网域名或 Basic-Auth 的 nginx location），客户域名只挂客户产物。
- CI 已加产物级 grep 门（客户 `out/` 零厂商词命中即红）。

## 2. 凭据轮换矩阵

| 凭据 | 位置 | 轮换动作 | 影响面 |
|---|---|---|---|
| `BOK_JWT_SECRET` | CP env（≥32B，与 CP_TOKEN 异值） | 改值+重启 CP | 全员登录态失效（重登即可）；已签 JWT 全部 401 |
| `BOK_CP_TOKEN` | CP + agent worker env | 同步改两处+重启 | 机器通道换凭据；用户面零影响 |
| LiveKit API key/secret | livekit.yaml + CP `LIVEKIT_API_*` env | 先加新 key 双活→切→删旧 | 在途房 token 到期自然收线（TTL 3600s） |
| Supabase/DB 密码 | DATABASE_URL | Supabase 控制台轮换→改 env→重启 | 连接池重建 |
| 厂商 api_key（TTS/ASR/LLM） | 设置库（root 面）/env | root 在平台控制台改存 | 下一通生效（agent 每通装配热读） |

**泄露响应**：JWT_SECRET 疑泄 → 立即轮换（全量下线成本最低）；CP_TOKEN 疑泄 → 轮换+重启 worker；厂商 key 疑泄 → 平台控制台即刻改存。

## 3. HTTP 响应头（已内置）

CP 全响应已加 `X-Frame-Options: DENY` + `X-Content-Type-Options: nosniff`（`security_headers_gate`）。CSP 建议在前置 nginx/云 LB 上对静态台加（CP 不盲配 script-src，防误杀 runtime-config 注入）：

```
Content-Security-Policy: default-src 'self'; connect-src 'self' wss://<livekit-host>; img-src 'self' data: blob:; media-src 'self' blob:; frame-ancestors 'none'
```

另在 nginx 收请求体：`client_max_body_size 25m;`（克隆参考音频上限 20MB + 余量）。

## 4. LiveKit 面

- **根凭据强度**：API secret ≥32B 随机；`bok doctor` 含检查位。token 全部服务端签发（`/api/token` 房间作用域，旁听 can_publish 全关）。
- **并发上限**：livekit.yaml 配 `room.max_participants` 与会话并发上限（防连接洪）；数值按部署容量定。
- **TURN/ICE 隐私**：客服/客户真实 IP 会经 ICE 候选互泄——对隐私敏感的客户开 relay-only（livekit TURN `use_external_ip` + 客户端 `iceTransportPolicy: "relay"` 档），代价是延迟与 TURN 带宽。

## 5. 滥用闸（已内置）

| 闸 | 位置 | 默认 |
|---|---|---|
| 登录频控 30/min/用户名 | CP login | 开 |
| 合成类操作 30/min/账号（preview/pregen×2） | CP `BOK_COST_RATE_LIMIT` | 开（=0 关） |
| 外呼日拨配额 N 通/天/账号 | CP `BOK_DIAL_DAILY_QUOTA` | **0=关**（SaaS 建议按合同设 50~200） |
| 账号订阅到期 | accounts.expires_at + identity_gate | NULL=永久 |

## 6. 残余风险（记录在案，接受）

- **localStorage JWT**：XSS 若成立可偷 token；缓解=8h TTL + 逐请求吊销（禁用/降权/到期即时生效）+ 全站零 `dangerouslySetInnerHTML` + Streamdown 禁 raw HTML（`tests/test_agent_prompt_hygiene.py` 钉死）。可选后续：迁 sessionStorage（牺牲登录持久化）。
- **登录按 IP 维度限速**：用户名维度滑窗已有；分布式撞库（海量用户名）留 IP/全局维度为后续项。
- **npm/pip 依赖审计**：CI 当前为**advisory**（存量有 high 级发现，先清零后翻 blocking）。

## 7. 监控与备份

- 磁盘水位告警（audit/calls/日志增长）+ agent.log 轮转（既有）。
- DB 备份加密落对象存储；恢复演练每季度一次（Supabase PITR 或 pg_dump 快照）。
- Sentry（`SENTRY_DSN`）看门狗/背景任务异常面已接（traces 0.2、无 PII）。

## 8. 上传面

- 克隆参考音频：≤20MB（端点硬限）+ 服务端生成的 voice_id 命名（不信客户端文件名）。
- 知识库/话术文本：Pydantic 字段钳制 + 存储侧无路径派生（id 全服务端 uuid）。
