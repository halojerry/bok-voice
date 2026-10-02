"""I3 补答去重(2026-10-02,call-4e8d58c1)单测。

病灶:弃流重生成功后的「晚到真答案」由 tee 直投 ``_late_answer_say``,结构性绕过
主回复流出口的 ``_RepeatSelfGuardStream``——17:28:08 重复交付已答内容实证。修法=
投递口硬闸:与已交付回复(上一句重复锚 last_reply ∪ 跨轮账本 gen==llm 条目)逐条
跑 ``_reply_similarity``,任一 ≥``BOK_REPEAT_CROSS_TURN_SIM``(0.85) 即整条弃
(``LATE_ANSWER_DEDUPED``);``BOK_LATE_ANSWER_DEDUP=0`` 关。同刀把
``_register_reply_lane(lane="late-answer")`` 的 gen 归位 ``"llm"``——补答本就是
LLM 真答案,跨轮账本 ``reply_ledger()`` 只回 gen=="llm",登记侧归位后补答也进
比对面。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _late_answer_dedup_verdict,
    _reply_similarity,
)
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    ContextState,
    _repeat_cross_turn_sim,
)

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")

_LATE = "您的包裹昨天已经到达驿站，尾号六六九九，请您凭取件码领取。"
_NEAR = "您的包裹昨天到了驿站，尾号六六九九，请您凭取件码领取。"  # 归一相似 0.92
_DIFF = "关于赔付方案，我们可以按运费的二到三倍补偿，您看这样可以吗？"  # 相似 0.11


def _fn_body(src: str, header: str) -> str:
    m = re.search(
        re.escape(header) + r".*?(?=\n    (?:async def |def )|\n    [A-Za-z_]+ =)",
        src,
        re.S,
    )
    assert m, f"未找到函数: {header}"
    return m.group(0)


# ---- 纯函数判定矩阵 ----


def test_identical_answer_dropped():
    """原句重复交付 → 弃(相似 1.0 ≥ 0.85)。"""
    drop, sim = _late_answer_dedup_verdict(
        _LATE, last_reply=_LATE, ledger=[], sim_threshold=_repeat_cross_turn_sim()
    )
    assert drop is True
    assert sim == 1.0


def test_near_answer_dropped_via_ledger_reference():
    """近似复述(0.92)也在弃档;参照可来自跨轮账本(last_reply 空)。"""
    drop, sim = _late_answer_dedup_verdict(
        _NEAR, last_reply="", ledger=[_LATE], sim_threshold=0.85
    )
    assert drop is True
    assert sim >= 0.85


def test_dissimilar_answer_passes():
    """新内容(赔付方案 vs 物流通知)→ 放行,相似度远低于闸。"""
    drop, sim = _late_answer_dedup_verdict(
        _DIFF, last_reply=_LATE, ledger=[_NEAR], sim_threshold=0.85
    )
    assert drop is False
    assert sim < 0.85


def test_empty_references_pass_opening_round():
    """开场轮/无参照(last_reply 空+账本空)→ 放行(零误杀)。"""
    drop, sim = _late_answer_dedup_verdict(
        _LATE, last_reply="", ledger=[], sim_threshold=0.85
    )
    assert (drop, sim) == (False, 0.0)


def test_kill_switch_disables_comparison():
    """BOK_LATE_ANSWER_DEDUP 关档:不比对,原句也放行。"""
    drop, sim = _late_answer_dedup_verdict(
        _LATE, last_reply=_LATE, ledger=[_LATE], sim_threshold=0.85, enabled=False
    )
    assert (drop, sim) == (False, 0.0)


# ---- 账本可见性(gen="llm" 归位) ----


def test_llm_gen_ledger_visible_script_not():
    """登记侧 gen="llm" 后,补答可被 reply_ledger() 读到;脚本行不参比。"""
    ctx = ContextState()
    ctx.record_reply(_LATE, "llm")
    ctx.record_reply("脚本直念句不参比", "script")
    assert ctx.reply_ledger() == [_LATE]
    # 端到端:用该账本当参照,重复补答被弃
    drop, _ = _late_answer_dedup_verdict(
        _LATE, last_reply="", ledger=ctx.reply_ledger(), sim_threshold=0.85
    )
    assert drop is True


def test_threshold_default_pinned_and_similarity_reused():
    """闸值缺省 0.85(与出口复读防线同源 env);判定走同款 _reply_similarity。"""
    assert _repeat_cross_turn_sim() == 0.85
    assert _reply_similarity(_NEAR, _LATE) >= 0.85


# ---- 接线结构锚(闭包内调用点) ----


def test_late_answer_say_dedups_before_delivery():
    """_late_answer_say:剥锚后、登记/出声前必过去重;弃即 return(不出声)。"""
    body = _fn_body(AGENT_SRC, "async def _late_answer_say")
    assert "_late_answer_dedup_verdict(" in body, "补答投递口未接去重闸(I3 回归)"
    assert 'BOK_LATE_ANSWER_DEDUP' in body, "kill-switch 读面缺失"
    assert "LATE_ANSWER_DEDUPED sim=" in body, "观测行缺失"
    strip_at = body.index("_strip_tail_anchor_text(text)")
    dedup_at = body.index("_late_answer_dedup_verdict(")
    register_at = body.index('_register_reply_lane(lane="late-answer"')
    say_at = body.index("_say_script(")
    assert strip_at < dedup_at < register_at < say_at, "闸序:剥锚→去重→登记→出声"


def test_late_answer_lane_registers_gen_llm():
    """登记侧 gen="llm"(不动账本过滤器,改登记方)——补答进跨轮比对面。

    2026-10-02 复标:登记点补 ``relieve=False``(先例 qa-fastpath)——stall 抵销
    单点归交付后 ``_report_assistant_turn``,登记再抵=双扣。
    """
    assert '_register_reply_lane(lane="late-answer", gen="llm", text=text, relieve=False)' in AGENT_SRC
    # 旧形态(无 relieve=False / 旧 gen 缺省 script)不得出现在补答登记点
    assert '_register_reply_lane(lane="late-answer", gen="llm", text=text)' not in AGENT_SRC
    assert '_register_reply_lane(lane="late-answer", text=text)' not in AGENT_SRC
