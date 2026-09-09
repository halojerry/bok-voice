# Bok Voice 分发形态与部署拓扑设计：薄节点 SaaS（本地 GPU + 云端轻面板）

> 日期：2026-09-10 · 状态：已批准（待实施计划） · 决策人：halo
> 本文档是分发形态讨论（2026-09-10）的最终规格，取代「纯云端 SaaS」与「桌面客户端直发」两条路线。

## 1. 背景与目标

现状是全栈单机桌面 App（Tauri 壳拉起 CP/LiveKit/ASR/LLM/worker，数据落本机 app-data）。
商业化需要多坐席形态：

- **坐席 100-200 人，峰值同时通话 20-60 路**（口径已钉死：按坐席数，不按满负荷路数）。
- 先服务**一家大客户**，但架构从第一天带 `org_id` 缝（见 §7，老板管理员需求使 org 边界成为首日刚需）。
- 开发栈维持 Mac（mlx），**生产部署接受 CUDA 环境**。
- 目标形态（用户拍板）：**GPU 部署在客户本地机房，云端只留轻面板与数据流程，支持远程删除，坐席纯 WebUI 访问**。

## 2. 决策记录

| # | 问题 | 拍板 |
|---|---|---|
| 1 | 并发口径 | 坐席 100-200 人，峰值 20-60 路 |
| 2 | 通话数据可否离开客户场所 | 可以，放我们云上（推翻早期「全云推理已否」的旧案，当时的隐私理由对目标客户非刚性） |
| 3 | 客户结构 | 先一家大客户；架构带租户缝 |
| 4 | 生产硬件 | 接受 CUDA；Mac Studio 为备选档 |
| 5 | 部署主体 | 控制面云端 + 推理可分位；最终收敛为「GPU 本地 + 云端轻面板」薄节点变体 |
| 6 | 远程删除权限 | **仅 super_admin 独占**，目的=防客户不续费；org 侧零删除权限 |
| 7 | 通话通道 | 纯 WebRTC（客户点链接/坐席代拨）；SIP 官方栈列二期预留（8kHz ASR 重验为前置门） |
| 8 | 站点 TLS 策略 | 纯内网 + 节点本地 HTTP 托管坐席 UI（B 线双端也在本地）；远程需求出现才升级域名+证书档 |

## 3. 总体拓扑

```text
坐席浏览器 ×100-200 ──── 管理员浏览器（客户老板）/ 平台超管（/admin 域）
   │                                  │
   │ ① HTTPS：登录/业务 API/话术/对象/审计          │
   │ ② WebRTC：语音媒体（办公室场景=内网直达）       │
   ▼                                  ▼
┌───────────── 云端轻面板（我们运营）──────────────┐
│ CP（FastAPI 容器，PaaS 托管，非裸 VPS）           │
│ Postgres（Supabase 托管）：org/账号/角色/对象/     │
│   话术/知识库/通话/turns/审计/节点注册表/license   │
│ 静态 web UI 托管（现有 Next.js 导出直接上）        │
│ 节点心跳/指令通道（熔断、删除、升级下发）           │
└──────┬───────────────────────────▲─────────────┘
       │ ③ 出站 HTTPS：数据回传/心跳/指令轮询         │
       │                            │（同办公室=内网 WebRTC，零公网跳）
┌──────┴─────────── 客户机房 ────────┴────────────┐
│ GPU 节点 ×1-2（CUDA 主推，Mac Studio 备选）        │
│  LiveKit（媒体）+ agent worker ×N + ASR/LLM      │
│  node-agent 守护（进程编排/自愈/心跳/删除执行/升级）│
│  → MiniMax 云 TTS（现状不变，key 云端下发可吊销）   │
└─────────────────────────────────────────────────┘
```

三条流量线各走各的（另加第 0 条：**坐席 UI 静态资源=节点本地 HTTP 托管**，见 §8 TLS 策略）：

1. **坐席业务操作**（登录/对象/建通话/看板）→ 云 CP。
2. **语音媒体**（WebRTC）→ 客户机房 LiveKit——坐席与节点同办公室时全程内网，**现有 PERCEIVED_MS p50=1.82s 的延迟底盘原样保留**。
3. **节点回传**：仅出站 HTTPS（turns/审计写云库、心跳、轮询指令）。云端永不反向连入节点——天然穿 NAT，节点侧无需入站白名单（官方确认：agent server 注册接 job 不需要任何入站端口）。

