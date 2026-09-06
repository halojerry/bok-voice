# 全链路回归 + E2E + 压测 + 可审计闭环 实施计划

> For agentic workers: REQUIRED SUB-SKILL superpowers:executing-plans。Steps 用 checkbox 跟踪。

**Goal**: A/B 双链路全量回归 + 真实输入端到端 + 并发/长稳压测 + 知识库复盘 + 审计闭环。
**Architecture**: 复用现有 8 个 E2E 脚本为回归基线；补 A 线边角 E2E 与 B 线 v2 E2E 两个新脚本；
并发/soak 各一新脚本；审计缺口（turns 落库/轮级延迟/查询面/日志轮转/usage_records）独立 PR；
知识库复盘只评审不改码。基线 = main 4a709db（#17+#18 已合）。
**Tech Stack**: Python 3.12 (.venv312) / livekit-rtc 客户端 / pytest / node --test / bok.py 栈。

## Global Constraints
- E2E 永不 fake-green：A 线必须真实 /api/token（E2E_SELF_TOKEN=1 仅 debug）。
- 术语门禁：新增旧粤语拼写字面量即测试失败。
- 主工作区另一会话 WIP 勿动；测试全部在 va-qa worktree。
- 栈端口：CP 8000 / web 3000 / ASR 8787 / TTS 8788 / LLM 1235 / MT 1236 / b-line 8790 / livekit 7880 / worker 8081-8083。

## Tasks
- [ ] T0 基线：合 #17/#18；va-qa worktree + runtime symlink；down && serve；status 全绿
- [ ] T1.1 单测门：compileall + pytest 全量 + npm test
- [ ] T1.2 A 线 E2E：trilingual(逐语) / multi_turn / flow_scenario / pipeline / http
- [ ] T1.3 scripts/e2e_edge_cases.py：空输入/超长/符号数字/打断/短应承×3/强挂断
- [ ] T1.4 scripts/e2e_interpret.py：B 线双向/字幕/turns 落库/settle/回退/启停×5/长流
- [ ] T1.5 AB 交叉：并行通话不串号；A 线中途换语言钉定不漂
- [ ] T2.1 延迟基线：measure_latency + flow_scenario p50/p95 + llm_cache_report
- [ ] T2.2 scripts/loadtest_calls.py：N=2/4 并发，TTFT p95 ≤2× 基线
- [ ] T2.3 scripts/soak_test.py：40 通采样 RSS/fd/线程/连接数
- [ ] T2.4 错误注入：kill ASR sidecar / 拔 MiniMax 凭据各一通
- [ ] T3.x 审计 PR（feat/turn-auditability）：turns 补全+language 列、B 线拆行、轮级延迟落库、metrics 端点、web 延迟摘要、日志轮转、usage_records 写入
- [ ] T4 docs/KB_REVIEW.md：蒸馏链验证 + 7 问题坐实 + P0-P2 方向
- [ ] T5 docs/QA-REPORT-2026-09-07.md + PR 交付
