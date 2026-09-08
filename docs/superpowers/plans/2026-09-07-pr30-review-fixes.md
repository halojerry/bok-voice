# PR#30 审查修复计划（join-hold 短尾/竞态 + WA 累积 + judge 护栏 + P3 收尾）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 PR #30（8bd2770）审查确认的 5 个高置信问题 + 4 个 P3 小项，恢复「报号句拆段不吞字、不丢轮、不假推进」的完整保障。

**Architecture:** 全部集中在 A 线 STT 提交层（livekit_plugins.py join-hold）、agent 层（WA 累积/护栏）、flow.py 共用判定函数与两份文档。每个问题独立 commit（TDD：先红测试再修），最后整栈 E2E + 一个 PR。

**Tech Stack:** Python 3.12（.venv312）、pytest、livekit-agents 1.8 STT 插件、bash。分支 `fix/pr30-review-followups`（main 受保护，走 PR）。

## Global Constraints
- 改完 Python 必跑 `.venv312/bin/python -m compileall -q apps packages services tools scripts`（AGENTS.md）。
- 术语门禁：不得引入旧粤语拼写字面量（tests/test_cantonese_terminology.py 全仓扫描——本行亦不得写出该拼写本身，2026-09-08 CI 实证归档文档自踩）。
- StopResponse 的 raise 必须留在任何 `except Exception: pass` try 之外（AGENTS.md 铁律）。
- 保留全部 kill-switch 语义：`QWEN3_ASR_JOIN_HOLD_MS`（0=关）、`BOK_WA_ACCUMULATE=0`、`BOK_WA_ACCUM_TIMEOUT_S`。
- 新注释跟随各文件现行风格；不碰 prompt 文本。
- Conventional commits with scope；一个逻辑改动一个 commit。

---

### Task 1: `_hold_flush` 补「带内容短尾豁免」（审查问题1）
livekit_plugins.py:4005 漏抄主路径 :3956 的 `not _tail_carries_content(payload)` 豁免——hold 超时 flush 会吞掉 <6 字数字尾（「六四三二」）。测试：`tests/test_sentence_commit.py::test_join_hold_timeout_flush_keeps_content_digit_tail`（seed `_committed_text`，断言 FINAL==「六四三二」）。Commit: `fix(agent): join-hold 超时 flush 补短尾内容豁免——与正常停嘴同一套规则`。

### Task 2: flush 会话纪元守卫（审查问题2）
`_hold_flush` 清掉 `_join_task` 后才 `await _finish_session()`；等待期 START 开新 sidecar 会话后，flush 恢复时无条件 `_reset()` 清掉新会话 → 续讲段整轮无 FINAL。修法：`self._session_epoch`（`_start_session` 成功 +1），flush 捕获纪元、`_reset()` 前比对。测试：`test_join_hold_flush_does_not_clobber_new_session`（阻塞式假 httpx client + 4 段 VAD 时序，断言两条 FINAL）。Commit: `fix(agent): join-hold flush 会话纪元守卫——finish 等待期续讲不再被 reset 清轮`。

### Task 3: `_wa_numberish` 剥渠道英文词（审查问题3）
strip 正则不去 ASCII 字母，「whatsapp」8 字母恒超 ≤6 上限 → WhatsApp 通道碎片永远不进累积、4 位下限提前捕获半截号码。加 `_WA_CHANNEL_WORD_RE`（what\s?app|wechat|we\s?chat，IGNORECASE）先剥再算。测试扩展 `test_wa_numberish_and_announce_head`。Commit: `fix(agent): _wa_numberish 剥渠道英文词——WhatsApp 通道碎片恢复累积`。

### Task 4: WA 假确认护栏抽共用函数并盖住背景 judge 路（审查问题4）
新护栏只在 rule-verdict 分支；`_background_flow_judge` CONFIRM 仍无条件 advance（c4f6e4f1 的泄漏点正是 judge 路）。抽 flow.py `wa_confirm_advance_allowed(goal, ref, captured)`，两路共用；judge 分支加 guard+blocked 日志；AGENTS.md ③ 句更新。测试：`test_wa_confirm_guard_shared_rule_and_judge` + `test_judge_path_has_wa_guard`（源码断言）。Commit: `fix(agent,flow): WA 假确认护栏抽 wa_confirm_advance_allowed 并盖住背景 judge 路`。

### Task 5: P3 批次
① :3795 copula regex `is` → `\bis\b`（"confirm this" 误扣 800ms；红断言 `test_join_worthy_gate` 加 "Let me confirm this." is False）。② :3788-3789 docstring 失实归因修正。③ :3900 「只影响号码句」失实修正。④ flow.py:353「6 位起」过期修正。⑤ 删死代码 `_join_worthy_now`。Commit: `fix(agent): join-hold is 词边界 + 注释准确性四小修 + 删死代码`。

### Task 6: docs/RUNTIME_TOPOLOGY.md 补 join-hold（审查问题5）
:149-150 A 线 stt 提交语义句后插入 join-hold 说明（env 名/默认值/守卫）。Commit: `docs(topology): A 线 stt 提交语义补 join-hold 说明`。

### Task 7: 全量验证 + E2E + PR
compileall → 全量 pytest → `bok.py serve` → `scripts/e2e_barge_in.py` + `scripts/e2e_edge_cases.py` → `bok.py down` → push + `gh pr create`（等用户复核合并）。

## 执行记录
- 2026-09-07 计划批准，Task 0 完成（分支已开）。