**架构红利（为什么工作量比想象小）**：

- LiveKit agent 分发机制天然是「可插拔推理节点」编排——worker 注册到 LiveKit、`RoomAgentDispatch` 自动分配，扩容=加节点，零新路由代码。「推理可分位」只是 CP 签 token 时 `serverUrl` 指向哪个 LiveKit 的区别（CP 已是官方 TokenSource 契约端点）。
- agent worker 本来就是调 CP API 写 turns/审计/结算——CP URL 从 `127.0.0.1:8000` 换成云端地址，业务数据即实时落云。

### 3.1 数据落点清单（什么、存哪、过不过公网）

登录/建通话走云端（图：坐席→CP 拿 JWT→CP 签 LiveKit token→坐席 WebRTC 直连机房）；**唯一真源=云端 Postgres，机房零业务驻留**。

| 数据 | 存哪里 | 过公网？ |
|---|---|---|
| 账号/角色/组织 | 云 Postgres | 是（HTTPS） |
| 对象/话术/知识库 | 云 Postgres+Storage | 是（HTTPS） |
| 通话转写+AI回复+审计+结算 | 云 Postgres（实时） | 是（HTTPS） |
| **语音音频包** | 不落盘，内网浏览器↔LiveKit | **否** |
| **ASR/LLM 推理** | 机房 GPU | **否** |
| 回复文本（TTS 待合成） | MiniMax 云（09-04 既有设计：客户听到的每句回复文字都过 MiniMax，但客户原声转写/话术/资料不经它） | 是 |
| 罐头音频缓存/日志 | 机房本地磁盘（易失，L3a 可擦） | 否 |
| 心跳/负载指标 | 云 CP（出站上报） | 是（HTTPS） |

断网语义推论：音频+推理都在机房，在途通话能继续讲完；新登录/建通话/看板全停（必须联网铁律成立）。

## 4. 本地薄节点

### 4.1 内容物与数据驻留

- 内容 = 现 `bok.py serve` 编排的那套**去掉 CP 与 SQLite**：LiveKit + ASR/LLM sidecar + **A 线 agent worker ×N + B 线 interpreter worker ×2（:8082/8083）+ MT 翻译 server :1236（可选，缺模型自动跳过→B 线回退 :1235，既有行为）** + `node-agent` 守护进程（由 `bok.py serve` 无头演化而来，复用其启动顺序/健康轮询/自愈骨架，新增心跳与指令执行；承接桌面壳职责：进程编排、崩溃自愈、日志轮转——顺手修「日志无轮转」旧审计缺口）。**B 线与 A 线同一节点包、同一运维面**（license/心跳/L1-L3/错误上报/一键部署全复用），GPU 不够的客户可不装 MT server。
- **业务数据零本地驻留**：全部实时落云 Postgres。本地仅易失物（tts-cache、滚动日志）。
- 断网语义：媒体面可继续在途通话，登录/建通话/看板不可用（符合既有「必须联网」铁律）。

### 4.2 停服与远程删除（防不续费的保险丝，super_admin 独占）

| 层级 | 动作 | 触发 | 语义 |
|---|---|---|---|
| L1 | license 心跳熔断 | node-agent 每 60s 上报；连续 3 次失败 | worker 拒接新 job（在途通话不掐），进维护模式 |
| L2 | 租户停用 | super_admin 将 org 置 suspended | 心跳响应携带停服指令，所有节点立即停接 |
| L3a | 节点擦除 | super_admin 签发一次性签名指令（nonce 防重放） | 擦除节点 app-data 全部内容+凭据（模型权重为通用资产默认保留，可选连删），执行后回执 |
| L3b | 租户数据清除 | super_admin 执行 | 云端该 org 全量数据清除（不续费/解约场景；执行时点按合同宽限期条款，设计不锁定天数） |

