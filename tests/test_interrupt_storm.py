"""B3 连环打断风暴退避(2026-09-17):滚动窗打断计数 → 静听让话。

连环打断旧版零退避:每次打断掐死在途回复、零账本记录,第 1/2 轮无任何降级
(唯一兜底 starve-ack 要等 2 轮零输出)。现在:speech watcher 计数(与
gen=interrupted 补账同源),窗内 ≥ 阈值 → 直念让路语 + on_user_turn_completed
静听至客户停嘴安静 QUIET_S。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _storm_ack_line,
    _storm_active,
    _storm_max_rounds,
    _storm_on_turn,
    _storm_prune,
    _storm_should_engage,
)


def test_storm_prune_keeps_window():
    ts = [0.0, 5.0, 12.0, 30.0]
    assert _storm_prune(ts, now=31.0, window_s=20.0) == [12.0, 30.0]


def test_storm_should_engage_at_threshold():
    now = 100.0
    ts = [now - 3, now - 2, now - 1]
    assert _storm_should_engage(ts, now, window_s=20.0, threshold=3)


def test_storm_not_engaged_below_threshold():
    now = 100.0
    ts = [now - 3, now - 2]
    assert not _storm_should_engage(ts, now, window_s=20.0, threshold=3)


def test_storm_window_expiry_ignored():
    # 第 3 次打断在窗外:旧两次虽在窗内都唔够位。
    now = 100.0
    ts = [now - 25, now - 22, now - 1]
    assert not _storm_should_engage(ts, now, window_s=20.0, threshold=3)


def test_storm_active_until():
    assert _storm_active(50.0, 49.9)
    assert not _storm_active(50.0, 50.0)
    assert not _storm_active(0.0, 10.0)


def test_storm_ack_line_three_langs():
    langs = [_storm_ack_line(l) for l in ("cantonese", "zh", "en")]
    assert all(langs)
    assert len(set(langs)) == 3
    assert len(langs[0]) <= 16  # 让路语短口,直念即点即完


# ---- 2026-09-17 B3 重排:静听轮处置(封顶/ack 交接/过期清账/防复燃) ----


def _fresh_state(engage_at: float, quiet_s: float = 8.0) -> dict:
    return {"ts": [engage_at - 1, engage_at - 0.5, engage_at],
            "active_until": engage_at + quiet_s, "rounds": 0}


def test_storm_on_turn_silent_then_ack_cadence():
    st = _fresh_state(engage_at=0.0)
    now = 1.0
    verdicts = []
    for i in range(5):
        verdicts.append(_storm_on_turn(st, now + i, quiet_s=8.0, max_rounds=5))
    # r1 静 r2 静 r3 ack r4 静 r5 ack(第 3 轮起奇数轮短承接)。
    assert verdicts == ["silent", "silent", "ack", "silent", "ack"]
    assert st["active_until"] == now + 4 + 8.0  # 每轮续期


def test_storm_on_turn_cap_forces_resume():
    st = _fresh_state(engage_at=0.0)
    verdicts = [_storm_on_turn(st, 1.0 + i, quiet_s=8.0, max_rounds=5) for i in range(6)]
    # r1..r5 静听,r6 超过 cap=5 → resume(恢复完整生成)。
    assert verdicts == ["silent", "silent", "ack", "silent", "ack", "resume"]
    assert st["active_until"] == 0.0 and st["rounds"] == 0 and st["ts"] == []


def test_storm_on_turn_resume_needs_fresh_counts():
    st = _fresh_state(engage_at=0.0)
    _storm_on_turn(st, 1.0, quiet_s=8.0, max_rounds=5)
    _storm_on_turn(st, 20.0, quiet_s=8.0, max_rounds=5)  # 过期 → resume+清 ts
    assert st["ts"] == [] and st["active_until"] == 0.0
    # 旧 3 笔计数已清:2 笔新打断唔够复燃,3 笔先重新开风暴。
    assert not _storm_should_engage([1.0, 1.5], now=21.0, window_s=20.0, threshold=3)
    assert _storm_should_engage([1.0, 1.5, 2.0], now=21.0, window_s=20.0, threshold=3)


def test_storm_on_turn_expiry_before_cap_resumes():
    st = _fresh_state(engage_at=0.0, quiet_s=8.0)
    # 客户 8s 唔出声先再开口:第 1 个静听轮都未排上就过期 → resume。
    v = _storm_on_turn(st, 9.5, quiet_s=8.0, max_rounds=5)
    assert v == "resume"
    assert st["active_until"] == 0.0 and st["rounds"] == 0


def test_storm_on_turn_max_rounds_zero_uncapped():
    st = _fresh_state(engage_at=0.0)
    verdicts = [_storm_on_turn(st, 1.0 + i, quiet_s=8.0, max_rounds=0) for i in range(20)]
    assert all(v in ("silent", "ack") for v in verdicts)
    assert st["active_until"] > 0.0  # 无限续期语义(旧档)保留


def test_storm_max_rounds_default_and_killswitch():
    import os
    old = os.environ.get("BOK_INTERRUPT_STORM_MAX_ROUNDS")
    try:
        os.environ.pop("BOK_INTERRUPT_STORM_MAX_ROUNDS", None)
        assert _storm_max_rounds() == 5
        os.environ["BOK_INTERRUPT_STORM_MAX_ROUNDS"] = "0"
        assert _storm_max_rounds() == 0  # 0=回退旧无限续期
        os.environ["BOK_INTERRUPT_STORM_MAX_ROUNDS"] = "bogus"
        assert _storm_max_rounds() == 5  # 配错回默认
    finally:
        if old is None:
            os.environ.pop("BOK_INTERRUPT_STORM_MAX_ROUNDS", None)
        else:
            os.environ["BOK_INTERRUPT_STORM_MAX_ROUNDS"] = old
