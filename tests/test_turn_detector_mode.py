"""TURN_DETECTION=detector 试点档（2026-09-25）：对象装配 + 回退语义。

A 线历史：v1-mini 无 Cantonese 校准档→停顿帧全判未完→提交撞 max_delay，
故 P2 默认 stt 句级提交。官方 unlikely_threshold 按语言覆写落地后，detector
档=env 一枚开关 + 阈值 JSON；构造失败必回退空串（框架默认 EOT）唔裸崩。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import agent_runtime.agent as agent_mod  # noqa: E402


def test_default_mode_is_stt(monkeypatch) -> None:
    monkeypatch.delenv("TURN_DETECTION", raising=False)
    assert agent_mod._turn_detection_mode_from_env() == "stt"


def test_literal_modes_pass_through(monkeypatch) -> None:
    for mode in ("stt", "vad", ""):
        monkeypatch.setenv("TURN_DETECTION", mode)
        assert agent_mod._turn_detection_from_env() == mode


def test_detector_mode_builds_object(monkeypatch) -> None:
    class _FakeDetector:
        def __init__(self, unlikely_threshold=None):
            self.threshold = unlikely_threshold

    import livekit.agents as lk_agents

    monkeypatch.setattr(lk_agents, "inference", type("M", (), {"TurnDetector": _FakeDetector}), raising=False)
    monkeypatch.setenv("TURN_DETECTION", "detector")
    monkeypatch.delenv("BOK_TURN_DETECTOR_THRESHOLD", raising=False)
    monkeypatch.delenv("BOK_TURN_DETECTOR_THRESHOLDS", raising=False)
    obj = agent_mod._turn_detection_from_env()
    assert isinstance(obj, _FakeDetector)
    assert obj.threshold is None


def test_detector_mode_reads_threshold_envs(monkeypatch) -> None:
    class _FakeDetector:
        def __init__(self, unlikely_threshold=None):
            self.threshold = unlikely_threshold

    import livekit.agents as lk_agents

    monkeypatch.setattr(lk_agents, "inference", type("M", (), {"TurnDetector": _FakeDetector}), raising=False)
    monkeypatch.setenv("TURN_DETECTION", "detector")
    monkeypatch.delenv("BOK_TURN_DETECTOR_THRESHOLDS", raising=False)
    monkeypatch.setenv("BOK_TURN_DETECTOR_THRESHOLD", "0.4")
    assert agent_mod._turn_detection_from_env().threshold == 0.4

    monkeypatch.setenv("BOK_TURN_DETECTOR_THRESHOLDS", '{"cantonese": 0.4, "zh": 0.5}')
    assert agent_mod._turn_detection_from_env().threshold == {"cantonese": 0.4, "zh": 0.5}


def test_detector_init_failure_falls_back(monkeypatch) -> None:
    class _Boom:
        def __init__(self, **kw):
            raise RuntimeError("no binary")

    import livekit.agents as lk_agents

    monkeypatch.setattr(lk_agents, "inference", type("M", (), {"TurnDetector": _Boom}), raising=False)
    monkeypatch.setenv("TURN_DETECTION", "detector")
    assert agent_mod._turn_detection_from_env() == ""
