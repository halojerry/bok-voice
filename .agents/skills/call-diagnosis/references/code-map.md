# 症状 → 代码地图

主战场在 `apps/agent/agent_runtime/`。改动前先跑 `tests/test_*.py` 对应套件；守卫类改动注意别破坏 env 回退开关。

## ASR 流式层 `providers/livekit_plugins.py`

| 机制 | 位置 | 回退开关 |
|---|---|---|
| VAD 骨架/START pre-roll/停嘴 finish | `_Qwen3ASRLiveStream._recognize`（START 分支并 pre-roll；END 分支 join-hold/犹豫门/finish） | — |
| 词表回声统一闸（剥尾保头） | `_Qwen3ASRLiveStream._echo_filter` + 模块级 `_vocab_echo_guard`/`_strip_vocab_echo_tail`/`_is_hotword_vocab_echo`/`_is_lone_vocab_word` | `QWEN3_HOTWORD_ECHO_GUARD=0` |
| 重解丢弃/长度感知取尾 | `_Qwen3ASRLiveStream._uncommitted`（`_ASR_REDECODE_DROP_RATIO=0.55`） | — |
| 纯犹豫残片门 | `_pure_hesitation` + `_hesitation_gate_on`（停嘴/join-flush 两处） | `QWEN3_ASR_HESITATION_GATE=0` |
| 句级提交门（≥6/≥10 字、双窗稳定、1.5s 限速） | `_sentence_boundary`/`_emit_sentence_commit` | `QWEN3_ASR_SENTENCE_COMMIT=0` |
| 跨段拼接 hold | `_join_hold_s`/`_join_worthy`/`_vocab_prefix_hold` | `QWEN3_ASR_JOIN_HOLD_MS=0` |
| partial 会话级抑制（GPU 竞态） | `set_partial_ms`（agent 侧调） | `BOK_ASR_PARTIAL_SLOW_MS=0` |

## 流程推进 `flow.py`

| 机制 | 位置 |
|---|---|
| verdict 判定（CONFIRM/QUESTION/UNCLEAR/DEFER/REPEAT/REFUSE/OBJECTION） | `decide_advance` + `_CONFIRM_RE`/`_QUESTION_RE`/`_DEFER_RE` 等正则族 |
| 规则必定推进 | `should_auto_advance`（平台词/通知步 say_step/WA captured 门） |
| judge confirm 内容门槛 | `judge_confirm_advance_allowed`（问句步需应承特征） |
| WA 假确认护栏 | `wa_confirm_advance_allowed` |
| 话术渐进披露（正稿/分支/注意解析+单分支命中） | `parse_step_ref`/`match_step_branch`/`StepRefParts` + `FlowController.current_step_text` |
| DEFER 短应承文本 | `agent.py::_defer_ack_line`（三语）+ hook 的 defer-ack 分支 |

## LLM 上下文/出口 `providers/livekit_plugins.py` + `agent.py`

| 机制 | 位置 | 回退开关 |
|---|---|---|
| 静态前缀（语言规则/节奏/应答准则/回应范例/话术总览/对象档案） | `ContextState.render_instruction_prefix` | — |
| 易变尾部（当前步/事实/你上一句截短锚/记忆）+ 瘦身 + 冻结重放 | `ContextState.render_context_tail`/`record_applied_tail` + `ContextAwareLLM.chat` | `BOK_TAIL_SLIM=0` |
| 出口复读防线（句级比对上一句，复读句剥除） | `_RepeatSelfGuardStream`（`ContextAwareLLM.chat` 出口） | `BOK_REPEAT_GUARD=0` |
| 尾部锚拟声剥离 | `_StripTailAnchorStream` | — |
| 每轮 hook（echo/WA/flow/DEFER/QA/转写落库） | `agent.py::entrypoint` 的 `on_user_turn_completed`（搜 `flow_ctrl.last_verdict = verdict`） | `BOK_DEFER_ACK=0` 等 |

## TTS/垫话 `agent.py` + `fillers.py` + `tts_cache.py`

垫话 out-of-band 播放+hold 排序（`BOK_FILLER=0` 关）；QA 快路（`BOK_QA_FASTPATH=0`）；MiniMax bidi 三件自愈（`MINIMAX_BIDI_*`）。

## 启动/编排 `tools/bok.py`

worker spawn 等待 livekit（轮询+死透补拉）、serve 幂等（端口已监听跳过）、desktop ready 含 worker 端口、status 显示 worker 三行。

## 测试索引

- flow 推进/verdict/judge 门：`tests/test_flow_controller.py`
- 渐进披露：`tests/test_step_branch_disclosure.py`
- 复读防线：`tests/test_repeat_self_guard.py`；锚剥离：`tests/test_tail_anchor_strip.py`
- ASR 句级提交/echo 闸/重解/犹豫门：`tests/test_sentence_commit.py`；守卫纯函数：`tests/test_canned_round2.py`/`tests/test_hotword_echo_guard.py`
- VAD 事件级：`tests/test_p0_wrapper_crash_regression.py`
- 术语门禁：`tests/test_cantonese_terminology.py`（新增旧粤拼写字面量即红；本文件也会被扫，写说明别把那个字面量打出来）
