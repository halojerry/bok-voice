"""P0 仪器化打点（2026-09-21，计划 §48）：纯观测零行为变化，测试钉住打点契约。

- P0.2 `BOK_FILLER reply_gap/reply_early`：只在「本轮真垫过」才打（防上一轮
  残值）；回复晚到=gap（裸静默，垫音体验主指标）、早到=early(held)。
- P0.3 `HISTORY_TRUNCATED`：摊销式截断真正动手时才打（前缀重锚=TTFT 尖峰源）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.fillers import FillerDirector  # noqa: E402
from agent_runtime.providers.livekit_plugins import _truncate_chat_items  # noqa: E402


class _GapStub(FillerDirector):
    """只测 on_reply_first_audio 的打点：绕开 manifest/播放器。"""

    def __init__(self) -> None:
        self._timer = None
        self._chain_task = None
        self._reply_audio_seen = False
        self._turn_seq = 0
        self._last_fire_seq = -1
        self._play_started = 0.0
        self._cur_dur = 0.0

    def _pools(self) -> dict[str, list[dict]]:  # type: ignore[override]
        return {"cantonese": []}


def _fired(stub: _GapStub, *, ended_ago_s: float) -> None:
    """把垫话态摆成「本轮已开播、结束线在 ended_ago_s 秒之前」。"""
    stub._turn_seq = 3
    stub._last_fire_seq = 3  # fired_this_round() = True
    stub._cur_dur = 1.0
    stub._play_started = time.monotonic() - ended_ago_s - stub._cur_dur


def test_reply_gap_logged_when_late(capsys):
    d = _GapStub()
    _fired(d, ended_ago_s=1.0)  # 垫话已播完 1s，真回复才到 → gap≈1000ms
    d.on_reply_first_audio()
    out = capsys.readouterr().out
    assert "BOK_FILLER reply_gap=" in out
    assert "reply_early" not in out


def test_reply_early_logged_when_held(capsys):
    d = _GapStub()
    _fired(d, ended_ago_s=-1.0)  # 垫话还要 1s 才播完，回复音频先到 → held
    d.on_reply_first_audio()
    out = capsys.readouterr().out
    assert "BOK_FILLER reply_early=" in out and "(held)" in out
    assert "reply_gap=" not in out


def test_no_log_when_filler_not_fired_this_round(capsys):
    d = _GapStub()
    d._turn_seq = 3
    d._last_fire_seq = 2  # 本轮没垫过（上一轮的残值）
    d._play_started = time.monotonic() - 5.0
    d._cur_dur = 1.0
    d.on_reply_first_audio()
    assert "BOK_FILLER reply_" not in capsys.readouterr().out


def _items(n: int):
    from livekit.agents import llm

    out = [llm.ChatMessage(role="system", content=["sys"])]
    for i in range(n):
        out.append(llm.ChatMessage(role="user" if i % 2 == 0 else "assistant", content=[f"m{i}"]))
    return out


def test_truncation_logs_reanchor(capsys):
    items = _items(80)  # dialog 79 条 > 4×8=32 → 动手
    out = _truncate_chat_items(items, max_turns=8)
    text = capsys.readouterr().out
    assert "HISTORY_TRUNCATED" in text and "items=81->17" in text
    assert len(out) == 17  # system + 8 对


def test_no_truncation_log_below_hysteresis(capsys):
    items = _items(20)  # 低于滞回线：不动手也不打点
    _truncate_chat_items(items, max_turns=8)
    assert "HISTORY_TRUNCATED" not in capsys.readouterr().out