- **三个键全部只在平台手里**：L1 吊销、L2 停用、L3 签发的权限仅 `super_admin`；`org_admin` 及以下角色无任何对应入口。
- L1/L2 是停服第一线（足以让不续费客户停摆），L3 是终局手段（IP 与凭据保护 + 云数据清除）。
- 双份审计：云端存指令签发记录，节点回执入库，两边时间戳对账。

### 4.3 升级通道

云端下发版本清单 → node-agent 拉包、灰度（先 1 号节点后 2 号）→ 健康检查不过自动回滚。P3 落地前，首客户阶段用 ssh+脚本过渡（明示的临时状态）。

## 5. 云端轻面板

「轻」字边界——只放四类职责：

1. **身份**：账号/角色/JWT（§7）。
2. **业务数据**：对象/话术/知识库/通话/turns/审计（现 CP 的表搬 Supabase Postgres，SQLite 停用）。
3. **编排**：节点注册表/心跳/license/L1-L3 指令通道/版本清单。
4. **静态 web UI** 托管（管理台；坐席工作台由节点本地托管，见 §8）。
5. **备份与告警**：Supabase PITR + 定期逻辑备份；节点离线/错误激增告警（首版=看板红点+错误上报聚合视图，邮件/IM 通知通道二期）。

不碰音频、不做推理、不缓存媒体。100-200 坐席的 API 写入量对托管 Postgres 是零压力，一台小容器足够。

## 6. 数据模型（分析优化的地基）

### 6.1 turns 表升级为结构化对话账本

| 字段 | 说明 |
|---|---|
| `org_id / call_id / seq` | 租户缝 + 通话内顺序 |
| `line` | `a`（客服）/ `b`（同传）——两条业务线同账本 |
| `speaker` | A 线：`customer` / `agent_ai` / `agent_human`（主管接管的人工发言）；B 线：`me` / `other`（源语方/听译方）——会话级区分发言人 |
| `gen` | `llm` / `script`（开场白/心跳/收线直念）/ `filler`（垫话）/ `qa_fastpath`——区分模型生成与罐头 |
| `template_step` | 该轮当时话术步号——话术卡点分析主维度 |
| `text / lang / started_ms / ended_ms` | 内容 + 通话内时间轴 |
| `perceived_ms` | 北极星打点落库（顺手治「turns.latency_ms 被 CP 丢弃」旧审计缺口） |

说话人区分是**结构性白送**：客户话从 ASR 用户轮进来=customer；AI 回复是自己生成=agent_ai；脚本直念=script；主管接管=agent_human。不跑任何声纹/diarization 模型，字面即权威标签。

### 6.2 知识库与录音

- **知识库上云**：vault markdown → Supabase Storage + Postgres 检索索引列；知识蒸馏在云端 settle 时跑。节点擦除（L3a）**不伤知识库**（属 org 云资产）。
- **录音**：留 `call_recordings` 开关位，**默认关**——存储成本与隐私面大，等分析优化确需回听再开。**依赖注明**：录音走官方自托管 **egress 独立服务**（S3 兼容存储；RoomComposite 需 headless Chrome 很重，**音频轨 egress 轻**——录音需求启用时优先 track/audio egress；旧 `StartRoomCompositeEgress` API 已废弃，用 `StartEgress` TemplateSource）。开关打开=节点多跑一个 egress runner 服务。

### 6.3 分析消费面（P2）

老板看板从 turns 表出：通话量/时长分布、话术步卡点、QA 命中率、坐席对比、延迟分布；`tts-mine` QA 挖掘闭环接云端数据。

## 7. RBAC：两级管理员

| 角色 | 范围 | 权限 |
|---|---|---|
| `super_admin`（平台超管，halo） | 跨 org | org 生命周期、节点注册/license、**L1/L2/L3 全部指令**、跨 org 可见；独立 `/admin` 域，建议 TOTP 二次验证；全部操作入审计 |
| `org_admin`（客户老板管理员） | 仅本 org | 员工账号管理、本 org 全部通话/审计/分析看板、话术/知识/对象管理；**不可见**其他 org 与平台层（节点/license/停服/删除） |
| `supervisor` | 仅本 org | 现有暂停/接管/转人工语义 |
| `agent`（坐席） | 仅本人 | 本人通话与数据；对象/话术只读 |

