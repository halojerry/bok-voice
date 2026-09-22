"""P3.2/P3.3（2026-09-21，§48）：垫话掐断 + 打断特权轮。

- P3.2 `BOK_FILLER cut_on_ready`：真回复音频就绪且垫话已播 >1s → 掐剩余+清
  hold 窗（官方 hold-message 姿势）；不足阈值照旧播完（reply_early/gap 打点）。
- P3.3 打断特权轮：`note_interrupt_round()`→arm 消费→连轮冷却豁免；
  `_nudge_should_fire` 的 2×delay 窗对「打断后未回应」豁免（gate1 不动）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _nudge_should_fire  # noqa: E402
from agent_runtime.fillers import FillerDirector, filler_cut_after_s  # noqa: E402


class _CutStub(FillerDirector):
    """只测 on_reply_first_audio 的掐断/打点：绕开 manifest/播放器。"""

    def __init__(self) -> None:
        self._timer = None
        self._chain_task = None
        self._reply_audio_seen = False
        self._turn_seq = 0
        self._last_fire_seq = -1
        self._play_started = 0.0
        self._cur_dur = 0.0
        self._count = 0
        self._player = None
        self._chain_depth = 0
        self._handle = object()  # 在播句柄(非 None=可掐)
        self.stopped = False

    def _pools(self) -> dict[str, list[dict]]:  # type: ignore[override]
        return {"cantonese": []}

    def _stop_playing(self) -> None:  # type: ignore[override]
        self.stopped = True
        self._handle = None


def _fired(stub: _CutStub, *, playing_for_s: float, dur: float = 2.0) -> None:
    stub._turn_seq = 2
    stub._last_fire_seq = 2
    stub._cur_dur = dur
    stub._play_started = time.monotonic() - playing_for_s


def test_cut_when_reply_ready_and_filler_long(monkeypatch):
    monkeypatch.delenv("BOK_FILLER_CUT_AFTER_S", raising=False)
    d = _CutStub()
    _fired(d, playing_for_s=1.5)  # 已播 1.5s ≥ 1.0 阈值 → 掐
    d.on_reply_first_audio()
    assert d.stopped, "应掐掉剩余垫话"
    assert d._play_started == 0.0  # hold 窗清零 → 回复即刻放行


def test_cut_logged(monkeypatch, capsys):
    monkeypatch.delenv("BOK_FILLER_CUT_AFTER_S", raising=False)
    d = _CutStub()
    _fired(d, playing_for_s=1.2)
    d.on_reply_first_audio()
    assert "BOK_FILLER cut_on_ready" in capsys.readouterr().out


def test_no_cut_below_threshold(monkeypatch, capsys):
    monkeypatch.delenv("BOK_FILLER_CUT_AFTER_S", raising=False)
    d = _CutStub()
    _fired(d, playing_for_s=0.4, dur=1.2)  # 已播 0.4s < 1.0 → 不掐,early 打点
    d.on_reply_first_audio()
    assert not d.stopped
    assert "reply_early=" in capsys.readouterr().out


def test_cut_disabled_by_env(monkeypatch, capsys):
    monkeypatch.setenv("BOK_FILLER_CUT_AFTER_S", "0")
    assert filler_cut_after_s() == 0.0
    d = _CutStub()
    _fired(d, playing_for_s=1.5)
    d.on_reply_first_audio()
    assert not d.stopped  # 0=关:照旧播完(此处 dur 内 → gap/early 打点)
    assert "cut_on_ready" not in capsys.readouterr().out


def test_interrupt_round_exempts_cooldown():
    """打断轮豁免连轮冷却：上一轮垫过+本轮 arm 前标了打断 → 冷却不拦。"""
    d = _CutStub()
    d._turn_seq = 5
    d._last_fire_seq = 5  # 上一轮刚垫过(序号差 1 → 冷却本会拦)
    d.note_interrupt_round()
    d.arm()  # 无 player → 定时器不起,但轮标记已消费
    assert d._round_interrupted is True
    assert d._interrupt_pending is False  # 消费即清(不泄漏到再下一轮)
    # 冷却条件本体(镜像 _fire 的判断):打断轮应放行
    would_skip = (
        d._turn_seq > 0
        and d._chain_depth == 0
        and d._turn_seq - d._last_fire_seq <= 1
        and not getattr(d, "_round_interrupted", False)
    )
    assert would_skip is False


def test_normal_round_keeps_cooldown():
    d = _CutStub()
    d._turn_seq = 5
    d._last_fire_seq = 5
    d.arm()  # 未标打断
    assert d._round_interrupted is False
    would_skip = (
        d._turn_seq > 0
        and d._chain_depth == 0
        and d._turn_seq - d._last_fire_seq <= 1
        and not getattr(d, "_round_interrupted", False)
    )
    assert would_skip is True  # 非打断轮:冷却照拦


def test_nudge_gate_interrupted_unanswered_exempts_2x_window():
    delay = 8.0
    reply_ts, user_ts, now = 100.0, 105.0, 112.0  # 用户后讲,now-user=7s ≤ 2×delay
    # 旧档:gate2 压住(答案在路上)——保持兼容
    assert _nudge_should_fire(now, reply_ts, user_ts, delay) is False
    # 打断后未回应:豁免 gate2,但 gate1(now-reply=12s ≥ delay)已过 → 允许开火
    assert _nudge_should_fire(now, reply_ts, user_ts, delay, interrupted_unanswered=True) is True


def test_nudge_gate1_still_holds_for_interrupt_round():
    delay = 8.0
    reply_ts, user_ts, now = 100.0, 101.0, 104.0  # now-reply=4s < delay
    assert _nudge_should_fire(now, reply_ts, user_ts, delay, interrupted_unanswered=True) is False
