# AGENTS.md — Bok Voice

本地优先语音客服（A 线）+ 实时同传工作台（B 线）。

## 第一性原理（优先于一切）

从原始需求和问题本质出发，不从惯例或模板出发。

1. 不要假设我清楚自己想要什么。动机或目标不清晰时，停下来讨论。
2. 目标清晰但路径不是最短的，直接告诉我并建议更好的办法。
3. 遇到问题追根因，不打补丁。每个决策都要能回答"为什么"。
4. 输出说重点，砍掉一切不改变决策的信息。

## 红线

- 简单优先：最少代码解决问题，不加未被要求的功能、抽象、配置。
- 外科手术式修改：只改任务直接相关的文件和行；重构必须显式立项，不顺手重构。
- 向本文档标准收敛：与现状冲突时小步走，不复制旧债，也不就地大改。
- 先问再改，触发清单：新依赖、公共接口变更、DB 结构、新增后台/定时任务、
  鉴权与权限、流式协议与 WebSocket/RTC 握手、延迟预算与超时默认值、探针语义。
- 跨 A/B 线改动必须显式声明：受影响模块、对应探针、延迟预算；
  packages/core 是共享契约层，默认波及双线。
- 新增异步/队列/流式/回调/重试/超时路径必须带可观测点（日志行或探针），
  不得静默失败，否则不算完成。
- 禁止硬编码密钥、URL、超时、分页大小；入配置或常量。
- 禁止吞异常；禁止遗留 print / TODO / FIXME。
- 删除公共接口、测试、文档、配置前必须确认。
- 单次改动 >10 文件或 >500 行，暂停确认。
- 修改行为必须补测试；提交前过 ruff、类型检查、测试。
- 接口、数据模型、流程、配置变化，同步更新 docs；
  探针/延迟预算/拓扑变更同步各自治理文档（RUNBOOK / LATENCY_BUDGETS /
  RUNTIME_TOPOLOGY），翻案级决策才进 docs/decisions.md。
- 禁止提交密钥 / Token / 连接串；凭据只走 env 或设置面。
- 完成报告必须贴出实际执行的验证命令与输出摘要（计数 / PASS/FAIL），
  不得只写「已通过」；摘要中不得出现凭据 / 密钥 / Token 值——先掩码或删除再贴。
- 治理文档缺失或过时不是绕过理由：找不到对应规则时停下来问，不自行推断。

## 排查与定位

- 开工四件证据：完整报错/堆栈、复现步骤、1-3 个直接相关文件、预期 vs 实际。缺件先补齐。
- 先诊断后改码：给出最可能的 3 个原因 + 每个的验证方式，确认后再修。
- 一次只验证一个假设；最小修复 + 回归验证，修一个不许坏三个。
- 根因分析开新会话：只带蒸馏结论（结论、探针名、基线读数），不带失败过程。
- 文档盲区用结构化日志补：函数入口/分支/返回值记输入、输出、错误码、调用链。
- 本仓一半的 bug 不抛异常（延迟回归、吞句、走错车道）——timing 账本就是报错信息：
  agent.log 的 PERCEIVED_MS / LLM_TTFT_MS / TTS_FIRST_AUDIO_MS 行，
  预算数字以 docs/LATENCY_BUDGETS.md 为准；超预算的变更默认不接受，除非显式立项。
- 症状 → 去处唯一入口 = docs/RUNBOOK.md（探针/测试/回退开关一页表）；
  仓内行话（垫话、吞句、车道等）释义与对应探针同在该表。
- 复现优先用 scripts/probes/ 探针，逐条跑，不并行。

## 代码风格

- Python：PEP 8、4 空格、`from __future__ import annotations`、签名带 type hints。
- Ruff 是唯一 lint（pyproject select=E/F/W/I、line-length=120）；真 bug 类
  （F821/F811/F601/F702/E722/E713/E714）全树恒绿，CI 强制；
  风格类只约束新增 .py。pre-commit 可选装（真 bug 层 + Conventional commit 校验）。