实现：CP 自管账号（argon2 + JWT，token 内嵌 `org_id+role`），所有查询 CP 层按 `org_id` 强制过滤；Postgres RLS 留 P4 多租户硬化再加。节点身份独立：每节点 `node_token`（注册时颁发、可吊销），心跳/指令通道用它认证，与用户 JWT 体系分离。

## 8. 坐席端与 token 流

登录 → CP 发用户 JWT → `POST /api/calls` → CP 查该 org 绑定节点 → `/api/token` 返回 `{serverUrl: 客户机房 LiveKit, participantToken}`。**与现有官方 TokenSource 契约完全一致**，唯一变化是 `serverUrl` 从 `ws://127.0.0.1:7880` 变为站点地址。坐席硬件 = 普通办公 PC + 耳机 + 浏览器，零安装、零 GPU 要求。

### TLS/证书策略（拍板：纯内网 + 节点本地托管 UI）

云端 HTTPS 页面连 `ws://` 会被浏览器 mixed-content 拦死，且官方明确 TURN/TLS 必须**受信 CA 证书（self-signed 不行）**、TURN 还需单独域名+证书。拍板走**纯内网路线**（首客户与 B 线双端都在本地）：

- **坐席 UI 由节点本地 HTTP 托管**（`http://<节点内网地址>:<port>`）——`http://` 页面连 `ws://` LiveKit 合法，零证书、零公网暴露。node-agent 注入运行时配置（`cpUrl`/`livekitUrl`），同一份 Next.js 静态导出两处托管：**云端=管理台（super_admin/org_admin，HTTPS）**，**节点=坐席工作台（A 线 CallStudio + B 线 interpret）**。
- 云 CP 开 CORS（坐席 UI origin 在节点、API 在云端，跨源）。
- **预留升级档**：一旦出现远程/居家坐席或 B 线跨地域端点，该站点升级为「公网域名 + Let's Encrypt 两张证书（主域 + TURN 域）」+ TURN-over-TLS 443（官方部署清单：443/80/7881/3478 UDP/50000-60000 UDP）。部署勘察时逐站点确认。

### 通话通道（拍板：纯 WebRTC，SIP 二期预留）

首客户客户侧=WebRTC 进房（点链接/坐席代拨），规格现状不变。**SIP 列二期预留**（客户要打真手机/固话时启用）：LiveKit 官方自托管 SIP 栈入口=SIP server 自托管（5060 信令 + RTP 10000-20000、`use_external_ip`）+ trunk 供应商（Twilio/Telnyx/Plivo 等）+ 外呼 `CreateSIPParticipant` + 入呼 dispatch rules + 答录机检测 AMD + 电话专用 Krisp 降噪 `BVCTelephony`。**启用前重验项=8kHz 窄带音频 vs 整条 16kHz ASR 栈**（句级提交/热词/数字保护全部要重新过测试）。

## 9. GPU 容量与栈

- 峰值 20-60 路：CUDA 上 4B 模型连续批处理，**1 张 48GB 卡扛 LLM 60 路**；ASR（qwen-asr CUDA）单独计容量。推荐节点配置 1-2 张卡；不够加节点，LiveKit dispatch 自动分流。B 线 MT 模型（Hy-MT2 小模型）与主 LLM 同卡共存、用量小，缺省自动跳过。
- **分流按官方参数落地**：worker `load_fnc` 默认是 5s CPU 均值（阈值 0.7）——GPU 场景必须自定义为**GPU 利用率**（`AgentServer(load_threshold=…)` + `server.load_fnc=…`），否则 GPU 打满 CPU 空闲时 dispatch 照样塞 job。LiveKit server 内建 load-aware round-robin + 单一派发原则。
- **多节点边界**：每站点**恰一个** LiveKit server、N 个 worker 注册（官方：standalone 零外部依赖；同站点起第二个 LiveKit 才需要 Redis——防误扩）。
- **升级 drain 语义官方现成**：SIGTERM → 拒接新 job、在途跑完（Python 生产模式 drain 默认 3600s）——P3 灰度升级直接复用；prewarm 走 `server.setup_fnc`。
- **指标白捡**：LiveKit server/egress 均支持 `prometheus_port` 暴露 `livekit_*` 指标——node-agent 心跳顺手抓，作节点健康与错误上报的数据源。
- Mac Studio 备选档：4-8 路/台 → 3-8 台；胜在全部调优已实证，零延迟重校成本。
- **大技术风险 = CUDA 延迟重校**：PERCEIVED_MS 基线是 Mac 实测；CUDA 侧 prefix cache 行为、ASR 速度、8bit 量化决策全部重验。对策：P0 即打 CUDA 原型节点，跑 `scripts/load_audio_concurrency.py` + `scripts/measure_latency.py` 出基线；**过不了门禁则首客户改用 Mac Studio 档**。给客户承诺的数字以实测为准。
- MiniMax TTS：节点 api_key 由 CP 引导下发（加密存本地、可吊销，与 L1/L2 联动）。

