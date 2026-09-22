"""垫话时长上限（2026-09-21）：`hold_if_playing()` 把回复扣多久，取决于垫话多长。

真通话实测（`call-54ed586a`，计划档 §42.2）：回复首段音频 ~2.8s 就绪，却要等垫话
播完 3.8s + gap 才出声（`tts ttfb` 被撑到 1398ms）——**自伤约 1.3s**；而那一轮的
垫话长 1.7s，是从「短档 1.0-1.5s / 长档 1.7-2.3s」里**随机**挑的。

本文件钉住 `_pick` 的时长筛选契约：**只挑盖得住就够的短档，但绝不饿死垫话**。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.fillers import FillerDirector, filler_max_dur_s  # noqa: E402


class _Stub(FillerDirector):
    """只测 `_pick` 的池筛选：绕开 manifest 加载/播放器。"""

    def __init__(self, pool: list[dict]) -> None:
        self._pool = pool
        self._recent: list[str] = []

    def _pools(self) -> dict[str, list[dict]]:  # type: ignore[override]
        return {"cantonese": self._pool}


def _entries(*durs: float) -> list[dict]:
    return [{"file": f"f{i}.wav", "text": f"t{i}", "dur_s": d, "cat": "ack"} for i, d in enumerate(durs)]


def test_default_cap_is_1_2s(monkeypatch):
    monkeypatch.delenv("BOK_FILLER_MAX_DUR_S", raising=False)
    assert filler_max_dur_s() == 1.2


def test_zero_disables_the_cap(monkeypatch):
    monkeypatch.setenv("BOK_FILLER_MAX_DUR_S", "0")
    assert filler_max_dur_s() == 0.0


def test_long_tier_is_avoided(monkeypatch):
    """短档在场时绝不挑长档——长档是纯延迟（回复 ~2.8s 就绪，盖 1.5s 就够）。"""
    monkeypatch.setenv("BOK_FILLER_MAX_DUR_S", "1.2")
    d = _Stub(_entries(1.0, 1.4, 1.8, 2.2))
    for _ in range(20):
        picked = d._pick("cantonese", "ack")
        assert picked is not None
        assert float(picked["dur_s"]) <= 1.2, picked


def test_cap_off_keeps_old_behaviour(monkeypatch):
    """0=关：整池随机（可含长档），旧行为逐字节保留。"""
    monkeypatch.setenv("BOK_FILLER_MAX_DUR_S", "0")
    d = _Stub(_entries(1.0, 2.2))
    seen = {float(d._pick("cantonese", "ack")["dur_s"]) for _ in range(40)}
    assert seen == {1.0, 2.2}


def test_never_starves_the_filler(monkeypatch):
    """池里**只有**长档时必须照挑——静音比长垫话更差（饿死垫话是更坏的回归）。"""
    monkeypatch.setenv("BOK_FILLER_MAX_DUR_S", "1.2")
    d = _Stub(_entries(1.8, 2.2))
    picked = d._pick("cantonese", "ack")
    assert picked is not None and float(picked["dur_s"]) > 1.2


def test_missing_dur_is_treated_as_compliant(monkeypatch):
    """条目缺 dur_s：不猜、按旧行为放行（manifest 正常都带 dur_s）。"""
    monkeypatch.setenv("BOK_FILLER_MAX_DUR_S", "1.2")
    d = _Stub([{"file": "a.wav", "text": "x", "cat": "ack"}])
    picked = d._pick("cantonese", "ack")
    assert picked is not None and picked["file"] == "a.wav"
