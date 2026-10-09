# Mimosa deep 全库审计分诊记录（2026-10-09）

- **Scan ID**: `scan-2026-10-09T01-41-57.618Z-3ce527fca7f4`
- **Seal**: `sha256:98678940ae59d4f79d280bde2c80870a0976381d31d95ffea416a5f2e65e61ba`
- **产物目录**: `~/.mimosa/security-scans/project-a3e480ea7e08b78e47f4b218/scan-2026-10-09T01-41-57.618Z-3ce527fca7f4`
- **基线**: origin/main @ `587f58e`（detached 快照 `/tmp/bok-audit-587f58e` 扫描后已清理——防主仓 work-* 目录污染与共享 main 中途漂移）
- **方法**: 单点 mimosa deep（整仓单扫保跨文件链）+ 3 个只读 Explore agent 按族并行分诊，32 条 hypotheses 全量逐条亲验（非抽样）
- **结论**: 60 findings（28 static + 32 假设内嵌）——**27 条 static 全部维持既有裁定、32 条假设全量闸在位、真漏洞候选 0**；**唯一真发现=依赖面**：apps/web npm audit 实测 9 vulnerabilities（1 critical+8 high），已随本轮修复 3 项（含 critical），余 6 high=braces→shadcn 构建链 accepted-risk（见 §4）

## §1 三基线对照

| 基线 | 深度 | findings | 构成 | 备注 |
|---|---|---|---|---|
| 2026-10-05 主仓（pre-收口） | normal | 27 | 23 med + 4 low | scripts 污点族 21 条在位 |
| 2026-10-09 PR #234 分支（post-收口） | deep | 39+32 假设 | 2 high + 32 med + 5 low | scripts 污点族 21→3；high 全为 sink 形状误报（就地三验后→0） |
| **本次 main@587f58e** | deep | **60（28 static+32 假设）** | 2 high + 53 med + 5 low | PR #234 未合并态；合并后 scripts 污点 21 条与 markdown_source/read 链预期消失 |

族级命中（mimosa_triage 门）：`fixed-scripts-cp-api-entries` 21 / `fp-cp-middleware-business` 32 / `fp-scripts-random` 5 / `fp-queue-proxy-room-claim-taint` 1 / `accepted-knowledge-source-fetch` 1 = **零未审增量**。

## §2 CP 闸族（32 条 hypotheses 全量：2 high + 30 medium）——分诊 A

**32/32 闸在位，0 缺失**。每条 handler 首部或状态变更前命中至少一道函数体闸（`_gate_page`/`auto_gate_management`/`require_role`/`deny_cross_account`/`deny_foreign_owner`/B3 owner 盖章族）。逐条矩阵存分诊档案（scan 日志），要点：

- **high×2 专项**：main.py `tts_minimax_voice_delete`（DELETE minimax-voices）——`auto_gate_management`→settings 键全链在位；voice_id=平台单把 API key 下云侧资源，无租户维度，结构性不适用 deny_cross_account。main.py `qa_canned_audio`（GET qa canned-audio）——`_gate_page("qa")`+`deny_cross_account` 双闸，他账号 404 不泄露存在性，音频读键来自服务端 pregen 状态表且过 `[0-9a-f]{40}` 正则非攻击者可控。均非真漏洞。
- route-gates.json（127 条全 reviewed）与实测闸链一致；两处标签欠声明（`PATCH /api/users/{id}` 少标 user-admin 全套、`/api/token` 少标 supervisor 403+跨账号 404）——**实测恒强于声明**，非缺口。
- `tests/test_route_gate_coverage.py` 为真运行期断言（import main 对比 cp.app.routes），但**不校验函数体内闸调用**——该残差正是引擎盲区，由本次人工逐条补齐。
- 维持 `fp-cp-middleware-business`（false-positive）裁定。

## §3 污点/假链/随机族（28 条 static：21 med taint + 1 med 假链 + 5 low + 1 med knowledge 链）——分诊 B

**27/27 裁定成立，无新真数据流**。要点与勘误：

- main.py `_write_distill_knowledge`→markdown_source `BokMarkdownSource.write` 链：@587f58e 版 read() 无 sink 闸 + `urlopen(headers=)` kwarg 错位（运行时 TypeError fail-closed，功能 bug 非安全洞）；**BokMarkdownSource 全仓零构造点=死适配器**，CP 装配面仅 LocalMarkdownSource（main.py knowledge 装配点）——env→HTTP 抓取链无实例化路径。PR #234 已全修（守卫+urlencode+kwarg）。
- sink 归因漂移（引擎锚到定义行）：`probe_flow_graph:948`/`probe_ambient_keyboard:88` 均为 `async def run_leg` 行，真出站在 :772-776/:90-108。
- `queue_proxy→room_claim`：链不成立铁证——queue_proxy 全文件零 room_claim 引用（报点=LaneGate `acquire` 槽租约），room_claim 唯一 import 方=agent_runtime/agent.py 装配点，锁路径源=claims_dir()+`_ROOM_SAFE` 白名单收敛。
- 5 low 固定种子逐条核实=确定性采样（SEED=20260921/0x5EED/20260917/20261006/--seed），注释自证非加密。
- **单点依赖记录**：erc.cp_request 与 mine_qa._safe_urlopen 的发送期 host 条件含 `or bool(host)`（环回白名被架空），主机约束唯一执行点=import 期 urlguard gate——涉事脚本全部已接 gate；PR #234 合并后由 cp_outbound 严格环回校验接管该层。
- 归档脚本 probe_qa_phonetic 无任何代码执行入口（仅 docs/账本引用）。
- 已回写 suppressions 三处 evidence（knowledge 链死适配器事实/mine_qa 执行体路径/flow_graph 漂移勘误）。

