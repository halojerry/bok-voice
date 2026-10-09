# RUNBOOK（症状 → 去处）

唯一「问题→去处」入口，人和 agent 都从这里进。**只链接不复写**：本页不解释机制，
内容归 `.agents/skills/call-diagnosis/` 三件套与各专题文档（`docs/LATENCY_BUDGETS.md`、
`docs/TURN-PIPELINE.md` 等），避免长出第 N 份漂移 map。

- **锚纪律**：一律符号名（类/函数/env 键），不点行号——行号必漂，符号名靠编辑器搜。
- **回退开关**列只收 `tools/bokctl/env.py` `_FORWARD_ENV` 已登记的键（prod 转发保真）；`—` = 无开关，只能改码或查因。
- **取证方法**（turns 账本 → 日志标记窗 → 离线复算）见 `.agents/skills/call-diagnosis/SKILL.md`；日志标记词汇表见其 `references/markers.md`。
- **SaaS 安全面**（暴露清单/凭据轮换矩阵/CSP/TURN/滥用闸/残余风险）唯一入口=`docs/SECURITY-SAAS.md`。
- 校验：`scripts/ops/check_doc_paths.py`（断链）+ `tests/test_doc_hygiene.py`；本页新增行也过这两道门。

| 症状 | 第一现场 | 探针/复算 | 测试 | 回退开关 |
|---|---|---|---|---|
| TTS 首声慢/静默/断流 | `apps/agent/agent_runtime/providers/livekit_plugins.py` `MiniMaxTTS`（bidi 头段 flush、首包看门狗） | `scripts/bench/bench_minimax_bidi.py`、`scripts/bench/probe_minimax_bidi_cold.py`；`scripts/probes/probe_latency_soak.py` | `tests/test_bidi_head_flush.py`、`tests/test_minimax_bidi.py` | `MINIMAX_BIDI_HEAD_FLUSH=0`；回 classic 通道 `MINIMAX_WS_MODE=classic` |
| 回复延迟超标（`PERCEIVED_MS` 超预算） | `apps/agent/agent_runtime/agent.py` `PERCEIVED_MS` 三段打点；预算表 `docs/LATENCY_BUDGETS.md` | `scripts/probes/probe_latency_soak.py`；`scripts/bench/measure_latency.py` | `tests/test_latency_soak_report.py` | — |
| 哑轮/卡死不回话（首 token 超时、stall 阶梯） | `apps/agent/agent_runtime/agent.py` `_register_reply_lane`（出口 chokepoint）+ 响应看门狗 | `scripts/probes/probe_latency_soak.py`（正常轮哑 ≥2 = FAIL） | `tests/test_reply_chokepoint_lint.py`、`tests/test_stall_ladder.py`、`tests/test_watchdog_real_reply.py` | `BOK_RESPONSE_WATCHDOG_S`、`LLM_FIRST_TOKEN_TIMEOUT_S`、`BOK_LLM_REGEN=0` |
| LLM 饥荒/误降级 | `apps/control-plane/control_plane/ops_metrics.py`（EMA 迟滞状态机） | `python tools/bok.py prod status`；web `/disaster` 面板 | `tests/test_llm_famine.py` | `BOK_MODEL_ROUTING=0` |
| 双派发/双开场白 | `apps/agent/agent_runtime/room_claim.py`（flock 同房互斥） | agent.log `[room-claim]` 标记（词汇表 `references/markers.md`） | `tests/test_room_claim.py`、`tests/test_dispatch_watchdog.py` | `BOK_ROOM_CLAIM=0` |
| 客户连环打断风暴/抢话 | `apps/agent/agent_runtime/agent.py` 打断退避（storm backoff）+ deferred abandon（打断确证才弃流，短应承嗯/好的豁免） | `scripts/e2e/e2e_barge_in.py`（基线 interrupted=yes） | `tests/test_interrupt_storm.py`、`tests/test_interrupt_deferred_abandon.py` | `BOK_INTERRUPT_STORM_BACKOFF=0`；封顶轮数 `BOK_INTERRUPT_STORM_MAX_ROUNDS`；弃流回旧档 `BOK_INTERRUPT_INSTANT_ABANDON=1` |
| AI 复读上一句/整段重复 | `apps/agent/agent_runtime/providers/livekit_plugins.py` `_RepeatSelfGuardStream` + 跨轮回复账本 | `scripts/probes/probe_offscript_soak.py`；turns `gen` 列 | `tests/test_repeat_self_guard.py`、`tests/test_repeat_cross_turn.py` | 跨轮 `BOK_REPEAT_CROSS_TURN=0`；出口防线 `BOK_REPEAT_GUARD=0` |
| AI 自闻自打断/回声误杀 | `apps/agent/agent_runtime/providers/livekit_plugins.py` `_echo_filter`（剥尾保头） | `scripts/e2e/e2e_edge_cases.py` | `tests/test_echo_guard.py`、`tests/test_hotword_echo_guard.py` | `QWEN3_ECHO_GUARD=0` |
| 客户话被吃字（头/尾）、重问变多 | `apps/agent/agent_runtime/providers/livekit_plugins.py` `_Qwen3ASRLiveStream`（START pre-roll、`_uncommitted` 取尾、join-hold） | `scripts/probes/probe_fast_speech.py`、`scripts/probes/probe_8khz_asr.py` | `tests/test_sentence_commit.py`、`tests/test_asr_chunk_keep.py` | `QWEN3_ASR_SENTENCE_COMMIT=0`（须配对 `TURN_DETECTION=` 置空，见 `docs/LATENCY_BUDGETS.md`） |
| WA 捕号错/数字读错/编造号码 | `packages/core/bok_voice_core/output_guard.py` `guard_fabricated_number` + 收号累积链 | `scripts/probes/probe_cantonese_digits.py`、`scripts/probes/probe_asr_digits_ab.py` | `tests/test_number_guard.py`、`tests/test_digit_accumulate.py` | `BOK_NUMBER_GUARD=0`；累积 `BOK_WA_ACCUMULATE=0`、`BOK_DIGIT_ACCUMULATE=0` |
| 流程不推进/乱推进（假推进） | `apps/agent/agent_runtime/flow.py` `FlowController`（`decide_advance`/`should_auto_advance`/`match_step_branch`） | `scripts/probes/probe_flow_20rounds.py`；离线复算法见 `.agents/skills/call-diagnosis/SKILL.md` 第 3 步 | `tests/test_flow_controller.py`、`tests/test_unclear_advance.py` | — |
| 意图不命中/误触发 | `packages/core/bok_voice_core/intent_rules.py` + `apps/agent/agent_runtime/laya_judge.py`（Laya 旁路） | `scripts/probes/probe_intent_mine.py`、`scripts/probes/probe_judge_parity.py` | `tests/test_intent_rules.py`、`tests/test_laya_judge.py` | `BOK_LAYA_JUDGE=0`；图 judge `BOK_FLOW_GRAPH_JUDGE=0` |
| 话术图跳转/播词条不生效 | `packages/core/bok_voice_core/flow_graph.py`（`validate_flow_graph`/`parse_flow_graph`） | `scripts/probes/probe_flow_graph.py` | `tests/test_flow_graph_core.py`、`tests/test_flow_graph_runtime.py` | `BOK_FLOW_GRAPH=0` |
| 分支动作（收线/转人工/跳步）误杀或失效 | `packages/core/bok_voice_core/branch_syntax.py` `parse_branch_action` | `scripts/probes/probe_branch_action.py` | `tests/test_branch_actions.py`、`tests/test_branch_action_safety.py` | `BOK_BRANCH_ACTION=0`；双护栏 `BOK_BRANCH_REFUSE_CONFIRM`、`BOK_BRANCH_REFUSE_HOTWORD_GUARD` |
| 垫话不响/垫话压住真答案 | `apps/agent/agent_runtime/fillers.py`（让路政策、冷却、配额） | `scripts/probes/probe_filler_timing.py`（首声 <2.5s 预算） | `tests/test_fillers.py`、`tests/test_filler_yield.py` | `BOK_FILLER_YIELD=0`；冷却窗 `BOK_FILLER_COOLDOWN_S=0` |
| QA 快路不命中/罐头没播 | `apps/agent/agent_runtime/qa_gate.py`（阈值与优先序见 AGENTS.md「罐头音三件套」） | `scripts/probes/probe_qa_hit.py` | `tests/test_qa_gate.py`、`tests/test_qa_rotation.py` | 轮换回退 `BOK_QA_ROTATION=0` |
| env 改了不生效（prod 静默死门） | `tools/bokctl/env.py` `_FORWARD_ENV`（CP 面另看 `_control_plane_env`） | —（静态扫描即门禁） | `tests/test_forward_env.py` | — |
| 改了源码行为没变/殭尸 worker | worker 是长命进程：`bok down && bok serve` 才吃新码；`ps aux` 查 `agent_runtime` 进程属哪个 worktree | `python tools/bok.py status`；`.agents/skills/call-diagnosis/SKILL.md` 第 0 步 | — | — |
| 云 ASR（豆包）失败/静默回退本地 | `apps/agent/agent_runtime/providers/doubao_asr.py` `DoubaoSTT`（缺 key 显式回退，绝不静默上云） | `scripts/probes/probe_cloud_asr.py`（corpus-v2 折叠评分） | `tests/test_doubao_asr.py` | `BOK_DOUBAO_ASR=0` |
| 转写同音字/词错不纠 | `packages/core/bok_voice_core/asr_polish.py` + `services/csc-sidecar/`（:8792，zh-only） | `scripts/pipeline/eval_csc_model.py`；`scripts/probes/probe_polish_model.py` | `tests/test_asr_polish.py` | `BOK_ASR_POLISH=0`；CSC `BOK_CSC_SIDECAR=0` |
| B 线同传延迟大/出译堆积 | `apps/agent/agent_runtime/interpret.py` `_PlaybackBacklog`/`_LagLedger` | `scripts/probes/probe_interpret_latency.py`（预算按姿势分档见 `docs/LATENCY_BUDGETS.md` §5）、`scripts/probes/probe_interp_backlog.py` | `tests/test_interp_backlog.py`、`tests/test_interp_lag_ledger.py` | 投机翻译 `BOK_INTERP_SPEC_MT=0`；碎片闸 `BOK_INTERP_FRAG_MERGE=0`；背压 `BOK_INTERP_BACKLOG=0`；说话中成句 `BOK_INTERP_CLAUSE_COMMIT=0`（配对 `QWEN3_ASR_SENTENCE_COMMIT=0`） |
| B 线断断续续/讲不完一句（碎片/天窗） | W6 归因：提交粒度与管道填满（`docs/superpowers/plans/2026-10-09-bline-fluency.md`）；薄线=句档 profile（`bokctl.servers.interp_lite_commit_env`） | `scripts/probes/probe_interp_fluency.py`（天窗/段时长/onset/提交单元四判据）、`scripts/probes/probe_interp_continuous.py`（边说边译硬定义） | `tests/test_interp_lite_commit_env.py`、`tests/test_doubao_asr.py`（对齐/保险丝） | 回碎片档=显式设 `QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS=6`；整段档 `QWEN3_ASR_CLAUSE_LEN_CHARS=999`；**薄线总开关 `BOK_INTERP_LITE=0`（serve 侧，回旧线逐字节）** |
| B 线译员耳语没了（rev 只字幕不出声） | `interpret.py` `_direction_audio_enabled`（2026-10-08 双向出声翻案） | `scripts/e2e/e2e_interpret.py` I2/I6 | `tests/test_interpret_tts_provider.py` | `BOK_INTERP_REV_AUDIO=0` |
| 同传译文无声吞句/卡死 20-39s | interp 日志 `MINIMAX_BIDI_FLUSH_ACK_STALE`/`MINIMAX_BIDI_DROP_STALE`/`sentences=0`；根因面=flush ack 纪元门禁（`livekit_plugins` `_MiniMaxBidiStream` `flush_epoch`，修复 8bd401a，call-4322e14d） | `scripts/probes/probe_interp_spec_live.py`；词汇表 `references/markers.md` | `tests/test_minimax_bidi.py`、`tests/test_interp_lite_spec.py` | —（结构性修复，无回退键） |
| 通话语言劈叉（粤语夹普通话） | `apps/agent/agent_runtime/agent.py` `_call_language`/`PinnedLanguageState`（话术快照语言优先） | `scripts/e2e/e2e_trilingual_livekit.py` | `tests/test_fixed_language_call.py`、`tests/test_cantonese_terminology.py` | — |
| 并发派单被拒/worker 拒派 | worker 入口 `load_threshold`（load=整机 CPU）+ 容量门 | `scripts/bench/load_audio_concurrency.py`（单机 A 线 2 路≈免费、4 路=悬崖） | `tests/test_capacity_admission.py` | `BOK_MAX_ACTIVE_CALLS`、`BOK_WORKER_LOAD_THRESHOLD` |
| 文档里的路径/锚点死了 | `scripts/ops/check_doc_paths.py`（活文档断链；锚纪律=符号名不点行号） | CI | `tests/test_doc_hygiene.py`、`tests/test_script_root_anchor.py` | — |
