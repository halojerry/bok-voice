# SIP 电话边缘落位界定 v2（薄节点拓扑修订案）

> 日期：2026-09-13 · 状态：方向已确认（待实施计划） · 决策人：halo
> 修订对象：`2026-09-10-thin-node-saas-design.md` §2 拍板 7 / §8「通话通道（拍板：纯 WebRTC，SIP 二期预留）」
> 前置交付：PR#68（外呼战役+名册+官方 SIP 外播 mock 档）已交付系统侧全部编排，本文件只解决「电话边缘放哪、谁管、怎么分期」。

## 1. 总纲（拍板）

**「云端轻面板 + GPU 落客户机房」的方向不变，SIP 边缘嵌进该拓扑而非另起炉灶：**

- **形态 1（正态）**：SIP 边缘 = 节点包**可选组件组**，落客户机房 DMZ/防火墙区。适用：机房有固定公网 IP 或自有 IPPBX/运营商线路的企业客户（外呼客服客户大概率本来就有线路与号码资源）。
- **形态 2（过渡 + 加购）**：SIP 边缘 = 平台运营的独立「电话边缘小站点」（小 VPS）。两个用途：①**自用期**（我们自己是第一个租户，dogfood 部署剧本）；②无公网 IP/无线路/要求全包的客户付费加购 SKU。

哲学校验：形态 1 完全符合「GPU 落客户机房、坐席纯内网」既有拍板——SIP 边缘与 GPU 同机房，只是它必须坐在 DMZ 带公网口；形态 2 不违背「节点出站-only」铁律（见 §3）。

## 2. 关键认识：外呼战役与坐席通话是两条流量面

spec §3 的「纯内网、零公网」红利（PERCEIVED_MS p50=1.82s 底盘）属于**坐席 WebRTC 通话**。外呼战役对端是 PSTN，媒体必须经有公网口的 SIP 网关——内网红利天然不适用，界定不触碰坐席通话的既有拍板：

```text
坐席通话（A 线进线）： 坐席浏览器 ──内网──> 节点 LiveKit <──> GPU worker   （spec §3 现状，不动）
外呼战役（outbound）： PSTN ──> SIP 网关(公网口) ──> 战役房间 ──出站 WS──> GPU worker
```

两条面**共用同一批 GPU worker 与模型 sidecar**，只差「战役房间开在哪个站点的 LiveKit 上」。

## 3. 两种形态的完整界定

| | 形态 1：边缘落客户机房（正态） | 形态 2：平台电话边缘站点（过渡+加购） |
|---|---|---|
| 内容物 | 节点包可选组件组：**Redis + livekit-sip**（livekit-sip 经 Redis 耦合站点 LiveKit，硬依赖——spec §9「同站点第二个组件才需要 Redis」的表述修订为「加 SIP 即加 Redis」）+ trunk 配置 + `--sip-edge` 拓扑开关 | 香港/同城小 VPS：Redis + livekit-sip + **独立 LiveKit 站点**；与客户节点的关系二选一：a) WireGuard 拉回客户站点（SIP 网关成为客户集群的远程组件）；b) 战役房间整个开在云站点、客户 GPU worker **出站 WS 注册到云站点**接 campaign job（推荐——worker 本就不需要入站端口，spec §3 已确认该性质） |
| 端口面 | 公网开 5060 + RTP 10000-20000/UDP（DMZ/防火墙区，客户 IT 管） | 同左（VPS 安全组），VPS 其余端口不对公网 |
| 号码/中继/合规 | **客户的**：线路费、号码、实名、反骚扰合规全归客户 | **平台的**：中继费打包进服务费（利润项+黏性）；平台按 org 隔离号码池与用量 |
| 治理 | 属节点包 → node_token 注册、心跳、L1/L2/L3 熔断、teardown-org（连 SIP 凭据一起擦）天然全覆盖 | 注册为「电话边缘节点」（同一套 node 治理）；P4 前**每 org 专属不共享**；出站-only 原则不破（worker/媒体全为客户侧出站或 PSTN 入站） |
| 媒体路径 | PSTN→客户机房 SIP→节点 LiveKit→GPU，零额外公网跳 | PSTN→VPS→云 LiveKit→（出站 WS）客户 GPU，多一跳 VPS↔客户机房（外呼可接受：客户原声本就从 PSTN 进来，内网红利本就不在战役面） |
| VAD 旁听/坐席介入 | 战役通话无坐席浏览器参与者；supervisor 旁听经 CP 签 token 连对应站点 LiveKit（站点 URL 由 CP 返回，契约不变） | 同左，跨站点走公网（supervisor 旁听是低频运维动作，可接受） |

**自用期定位**：Mac GPU 节点 + 香港 VPS 电话边缘 = 形态 2 的单租户 dogfood，踩熟的部署剧本将来即形态 2 的标准交付物。

## 4. CP/云端轻面板增量（sites 维度——本界定的核心数据模型工作）

- 新增 **`sites`** 概念：每节点集群/电话边缘站点 = `{site_id, org_id, livekit_url, sip_edge: none|local|cloud, trunk_id, numbers[], region}`。
- CP 本就是官方 TokenSource 契约端点，唯一变化是**按通话类型选站点**：坐席通话 token 的 `serverUrl` 指节点站点；战役通话由 campaign 循环按 `site_id` 路由 dispatcher（`create_dispatch` 目标站点）。
- PR#68 的 `campaigns` 加 `site_id`（dispatcher 目标从单一 LiveKit 扩为按 site 查表，改动小）；`call_sessions` 加同款缝。
- 形态 2 下中继用量计费（分钟×org×号码池）入 P2 看板 / P4 计费顺路项。
- `GET /api/token` / `/api/campaigns` 响应带站点信息，web 端 CallStudio/战役页透传展示。