## §4 依赖面 + 覆盖率（真发现所在）——分诊 C

### 4.1 依赖 advisory（真漏洞，已处置）

`npm audit --package-lock-only`（2026-10-09 live registry，apps/web=唯一存续 lock）：

| 包 | 修复前 | 严重度 | 处置 |
|---|---|---|---|
| next | 16.3.3 | **critical**（7 GHSA：ImageResponse RCE + Image Optimization SSRF 等） | **已修**→16.4.0（非破坏） |
| sharp | 0.35.4 | high（librsvg CVE-2026-96889） | **已修**→0.35.5 |
| source-map-js | 1.2.1 | high | **已修**→1.2.2 |
| braces→micromatch→fast-glob→ts-morph（shadcn@4.21.0 链） | 3.0.3 | high×6（栈耗尽 DoS GHSA-vfj7-8cjw-p6xm） | **accepted-risk**：唯一修复路径=shadcn 破坏性降级；暴露面=构建期 codegen 工具链非运行时服务面；shadcn 为仓规钉死版本 |

2026-09-23「两份 lock 0 vulnerabilities」裁定**不维持**（上游期间新发 advisory），dependencyAdvisories 元数据已刷新。修复后 web 验收：tsc 0 错 / npm test 199 pass / build 静态导出成功。

### 4.2 覆盖率

- **选择层缺口≈0%**：coverage selectedFiles=974 ≈ 代码文件 973（py 824/ts 37/tsx 88/js 3/mjs 21）；vendored tojyutping 与 services/s2s fork 均在扫面内（0 finding）。
- **分析深度层缺口**：completeness=**partial**（threatModel/findingDiscovery 两相 partial）；pathAnalysis **traces=0**（零端到端污点追踪——跨文件链全靠人工分诊补）；32 业务假设 100% inconclusive。
- **apps/web TS/TSX**：145 文件进扫（计数推断，parseFailures=0）但 **0 finding**——web 侧分析深度无法从产物证实，属声明边界而非已证盲区。
- **补偿控制缺口**：ci.yml ruff 真 bug 门路径 `apps packages tools scripts`——**services/ 整体不在内**（含 s2s/sidecar）；gitleaks（全历史密钥）与 web tsc/npm test 在位。services/ 纳入 ruff 门=后续候选。

### 4.3 HEAD 凭据面

`git ls-files` 仅 3 个 `.env.example`（dev 默认值/占位符），.gitignore 阻断真实 env；无凭据入库（历史面=gitleaks CI 管辖）。

## §5 在途面（「全库」完整性）

| 来源 | 内容 | 与本审计关系 |
|---|---|---|
| PR #234（本会话） | 出站守卫+收敛+deps 修复+本档 | 合并即消 §3 的 21 条 taint+knowledge 链+read 洞形状 |
| PR #235（他会话，同日） | Mimosa 清零第一波：CP 出站真 allowlist+弱随机 SystemRandom 化 | 同主题并行刀——**合并顺序需协调**（与 #234 的 cp_outbound/random 面可能冲突，后合方 rebase） |
| origin/fp/first-principles-1001 | 830 files ±陈旧分支 | 无 open PR，僵尸候选（另行处置） |
| origin/session-20260919-232858-4eb7 | 1080 files ±9 月旧会话分支 | 同上 |

各在途分支合并后由下轮主仓扫描覆盖。

## §6 真漏洞协议执行记录

本轮触发一次：§4.1 依赖面 critical——已按协议**立即修复**（next/sharp/source-map-js 三项非破坏升级）+ 本档记录 + accepted-risk 项写明理由与暴露面；未进任何 suppressions 静默（braces 链以 accepted-risk 显式记录而非删除）。

## §7 复扫对照基线

本档三类形状（函数体闸盲区 / sink 归因漂移到定义行 / 固定种子确定性采样）并入 2026-10-01 档的已知噪音清单；新 finding 先对照后进人工分诊；净增判定以 `scripts/ops/mimosa_triage.py` 零未审门为准（本轮 60/60 命中）。