## 10. 分期

| 期 | 内容 |
|---|---|
| P0 地基拆分 | 仓库拆「节点包」（LiveKit+sidecars+workers+node-agent）与「云 CP」；CP 搬 Supabase Postgres；turns 新 schema 落地；节点注册/心跳最小版；CUDA 原型节点打样 + 延迟/负载基线（并行线）；手工部署脚本草版（ps1/install.sh） |
| P1 身份与两级管理员 | orgs/users/roles/JWT、web 登录、token 流按 org 路由、super_admin 最小控制台、org_admin（老板）视图；**错误上报 v1**（模板+fingerprint 聚合+最简看板+浏览器 beacon——部署在客户机房，远程排障是首客户前必备） |
| P2 分析地基与发行管线 | 老板看板（通话量/卡点/QA 命中/坐席对比）+ QA 挖掘接云；**节点发行管线**：Nuitka 编译产物（Windows CUDA + macOS MLX 两档）+ 签名 + `install-node.ps1` 一键部署正式化（在线脚本+离线包）；`verify_bundle.sh` 加「产物必须为编译产物」门禁 |
| P3 熔断与升级 | L1 心跳/license、L2 停用、L3a/L3b、**`teardown-org` 一键删除完整版**（dry-run 清单/离线指令排队/删除回执单）、灰度升级与回滚（此前首客户阶段用 ssh+脚本过渡） |
| P4 多租户硬化（首客户后） | Postgres RLS、配额、计费 |

## 11. 运维工具链与交付加固（2026-09-10 追加拍板）

### 11.1 一键删除 `teardown-org`

工具脚本与管理台按钮同一套 CP 端点，动作独立幂等、中断可续跑：

1. **dry-run（默认先跑）**：打印将删清单（各表行数统计+将触达节点列表），不动数据。
2. **L2 停用 + L1 吊销 node_token**：节点立即停摆。
3. **L3a 签名擦除指令**（nonce 一次性）：指令挂在心跳通道，**节点离线时排队、下次上线即执行并回执**——拔网线逃不掉；控制台显示「已确认擦除 n/m」直至收齐。
4. **L3b 云端数据清除**：`--grace-days` 宽限期（默认按合同条款）。
5. **删除回执单**：删除的表/行数统计、节点回执状态、未确认项清单；签发与执行双份审计。

### 11.2 Windows CUDA 一键部署

`install-node.ps1`（在线）+ 离线包（zip+`install.cmd`，runtime+模型全量，企业内网无外网交付硬要求）：

① 环境体检（nvidia-smi 驱动/CUDA/显存/磁盘/端口占用）→ ② 装 runtime + `bok.py download` 模型（幂等续传）→ ③ `--CP <url> --Token <node_token>` 注册节点拉配置 → ④ 注册 Windows 服务（自启+崩溃重启，对应 Mac launchd KeepAlive）→ ⑤ 防火墙内网接口放行 7880/7881/7882+UDP 段 → ⑥ `doctor` 终检出「节点就绪单」。对称配 uninstall。Mac 档复用 `bok.py prod install`（launchd）。

### 11.3 防反编译（面收缩到节点包）

**CP 上云后不分发**，早期「Nuitka 编译 CP」不再需要；交付物只有节点包。保护对象=agent worker 业务逻辑（flow 引擎/prompt 脚手架/罐头与 QA 逻辑）：