## 5. 节点包增量

- `bok.py serve` 拓扑开关 `--sip-edge`：拉起 Redis + livekit-sip（config.yaml 用站点 api_key/secret，`use_external_ip`），健康轮询纳入 serve 现有自愈骨架；`prod install`（launchd）/`install-node.ps1` 各加对应服务单元。
- livekit-sip 为 Go 上游二进制（Apache-2.0，`mage build` 自编译，无官方预编译包）：进 §11.2 安装脚本组件清单与 §11.3「上游二进制无可保护」类别（Nuitka 保护面不涉及）；Windows CUDA / macOS 两档 CI 产物矩阵各带一份。
- trunk 凭据治理与 MiniMax key 同款：CP 引导下发、本地加密存、**L1 吊销联动**（熔断即拨号哑火）、teardown-org 连凭据擦除。
- 形态 2 站点复用同一 node-agent 守护与心跳（`node_type: telephony_edge`），无 GPU 无模型，健康检查项=5060 监听/RTP 端口段/Redis。

## 6. 前置门（不变，且是唯一硬闸门）

spec §2 拍板 7 原文有效：**真中继启用前必须过 8kHz 窄带重验**——运营商音频为 PCMU/PCMA 8kHz，现有 16kHz ASR 栈的句级提交/热词/数字保护/拆句组装全部重测。执行方式：

- 专项先于任何真中继配置；PR#68 的 mock 档是现成测试床（`mock_callee` 加 8kHz 重采样合成档即可模拟窄带话音，先在 mock 档重跑三语/数字/拆句探针，再上真中继）。
- 真中继数据（真实客户话音 8kHz 录音样本）回填喂重验——外呼自用期顺路产生。

### 6.1 mock 档实测（2026-09-15，第一轮）

测试床：`mock_callee --narrowband`（3.4kHz 6 阶 Butterworth 抗混叠 → 抽降至 8k → 升回 16k 推流，模拟电话频带损失；不含 G.711 8bit 量化噪声）。探针：`scripts/probe_8khz_asr.py`——**真链路**（CP 建最小战役 → agent 读 dial 块 `narrowband` → mock 档窄带推流 → agent 真 ASR 转写 → `/api/calls/{id}/turns` 取 customer 转写 → difflib 字符准确率 + 号码逐位比对）。宽/窄同词同音色各 4 腿 × 2 轮，取各档较好轮次：

| 腿（语言） | 台词 | 宽档 | 窄档 |
|---|---|---|---|
| zh-sentence（zh） | 你好我是快递公司的专员 + 好的再见 | **1.000** | **1.000** |
| en-sentence（en） | Let me check that for you + okay bye bye | **0.933** | **0.933** |
| hotword（zh，模板热词 拼多多/京东/下单） | 拼多多京东下单 + 好的再见 | **0.917** | **0.917** |
| cantonese-number（cantonese） | 我WhatsApp係 + 六四三二零一一一 + 好嘅再見 | 0.564 | 0.579 |

- **号码句窄带逐位全对**：`64320111` 逐位命中，宽/窄两档 4/4 轮均如此。
- **门禁结论：过** —— `8KHZ_PROBE wide_acc=0.950 narrow_acc=0.950 digit_ok=1 gate=PASS`（三语句窄带准确率 0.950 ≥ 0.90 且号码句逐位全对）；窄带档真伪核验 8/8 腿（每通窄腿的 `mock_callee` 日志都有 `narrowband=1` 自报，排除「窄带档没真跑」的假绿）。
- 三条语句腿的窄档转写与宽档**逐字一致**（±0.000）：3.4kHz 电话频带对本 ASR 栈的句级提交 / 热词软偏置（拼多多、京东、下单全中）/ 数字保护没有可测退化。
- cantonese 腿的两句短句（「我WhatsApp係」「好嘅再見」）在**宽窄两档同样**听岔（"我。惨唔系" / "我。单号"），与窄带无关（本地 TTS 混排短句 + ASR 组合问题）——该腿不进三语句分母，只作号码逐位比对。
- 局限：mock 档只模拟带宽损失，未模拟 G.711 8bit 对数量化噪声与真线路抖动；真中继样本到位后按本节口径回填复核（外呼自用期顺路产出）。

## 7. 分期（对 spec §10 的增量）

| 期 | 内容 |
|---|---|
| P0-P1（现行） | 不动。mock 档战役已交付（PR#68），任何拓扑可模拟联调 |
| **P1.5（新增：自用外呼上线）** | 香港 VPS 电话边缘（形态 2 单租户）+ `sites` 维度最小版 + campaign 按 site 路由 + trunk 商开户配置（推荐 Telnyx，备 DIDWW/Plivo）+ **8kHz ASR 重验专项**（mock 档先测床）——自用外呼业务先跑通，同时产出真话音数据 |
| P2 | 节点包 `--sip-edge` 组件组进发行管线；形态 1 成为客户站点部署选项（安装脚本/doctor/节点就绪单扩展） |
| P4 | 电话边缘多租户共享（形态 2 有规模需求时）、中继计费、号码池按 org 隔离硬化 |

## 8. 明确不做（YAGNI）

- 给客户 Mac/机房做端口转发/VPN 打洞跑 SIP（脚枪：SDP 公网地址、万端口段 NAT、CGNAT——形态 2 的 VPS 就是为避免它存在的）。
- 平台侧买号码资源做形态 1 客户的转售（合规与号码资源归属客户；平台只做我们自用的形态 2）。
- 入呼（inbound）客服热线（A 线现状是外呼+坐席进线；入呼 dispatch rules 列真需求出现后再议）。
- AMD 答录机检测（沿用 PR#68 决策：自部署需自带 LLM/STT 验质量，香港场景渗透低，deferred）。