- TS/React 随周围组件写法；bash 一律 `set -euo pipefail`。
- 语言三态只有 zh / cantonese / en（cantonese 小写；门禁
  tests/test_cantonese_terminology.py）；新增语言必须先立项。
- 新增 env 开关必须登记 tools/bokctl/env.py 的 `_FORWARD_ENV` 单点表
  （门禁 tests/test_forward_env.py，漏登记 = prod 静默死门）。

## 测试与验收

- 提交底线：全量 pytest + compileall + web tsc/build。
- Never fake-green：A 线 E2E 用真 /api/token；探活打真端点。
- merge gate = pytest 全绿 + web 构建 + Launcher smoke。
- 回归资产映射（改动面 → 必跑探针）在 docs/RUNBOOK.md；改流程/打断/垫话/
  延迟/热词必须跑对应探针，离线单测不算验收。
- PR 描述必须含：改动说明、实际执行的命令与输出摘要、风险说明。

## CI/CD

- 三通道 required contexts：Python checks（pytest + scripts import smoke）/
  Web typecheck + export / Launcher smoke。删 CI job 必须同步删分支保护 required context。
- 发布：本地全量验收不过不 tag；tag → release.yml node-pkg 冒烟。
- doc 门禁三件：`scripts/ops/check_doc_paths.py`（断链只减不增）/
  `scripts/ops/check_doc_anchors.py` 锚棘轮（增长须 `--update` 认账重录）/
  scripts README 索引双向钉。门禁失败时修文档本体，不得跳过检查；
  `--update` 只用于合法重录，不是绕过手段。
- gitleaks 扫全历史；postgres-smoke 真 PG 幂等二跑。

## 命令

| 动作 | 命令 |
|---|---|
| 装依赖 | ./scripts/bootstrap.sh |
| Lint（真 bug 类，全树恒绿） | .venv312/bin/python -m ruff check --no-cache --select F821,F811,F601,F702,E722,E713,E714 apps packages tools scripts services |
| 全量测试 | .venv312/bin/python -m pytest -q |
| 语法检查 | python -m compileall -q apps packages services tools scripts |
| 起栈 / 停栈 | python tools/bok.py serve / down |
| 栈状态 / 体检 | python tools/bok.py status / doctor |
| web 构建 | cd apps/web && npx tsc --noEmit && npm run build |

命令不存在、退出非零、或输出与预期不符：暂停并询问，不得编造、重试掩盖或跳过。
风格类规则（E501/F401/I001…）只对新增 .py 强制（CI diff-filter=A 步）；存量风格债不整体清。

## 事实（环境物理）

- worker 长命进程：改 apps/agent 必须重启栈（bok down && bok serve）才生效。
- A 线 = apps/agent/agent.py，B 线 = apps/agent/interpret.py；跨模块契约住 packages/core 单源。
- web 是纯浏览器静态导出：零动态段、深链走 query 参数、不直连 DB。
- DB 迁移唯一授权路径 = apps/control-plane/control_plane/deps.py build_engine() 幂等段。
- 改运行时行为前，先读 docs/RUNTIME_TOPOLOGY.md。

## 文档

- 症状排查入口 → docs/RUNBOOK.md
- 病灶与修复路线 → docs/ARCHITECTURE-MAP.md
- 决策索引 → docs/decisions.md
- 延迟预算 → docs/LATENCY_BUDGETS.md
- 历史全量（2026-10-09 前，含波次纪要/LiveKit 决策/生产姿态）→ docs/archive/AGENTS-2026-10-09.md
- 过程叙事在 git 史与 docs/superpowers/plans/，不回填本文件。

## 维护

- AI 改乱一次，教训补进红线。
- 结构重构后同步更新本文件。
- 通用规则住根；模块规则在模块治理完成后才立子目录 AGENTS.md。
