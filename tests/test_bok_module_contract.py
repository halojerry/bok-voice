"""bokctl 模块契约门禁（G2 W②，2026-10-04）。

单源表红线（治理计划 §G2）：`_FORWARD_ENV` 是 prod 封闭 env 面死门的唯一防线
（历史上 69 键 prod 死门事故）——拆包拆成两份表 = 重现「prod 静默死门」class
事故。本测试钉四件事：

  ① `_FORWARD_ENV = (` 字面量全 tools/ 唯一（表本体不散抄）；
  ② 表键集合 == 冻结快照（env 域搬运波 2026-10-04 基线，254 键；
     键序不变原样搬运由 tests/_bok_src.py 源级 pin 侧与 git diff 保障）；
  ③ `def _control_plane_env` 全 tools/ 唯一（CP 面注入单源）；
  ④ 门面恒等：``bokctl.env._FORWARD_ENV is bok.env._FORWARD_ENV``（门面镜像
     是同一模块对象，读面走 ``bok.env.X`` 不产生副本）。

新增/删除 env 键 → ② 必红：更新快照是**显式立法动作**（与在表里改行同责，
test_forward_env.py 扫 agent_runtime 读取面管「漏登记」，本测试管「静默漂移」）。

W③（2026-10-05）起加钉 CLI 分派面：
  ⑤ 分派注册表：bokctl.cli._COMMANDS 键集 == 全部子命令（冻结 frozenset，
     14 键基线；2026-10-06 demo-setup 立法 +1=15 键），每个值都有可调用的
     run(args)（run(args) 协议）；
  ⑥ 门面恒等：``bok.parse_args is bokctl.cli.parse_args``、
     ``bok.main is bokctl.cli.main``、
     ``bok.commands.down.cmd_down is bokctl.commands.down.cmd_down`` 所指
     同一函数对象（门面镜像零拷贝）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tools"))

import bok  # noqa: E402
import bokctl.cli  # noqa: E402
import bokctl.commands  # noqa: E402
import bokctl.env  # noqa: E402


def _tools_py_sources() -> list[str]:
    """tools/ 全部 .py 源码（__pycache__ 跳过；源级断言的统一扫描面）。"""
    return [
        p.read_text(encoding="utf-8")
        for p in sorted((_ROOT / "tools").rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


# ── ② 冻结快照（env 波 2026-10-04 自 tools/bokctl/core.py 搬出时刻基线）────────
_FORWARD_ENV_SNAPSHOT: frozenset[str] = frozenset({
    "BOK_ACTIVE_CALLS_DIR", "BOK_AMBIENT_FILE", "BOK_AMBIENT_GAIN_DB", "BOK_AMBIENT_KEYBOARD", "BOK_AMBIENT_KEYBOARD_VOL", "BOK_AMBIENT_SCENE", "BOK_ASR_ENGINE",
    "BOK_ASR_FRAME_DEBUG", "BOK_ASR_HOTWORDS", "BOK_ASR_PARTIAL_SLOW_MS", "BOK_ASR_POLISH",
    "BOK_A_LINE_VOICE_TAGS", "BOK_A_REPLY_PROBE", "BOK_BRANCH_ACTION", "BOK_BRANCH_CANNED",
    "BOK_BRANCH_REFUSE_CONFIRM", "BOK_BRANCH_REFUSE_HOTWORD_GUARD", "BOK_BREATH_INJECT", "BOK_BREATH_SENT_CHARS",
    "BOK_BURST_MERGE_WINDOW_S",
    "BOK_CANNED_TEXT_GUARD", "BOK_CONTEXT_MEM_LEGACY", "BOK_CP_TOKEN", "BOK_CSC_CONF_GATE", "BOK_CSC_SIDECAR",
    "BOK_CSC_URL", "BOK_DEFER_ACK", "BOK_DIGIT_ACCUMULATE", "BOK_DOUBAO_ASR", "BOK_DOUBAO_END_WINDOW_MS", "BOK_DOUBAO_DDC", "BOK_DOUBAO_FIRST_TOKEN_BOOST", "BOK_DOUBAO_NONSTREAM", "BOK_E2E_NUDGE_IMMUNE",
    "BOK_FACT_CORRECTION", "BOK_FILLER", "BOK_FILLER_BACKFILL", "BOK_FILLER_CHAIN", "BOK_FILLER_CLOUD", "BOK_FILLER_CONTEXT",
    "BOK_FILLER_COOLDOWN_S", "BOK_FILLER_CUT_AFTER_S", "BOK_FILLER_DELAY_MS", "BOK_FILLER_GAP_MS",
    "BOK_FILLER_HESITATION", "BOK_FILLER_MATCH", "BOK_FILLER_MATCH_THRESHOLD", "BOK_FILLER_MAX",
    "BOK_FILLER_MAX_DUR_S", "BOK_FILLER_RESHOT", "BOK_FILLER_YIELD", "BOK_FIRERED_MODEL_DIR", "BOK_FIRERED_SMOOTH",
    "BOK_FIRERED_THRESHOLD", "BOK_FLOW_GRAPH", "BOK_FLOW_GRAPH_JUDGE", "BOK_GARBLED_REASK",
    "BOK_HOTWORD_LEAK_SANITIZE", "BOK_INTENT_CONTEXT", "BOK_INTENT_RULES", "BOK_INTENT_SEMANTIC",
    "BOK_INTENT_SEM_BASE_URL", "BOK_INTENT_SEM_COS_W", "BOK_INTENT_SEM_SUB_W", "BOK_INTENT_SEM_THRESHOLD",
    "BOK_INTENT_SEM_TIMEOUT_MS", "BOK_INTERP_CLAUSE_COMMIT", "BOK_INTERP_ECHO_DEDUP", "BOK_INTERP_ECHO_DUP_WINDOW_S", "BOK_INTERP_FRAG_HOLD_S", "BOK_INTERP_FRAG_MERGE", "BOK_INTERP_MT_CLOUD_INSTRUCT", "BOK_INTERP_MT_FIRST_CHUNK_CHARS", "BOK_INTERP_MT_PROBE", "BOK_INTERP_MT_STREAM_SAY", "BOK_INTERP_REV_AUDIO", "BOK_INTERP_SERVER_UTT", "BOK_INTERP_SPEC_BUSY_DEPTH", "BOK_INTERP_SPEC_MT", "BOK_INTERP_STUTTER_FIX", "BOK_INTERP_SPEC_WAIT_S", "BOK_INTERP_UTT_MERGE", "BOK_INTERP_UTT_WAIT_S", "BOK_INTERRUPT_INSTANT_ABANDON",
    "BOK_INTERRUPT_LEDGER",
    "BOK_INTERRUPT_REAP",
    "BOK_INTERRUPT_STORM_BACKOFF", "BOK_INTERRUPT_STORM_EXPIRY_RESUME", "BOK_INTERRUPT_STORM_MAX_ROUNDS",
    "BOK_INTERRUPT_STORM_QUIET_S",
    "BOK_INTERRUPT_STORM_THRESHOLD", "BOK_INTERRUPT_STORM_WINDOW_S", "BOK_JUDGE_CAPPED_SKIP",
    "BOK_LATE_ANSWER_DEDUP", "BOK_LATE_FINAL_GUARD", "BOK_LATE_FINAL_HOTWORD_GUARD",
    "BOK_LATE_FINAL_MAX_TAIL_CHARS", "BOK_LAYA_JUDGE", "BOK_LAYA_QA", "BOK_LAYA_QA_P", "BOK_LAYA_QA_TIMEOUT_MS",
    "BOK_LAYA_SIDECAR_URL", "BOK_LLM_FALLBACK", "BOK_LLM_FAMINE", "BOK_LLM_FAMINE_DRAIN_S",
    "BOK_LLM_FAMINE_FIRST_S", "BOK_LLM_FAMINE_TTFT_S", "BOK_LLM_MSG_DEBUG", "BOK_LLM_REGEN",
    "BOK_LLM_STALL_OBS_TPS", "BOK_LOG_LEVEL", "BOK_MAX_CALL_DURATION_S", "BOK_MEMORY_CHARS", "BOK_MINED_HOTWORDS",
    "BOK_MINIMAX_ASR", "BOK_MINIMAX_BIDI_GUARD", "BOK_MLX_ABORT", "BOK_MODEL_ROUTING",
    "BOK_NUMBER_GUARD", "BOK_PAUSE_ACK",
    "BOK_PERCEIVED_BUDGET_MS", "BOK_PREEMPTIVE_DEBUG", "BOK_PREFILL_SPEC", "BOK_PREFILL_SPEC_DEBUG",
    "BOK_PREFILL_SPEC_FINAL_QUIET_MS", "BOK_PREFILL_SPEC_CLOUD", "BOK_PREFIX_PREWARM_YIELD", "BOK_QA_AUTO_DIGEST",
    "BOK_QA_CANNED_COOLDOWN_S", "BOK_QA_FASTPATH", "BOK_QA_HOMOPHONE", "BOK_QA_MATCH_THRESHOLD", "BOK_QA_PHONETIC",
    "BOK_QA_PHONETIC_THRESHOLD", "BOK_QA_PINYIN", "BOK_QA_PRIORITY", "BOK_QA_RECALL_FLOOR", "BOK_QA_RECALL_K",
    "BOK_QA_ROTATION", "BOK_QA_SEMANTIC", "BOK_QA_SEM_BASE_URL", "BOK_QA_SEM_THRESHOLD", "BOK_QA_SEM_TIMEOUT_MS",
    "BOK_QA_WA_STEP_QUESTION", "BOK_QWEN_REALTIME", "BOK_REALTIME_DEMO_MAX_S", "BOK_REASK_CONF_MEAN",
    "BOK_REASK_LOW_RATIO", "BOK_REASK_MAX_CONSEC", "BOK_REASK_MIN_CONTENT_CHARS", "BOK_REPEAT_ACK",
    "BOK_REPEAT_CROSS_TURN",
    "BOK_REPEAT_CROSS_TURN_SIM", "BOK_REPEAT_GUARD", "BOK_REPEAT_HEAD_MAX_HOLD",
    "BOK_RESPONSE_WATCHDOG_FILLER_EXT_S", "BOK_RESPONSE_WATCHDOG_S", "BOK_RESPONSE_WATCHDOG_SYNTH_EXT_S",
    "BOK_ROOM_CLAIM", "BOK_ROOM_CLAIM_DIR", "BOK_ROUTE_JUDGE", "BOK_SAY_STEP_LIMIT", "BOK_SETTLE_WAIT_S",
    "BOK_SIP_MODE", "BOK_SLOT_ACTOR", "BOK_SMART_TURN", "BOK_SNIPPETS", "BOK_SPEAKER_LOCK", "BOK_SPEAKER_LOCK_MODEL", "BOK_STALL_LADDER", "BOK_STARVE_ACK",
    "BOK_TAIL_MEMORY_EVERY", "BOK_TAIL_SLIM", "BOK_TAIL_STABLE_SPAN", "BOK_TOOLS_FOLLOWUP", "BOK_TTS_FALLBACK",
    "BOK_TTS_FIRST_CHUNK_CHARS", "BOK_TTS_FIRST_CLAUSE", "BOK_TTS_FIRST_CLAUSE_CHARS", "BOK_TTS_PREWARM",
    "BOK_TURNS_REPLAY", "BOK_TURN_DETECTOR_THRESHOLD", "BOK_TURN_DETECTOR_THRESHOLDS", "BOK_UNCLEAR_ADVANCE",
    "BOK_UNCLEAR_ADVANCE_N", "BOK_VAD_PROVIDER", "BOK_WA_ACCUMULATE", "BOK_WA_ACCUM_TIMEOUT_S",
    "BOK_WA_ECHO_STRIP", "BOK_WA_LEN_CHECK", "BOK_WORKER_LOAD_THRESHOLD", "BOK_WORKER_PORT",
    "BOK_WORKER_PORT_GUARD", "CONTEXT_RAG", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "DEEPSEEK_THINKING", "EMOTION_TAG_PILOT", "EMOTION_TAG_PROMPT", "ENDPOINT_MAX_DELAY", "ENDPOINT_MIN_DELAY",
    "FALSE_INTERRUPTION_TIMEOUT", "FLOW_JUDGE_DELAY", "FLOW_JUDGE_IDLE_CAP", "FLOW_JUDGE_LLM_API_KEY",
    "FLOW_JUDGE_LLM_THINKING", "FLOW_LLM_ADVANCE", "INTERRUPT_MIN_DURATION", "LLM_FIRST_TOKEN_TIMEOUT_S",
    "LLM_HISTORY_TURNS", "LLM_LATE_ANSWER_DEADLINE_S", "LLM_MAX_TOKENS", "LLM_PREFIX_PREWARM",
    "LLM_REQUEST_RETRIES", "LLM_REQUEST_TIMEOUT_S", "LLM_TEMPERATURE", "LLM_WARMUP", "MINIMAX_API_KEY",
    "MINIMAX_BASE_URL", "MINIMAX_BIDI_AUTO_REWARM", "MINIMAX_BIDI_CANCEL_WAIT_S",
    "MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S", "MINIMAX_BIDI_HEAD_FLUSH", "MINIMAX_BIDI_PING_MAX_MISS",
    "MINIMAX_BIDI_PING_S", "MINIMAX_BIDI_PREWARM_RETRY", "MINIMAX_BIDI_STALL_MAX_HEALS",
    "MINIMAX_BIDI_SYNTH_WARMUP", "MINIMAX_BIDI_TAIL_IDLE_S", "MINIMAX_CONTINUOUS_SOUND", "MINIMAX_EMOTION",
    "MINIMAX_FIRST_AUDIO_TIMEOUT_S",
    "MINIMAX_LANGUAGE_BOOST", "MINIMAX_PAUSE", "MINIMAX_PAUSE_SECS", "MINIMAX_PITCH", "MINIMAX_REGION",
    "MINIMAX_SPEED", "MINIMAX_TTS_OVERLAP", "MINIMAX_TTS_OVERLAP_CHARS", "MINIMAX_TTS_OVERLAP_MS", "MINIMAX_VOL",
    "MINIMAX_WS", "MINIMAX_WS_MODE", "MINIMAX_WS_POOL", "MINIMAX_WS_URL", "PREEMPTIVE_DISABLE_ON_MARKER",
    "PREEMPTIVE_GENERATION", "PREEMPTIVE_MAX_RETRIES", "PREEMPTIVE_TTS", "QWEN3_ASR_CHUNK_KEEP",
    "QWEN3_ASR_CHUNK_MS", "QWEN3_ASR_CLAUSE_COMMIT_MIN_CHARS", "QWEN3_ASR_CLAUSE_LEN_CHARS",
    "QWEN3_ASR_COMMIT_MIN_INTERVAL_S", "QWEN3_ASR_HESITATION_GATE", "QWEN3_ASR_JOIN_HOLD_MS", "QWEN3_ASR_JOIN_HOLD_VOCAB",
    "QWEN3_ASR_PAUSE_COMMIT_MIN_CHARS", "QWEN3_ASR_PREFLIGHT_LANG_GATE", "QWEN3_ASR_SENTENCE_PAUSE_TRIGGER",
    "QWEN3_ASR_STREAM", "QWEN3_ECHO_GUARD", "QWEN3_HOTWORD_ECHO_GUARD", "QWEN3_TTS_MAX_TASK_AUDIO_SEC",
    "QWEN3_TTS_OVERLAP", "QWEN3_TTS_OVERLAP_CHARS", "QWEN3_TTS_OVERLAP_MS", "QWEN_REALTIME_BASE_URL",
    "QWEN_REALTIME_KEY", "QWEN_REALTIME_WS_BASE", "REPLY_MEMORY_LINES", "RESUME_FALSE_INTERRUPTION", "SENTRY_DSN",
    "SENTRY_ENVIRONMENT", "SENTRY_SEND_PII", "SILENCE_NUDGE_MAX", "SILENCE_NUDGE_SECONDS", "TURN_DETECTION",
    "VOLC_ACCESS_TOKEN", "VOLC_APP_ID", "VOLC_DIALECT", "VOLC_LANGUAGE", "VOLC_LOUDNESS_RATE", "VOLC_RESOURCE_ID",
    "VOLC_SPEAKER", "VOLC_SPEECH_RATE", "VOLC_TTS_ENDPOINT", "WEB_SEARCH",
})
assert len(_FORWARD_ENV_SNAPSHOT) == 290, "快照含重复键（生成脚本口径坏了）"


def test_forward_env_table_literal_is_unique_in_tools():
    """① `_FORWARD_ENV = (` 字面量全 tools/ 恰好一次——表本体唯一，别名/读面不复制表。"""
    hits = sum(src.count("_FORWARD_ENV = (") for src in _tools_py_sources())
    assert hits == 1, f"_FORWARD_ENV = ( 在 tools/ 出现 {hits} 次（必须恰 1 次=单源红线）"


def test_forward_env_key_set_matches_frozen_snapshot():
    """② 表键集合 == 冻结快照。变更表 = 显式立法动作：改表同时更新本快照，
    评审才能看见键面 diff（静默漂移=prod 死门 class 事故的入口）。"""
    current = set(bokctl.env._FORWARD_ENV)
    added = sorted(current - _FORWARD_ENV_SNAPSHOT)
    removed = sorted(_FORWARD_ENV_SNAPSHOT - current)
    assert not added and not removed, (
        f"_FORWARD_ENV 键面漂移：新增 {added}，删除 {removed} ——"
        "更新表必须同步更新 tests/test_bok_module_contract.py 快照（显式立法动作）"
    )
    assert len(current) == 290


def test_control_plane_env_def_is_unique_in_tools():
    """③ `def _control_plane_env` 全 tools/ 恰好一次——CP 面注入单源。"""
    hits = sum(src.count("def _control_plane_env(") for src in _tools_py_sources())
    assert hits == 1, f"def _control_plane_env( 在 tools/ 出现 {hits} 次（必须恰 1 次）"


def test_facade_identity_env_module():
    """④ 门面恒等：bok.env 与 bokctl.env 是同一模块对象，_FORWARD_ENV 是同一
    tuple（门面镜像零拷贝——读面走 bok.env.X 不会拿到分叉副本）。"""
    assert bok.env is bokctl.env
    assert bok.env._FORWARD_ENV is bokctl.env._FORWARD_ENV
    # 历史名别名同源（2026-09-18 旧调用面锚）
    assert bok.env._BOK_PASSTHROUGH_KEYS is bok.env._FORWARD_ENV


# ── ⑤ 冻结快照（W③ 2026-10-05 CLI 分家时刻的 14 子命令 + 2026-10-06 demo-setup
#    立法 +1 = 15 子命令全集）────────────────────────────────────────────────
_SUBCOMMAND_NAMES: frozenset[str] = frozenset({
    "catalog", "manifest", "status", "serve", "down", "doctor", "tts-mine",
    "clean-testdata", "monitor", "up", "download", "tts-pregen", "prod", "setup",
    "demo-setup",
})


def test_dispatch_registry_covers_exactly_the_15_subcommands():
    """⑤ 分派注册表钉死：_COMMANDS 键集 == 全部 15 子命令（新增/删除子命令
    必须显式立法进表+快照），且每个值都有可调用的 run(args)（run 协议）。"""
    assert set(bokctl.cli._COMMANDS) == set(_SUBCOMMAND_NAMES), (
        "bokctl.cli._COMMANDS 与 15 子命令全集漂移——新增/删除子命令是显式"
        "立法动作：同步 cli._COMMANDS 与本测试的 _SUBCOMMAND_NAMES 快照"
    )
    for name, mod in bokctl.cli._COMMANDS.items():
        assert callable(getattr(mod, "run", None)), f"_COMMANDS[{name!r}] 缺 run(args)"


def test_facade_identity_cli_dispatch():
    """⑥ 门面恒等：bok.parse_args/bok.main 与 cli 同一函数对象（core 经
    vars(core) 镜像续读），命令实现与属主模块同对象（零拷贝）。"""
    assert bok.parse_args is bokctl.cli.parse_args
    assert bok.main is bokctl.cli.main
    assert bok.commands.down.cmd_down is bokctl.commands.down.cmd_down
