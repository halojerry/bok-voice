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

三条流量线各走各的：

1. **坐席业务操作**（登录/对象/建通话/看板）→ 云 CP。
2. **语音媒体**（WebRTC）→ 客户机房 LiveKit——坐席与节点同办公室时全程内网，**现有 PERCEIVED_MS p50=1.82s 的延迟底盘原样保留**。
3. **节点回传**：仅出站 HTTPS（turns/审计写云库、心跳、轮询指令）。云端永不反向连入节点——天然穿 NAT，节点侧无需入站白名单。

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

- 内容 = 现 `bok.py serve` 编排的那套**去掉 CP 与 SQLite**：LiveKit + ASR/LLM sidecar + agent worker ×N + `node-agent` 守护进程（由 `bok.py serve` 无头演化而来，复用其启动顺序/健康轮询/自愈骨架，新增心跳与指令执行；承接桌面壳职责：进程编排、崩溃自愈、日志轮转——顺手修「日志无轮转」旧审计缺口）。
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
4. **静态 web UI** 托管。

不碰音频、不做推理、不缓存媒体。100-200 坐席的 API 写入量对托管 Postgres 是零压力，一台小容器足够。

## 6. 数据模型（分析优化的地基）

### 6.1 turns 表升级为结构化对话账本

| 字段 | 说明 |
|---|---|
| `org_id / call_id / seq` | 租户缝 + 通话内顺序 |
| `speaker` | `customer` / `agent_ai` / `agent_human`（主管接管的人工发言）——会话级区分发言人 |
| `gen` | `llm` / `script`（开场白/心跳/收线直念）/ `filler`（垫话）/ `qa_fastpath`——区分模型生成与罐头 |
| `template_step` | 该轮当时话术步号——话术卡点分析主维度 |
| `text / lang / started_ms / ended_ms` | 内容 + 通话内时间轴 |
| `perceived_ms` | 北极星打点落库（顺手治「turns.latency_ms 被 CP 丢弃」旧审计缺口） |

说话人区分是**结构性白送**：客户话从 ASR 用户轮进来=customer；AI 回复是自己生成=agent_ai；脚本直念=script；主管接管=agent_human。不跑任何声纹/diarization 模型，字面即权威标签。

### 6.2 知识库与录音

- **知识库上云**：vault markdown → Supabase Storage + Postgres 检索索引列；知识蒸馏在云端 settle 时跑。节点擦除（L3a）**不伤知识库**（属 org 云资产）。
- **录音**：留 `call_recordings` 开关位（节点上传 Supabase Storage），**默认关**——存储成本与隐私面大，等分析优化确需回听再开。

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

网络两路（写进部署要求，不在代码里解决）：

- **办公室场景（首客户默认）**：媒体走内网，站点零公网暴露。
- **远程/居家坐席**：站点 LiveKit 需公网可达（UDP 端口段转发或 TURN-over-TLS 443）。

## 9. GPU 容量与栈

- 峰值 20-60 路：CUDA 上 4B 模型连续批处理，**1 张 48GB 卡扛 LLM 60 路**；ASR（qwen-asr CUDA）单独计容量。推荐节点配置 1-2 张卡；不够加节点，LiveKit dispatch 自动分流。
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

- agent worker + node-agent + sidecar 自研部分全部 Nuitka 编译；CI 出 Windows CUDA + macOS MLX 两档产物。
- **prompt 真源在云端 DB**（运行时 HTTPS 拉取装配），二进制只含脚手架=最小暴露。
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
3. **站点网络**：远程坐席需公网媒体端口/TURN；办公室场景零要求——部署前勘察。
4. **版本碎片**：升级通道 P3 前用 ssh+脚本，明确为临时态；一键部署/升级正式化后节点版本可观测（心跳带 node_version）。
5. **安全**：节点通信全 TLS + node_token；L3 指令一次性 nonce 防重放；super_admin 全操作审计。
6. **离线交付**：首客户机房可能无外网，离线包须在干净 Windows 机器上预演安装（含模型全量与驱动缺失的失败路径文案）。

## 13. 明确不做（YAGNI）

- 坐席桌面客户端分发（浏览器已覆盖；Tauri 壳保留给 B 线同传与开发形态）。
- 声纹/diarization 模型（结构性说话人标签已权威）。
- 音频录音默认存档（开关位保留，默认关）。
- 多租户 RLS/配额/计费（P4）。
- B 线同传上云（首客户范围外，桌面形态保留）。
