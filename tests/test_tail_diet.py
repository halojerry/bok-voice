"""尾部节食单测（第十一波,2026-09-29）——slim 轮记忆块降频。

物理前提（实测账本）：尾部骑在每条新 user 消息之后=全新位置，mlx 严格前缀
缓存对它零命中——**尾部全文每轮都是 uncached**（uncached 中位 ~310 tok 里尾部
占 ~250,记忆块 ≤250 字≈170 tok 是最大件）。节食=slim 轮（同步未推进,多数轮）
距上次「带记忆的尾部」≥BOK_TAIL_MEMORY_EVERY(默认 3)条账本项才带记忆块;
全量轮（步首条 emit_stable / revision 变化非 slim）恒带。

铁律：降频决定必须是**账本纯函数**——F3 重试重渲染时账本未动 → 同一决定 →
identical_skipped 逐字节判定不被降频节奏假断裂（前缀真裂=TTFT 回归）。

质量逻辑：LLM_HISTORY_TURNS(6)原始历史窗口本来就装着最近对话,记忆块职责=
窗口外的旧事实,隔 K 轮不带不丢信息。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402

STEP1 = "流程第 1/5 步\n这一步要达成:确认客户身份"
_MEM = "【本通对话记忆】"


@pytest.fixture(autouse=True)
def _env_defaults(monkeypatch):
    monkeypatch.delenv("BOK_TAIL_SLIM", raising=False)
    monkeypatch.delenv("BOK_TAIL_MEMORY_EVERY", raising=False)
    monkeypatch.delenv("BOK_TAIL_STABLE_SPAN", raising=False)
    monkeypatch.delenv("LLM_HISTORY_TURNS", raising=False)
    monkeypatch.setenv("BOK_ASR_POLISH", "0")


def _mk_state() -> ContextState:
    st = ContextState()
    st.set_flow_current(STEP1)
    st.add_summary("客户", "问过平台系拼多多")
    return st


def _slim_sequence(st: ContextState, n: int) -> list[str]:
    """模拟连续 slim 轮：render(记尾部)→record,同 revision 不动。"""
    out: list[str] = []
    # 首条：步首条 emit_stable=True 全量尾（记忆恒带,账本起锚）。
    t1 = st.render_context_tail()
    st.record_applied_tail("u1", f"u1\n\n{t1}")
    out.append(t1)
    for i in range(2, n + 1):
        t = st.render_context_tail()  # slim=True（同 revision）
        st.record_applied_tail(f"u{i}", f"u{i}\n\n{t}")
        out.append(t)
    return out


# ----------------------------------------------------------------- 降频节奏
# 窗口语义（LLM_HISTORY_TURNS=6 → span=4）下的期望序列：
#   t1     步首条:稳定段+记忆（全量尾,账本起锚）
#   t2-t4  slim:载条 t1 在回看窗内无稳定段;记忆距离 1/2/3 中 3<EVERY
#               (EVERY=3 指距上次带过 ≥3 条账本项 → 隔 3 条不带,第 4 条带)
#   t5     slim:记忆距离 3 → 带记忆
#   t6     稳定段重发(t2-t5 四条均不带 → 载条滑出 span=4 窗)→ 非 diet →
#          记忆恒带
#   t7     slim:无稳定段,记忆 d1 不带


def test_memory_cadence_default_every_3():
    st = _mk_state()
    tails = _slim_sequence(st, 7)
    assert [_MEM in t for t in tails] == [
        True, False, False, False, True, True, False,
    ]


def test_stable_segment_window_cadence():
    """稳定段：步首条带 → 连续 slim 不带 → 载条滑出回看窗(span=4)才重发。

    旧 bug（第十一波修）：与末条账本键比对,slim 记 "" → 隔轮重发
    （uncached 167↔642 交替形状）。窗口语义下 5 轮才重发一次。
    """
    st = _mk_state()
    tails = _slim_sequence(st, 7)
    assert ["【现在这一步】" in t for t in tails] == [
        True, False, False, False, False, True, False,
    ]


def test_memory_cadence_kill_switch_every_1():
    """BOK_TAIL_MEMORY_EVERY=1 → 每轮都带记忆（旧字节,一键回滚）。"""
    import os

    os.environ["BOK_TAIL_MEMORY_EVERY"] = "1"
    try:
        st = _mk_state()
        tails = _slim_sequence(st, 5)
        assert all(_MEM in t for t in tails)
    finally:
        os.environ.pop("BOK_TAIL_MEMORY_EVERY", None)


def test_step_transition_refreshes_memory():
    """换步（set_flow_current → revision bump）→ 全量尾恒带记忆并重置节奏。"""
    st = _mk_state()
    tails = _slim_sequence(st, 3)  # t1 带,t2-t3 不带
    assert [_MEM in t for t in tails] == [True, False, False]
    st.set_flow_current("流程第 2/5 步\n这一步要达成:平台确认")  # revision+1
    t4 = st.render_context_tail()  # 非 slim（revision 变）→ 全量尾
    assert _MEM in t4
    st.record_applied_tail("u4", f"u4\n\n{t4}")
    t5 = st.render_context_tail()  # 回 slim,距离从 t4 重新起算 → 不带
    assert _MEM not in t5


def test_fact_addition_turn_carries_memory_via_full_tail():
    """add_call_fact → revision+1 → 该轮全量尾（记忆+事实都在,既有语义不变）。"""
    st = _mk_state()
    _slim_sequence(st, 2)
    st.add_call_fact("客户讲过在拼多多买")
    t3 = st.render_context_tail()
    assert _MEM in t3
    assert "拼多多" in t3


# ------------------------------------------------------- F3 重渲染字节复现


def test_rebuild_rerender_byte_identical():
    """slim 轮原渲染 vs 同账本状态重渲染（F3 姿势）→ 逐字节相同。

    identical_skipped 判定依赖这个:降频决定吃账本而非可变计数器,
    账本未动 → 同决定 → 不假断裂前缀。
    """
    st = _mk_state()
    _slim_sequence(st, 3)
    original = st.render_context_tail()  # slim,不带记忆（节奏内）
    st.record_applied_tail("u4", f"u4\n\n{original}")
    # F3 姿势:emit_stable=tail_emit_stable_for_rebuild() 重渲染
    rebuilt = st.render_context_tail(emit_stable=st.tail_emit_stable_for_rebuild())
    assert rebuilt == original
    assert _MEM not in rebuilt


def test_ledger_parity_across_prune():
    """prune_applied_tails 三条平行账本对齐截断。"""
    st = _mk_state()
    _slim_sequence(st, 6)
    st.prune_applied_tails(keep=3)
    assert len(st._applied_tails) == 3
    assert len(st._applied_stable_keys) == 3
    assert len(st._memory_in_tails) == 3


def test_bare_entry_records_false_and_extends_distance():
    """洞消息（bare）记 False：计入距离、不重置节奏、不带记忆/稳定段。"""
    st = _mk_state()
    _slim_sequence(st, 2)  # 账本 [(K,True), ("",False)]
    st.record_applied_tail("hole", "hole", bare=True)  # + ("",False)
    t = st.render_context_tail()  # n=3,距 True=2 <3 → 不带记忆;载条 K 仍在窗内无稳定段
    assert _MEM not in t
    assert "【现在这一步】" not in t
    st.record_applied_tail("u4", f"u4\n\n{t}")
    t5 = st.render_context_tail()  # 记忆距离 3 → 带
    assert _MEM in t5


# ------------------------------------------------------- env 立法 pin


def test_tail_diet_env_in_forward_env():
    """BOK_TAIL_MEMORY_EVERY 必须在 _FORWARD_ENV——prod 封闭 env 面的逃生门。"""
    import tools.bok as bok

    assert "BOK_TAIL_MEMORY_EVERY" in bok._FORWARD_ENV
