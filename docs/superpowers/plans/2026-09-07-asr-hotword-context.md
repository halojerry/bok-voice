# ASR 热词/Context 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans。Steps 用 checkbox 语法。规格：`docs/superpowers/specs/2026-09-07-asr-hotword-context-design.md`。

**Goal:** Qwen3-ASR 官方 context（system message）通道接通：话术领域词+对象文字字段作热词软偏置，降领域词误听；两级 kill-switch；延迟护栏。

**Architecture:** sidecar `/api/start` 收 context 存 session、三个 mlx generate 点透传 `system_prompt`；agent 装配时 `asr_hotword_context(lang, object_card)` 纯函数拼词表，`Qwen3ASRSTT` 两条 /api/start 路径带 context。分支 `feat/asr-hotword-context`（基于 main）。

**Tech:** Python 3.12、FastAPI（sidecar）、mlx_audio、pytest。

## Global Constraints
- compileall + 术语门禁 + kill-switch 保留（`BOK_ASR_HOTWORDS` / `QWEN3_ASR_CONTEXT`）。
- 数字串不进热词；context ≤120 字符；`Vocabulary: …` 官方示例格式。

### Task 1: sidecar context 通道
- 红测试 `tests/test_asr_hotword_context.py`（fake model 记录 generate kwargs）：start 存 context / partial 与 finish 的 generate 收到 `system_prompt=context` / `QWEN3_ASR_CONTEXT=0` 时收 None。
- 实现：`CONTEXT_ENABLED` 常量；`start(language, context)` 存 session；`_session_context(session)` helper；`_partial_mlx`/`_try_incremental_finish`/finish 整句三个 generate 点传参；`/api/start` 路由加 `context` query。
- Commit: `feat(asr): /api/start 收 context 并透传 generate system_prompt（Qwen3 官方热词通道）`

### Task 2: agent 热词组装 + 传递
- 红测试 `tests/test_asr_hotwords.py`：语言分表/对象 courier+contact_channel 注入/数字串过滤/去重/≤120 截断/`BOK_ASR_HOTWORDS=0`。
- 实现：agent.py `_ASR_HOTWORDS` + `asr_hotword_context()`；livekit_plugins `Qwen3ASRSTT(hotword_context=)`，offline(:3607) 与 LiveSTT(:4028) 两条 /api/start 带 `context`；装配点 agent.py:1096 传参（`BOK_ASR_HOTWORDS` gate）。
- Commit: `feat(agent): 话术领域词+对象文字字段组装 ASR 热词并随会话下发`

### Task 3: 文档
- AGENTS.md ASR 段加热词条目（机制/来源/开关/护栏）；RUNTIME_TOPOLOGY ASR 一句。
- Commit: `docs: ASR 热词 context 通道说明`

### Task 4: 验证
- compileall + 全量 pytest；起栈跑 edge-cases（重点 E2 + ASR_MS 对比，护栏 p50 增量 ≤80ms）；down；PR。

## 执行记录
- 2026-09-07 计划就绪。