- agent worker + node-agent + sidecar 自研部分全部 Nuitka 编译；CI 出 Windows CUDA + macOS MLX 两档产物。**Nuitka 不支持交叉编译——Windows 产物必须在 Windows CI runner 构建**（GitHub Actions windows-latest），构建矩阵两档并行。
- **模型权重分发渠道**：升级通道只覆盖代码；GB 级模型权重走离线包内置（首版）或云端 CDN 按需下载（客户带宽允许时），安装脚本两路都支持。
- **prompt 真源在云端 DB**（运行时 HTTPS 拉取装配），二进制只含脚手架=最小暴露。**逐轮响应零影响**：prompt 是每通一次装配（建通话拿 token 的同一班车，增量仅一次 HTTPS 往返中多几 KB，落在接通准备上），逐轮生成靠 KV-cache 已有前缀、字节整通冻结（尾部冻结重放前提不变）；可选节点按 `(org, template_id, revision)` 缓存模板，未改版不重复拉。云端真源的收益=改话术全局下一通生效。
- 上游二进制（llama-server/livekit/ASR 引擎）无可保护。
- 门禁：`verify_bundle.sh` 校验交付产物为编译产物，明文 Python 不进客户机房。
- 诚实边界：提高逆向成本非绝对（内存 dump 可取运行时拼装结果）；真正防线是 L1/L2 熔断——盗版二进制会哑火。

### 11.4 错误上报（P1 提前）

远程排障是首客户前必备（P3 升级通道落地前更依赖）。事件模板四段：

```text
【环境指纹】event_id/ts_node/ts_utc/node_id/org_id/node_version(构建号)/
  os+版本/GPU+显存/driver/CUDA/引擎版本/uptime_s/当前并发+近1h峰值
【错误本体】severity(fatal|error|warn)/category(asr|llm|tts|dispatch|livekit|node|cpsync)/
  error_type/fingerprint(归一化stack sha1,同指纹聚合防刷屏)/message/stack(路径归一)
【复现钥匙】call_id/room/call_language/template_id/flow_step/
  事发前操作序列(近10事件)/metrics时间线(事发前120s: PERCEIVED_MS/ASR_MS/
  LLM_TTFT_MS(cached=N/M)/TTS_FIRST_AUDIO_MS/MINIMAX_BIDI_PERF/partial抑制态/
  抢跑命中率——直接读现有打点环形缓冲)/日志尾(category过滤,脱敏,近200行)
【聚合】repeat_count/first_seen/related_event_ids
```

- 端点 `/api/telemetry/errors`（node_token 认证，批量）；坐席浏览器 JS 错误走 beacon（无浏览器侧报错则「挂断不结算」类 bug 无法复现）。
- 看板按 fingerprint 聚合、按 org/node/version 切；super_admin 全局、org_admin 本 org。
- **铁律：上报永不阻塞通话链路**——异步队列、失败静默重试有上限、队列满丢弃保通话。

## 12. 风险清单

1. **CUDA 延迟重校不过关** → Mac Studio 备选档兜底，P0 原型先行验证。
2. **云 CP 单点**：CP 挂=不能登录/建通话/看板，在途通话媒体面不受影响 → CP 多实例 + 托管 Postgres 高可用。
3. **站点网络**：拍板纯内网+节点本地托管 UI，零证书零公网；**远程坐席/B 线跨地域端点出现时**该站点须升级「公网域名+受信证书+TURN」档——部署前勘察确认无隐藏远程需求。
4. **版本碎片**：升级通道 P3 前用 ssh+脚本，明确为临时态；一键部署/升级正式化后节点版本可观测（心跳带 node_version）。
5. **安全**：节点通信全 TLS + node_token；L3 指令一次性 nonce 防重放；super_admin 全操作审计。
6. **离线交付**：首客户机房可能无外网，离线包须在干净 Windows 机器上预演安装（含模型全量与驱动缺失的失败路径文案）。

## 13. 明确不做（YAGNI）

- 坐席桌面客户端分发（浏览器已覆盖；Tauri 壳保留给开发形态）。
- 声纹/diarization 模型（结构性说话人标签已权威）。
- 音频录音默认存档（开关位保留，默认关）。
- 多租户 RLS/配额/计费（P4）。
