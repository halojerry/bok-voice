"""LLM 慢速观测行（P2.c，spec 2026-09-29 v2 §5）。

纯打点不处置：流完成时平均 tps 低于 BOK_LLM_STALL_OBS_TPS（默认 5，"0"=关）
且 gen≥5（超短流噪声过滤）→ 汇总行尾附 LLM_STALL_OBS。治本（P1 生命周期
四件）后凭此行判断慢速是否绝迹；不绝迹再议处置（v1 三窗口方案已否决留档）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import _format_llm_metrics  # noqa: E402


def _m(ttft: float, tps: float, gen: int, prompt: int = 1000, cached: int = 900) -> SimpleNamespace:
    return SimpleNamespace(
        ttft=ttft, tokens_per_second=tps, completion_tokens=gen,
        prompt_tokens=prompt, prompt_cached_tokens=cached,
    )


def test_stall_obs_fires_on_slow_stream(monkeypatch):
    monkeypatch.delenv("BOK_LLM_STALL_OBS_TPS", raising=False)
    out = _format_llm_metrics(_m(0.9, 2.6, 28))
    assert "LLM_STALL_OBS tps=2.6 gen=28" in out


def test_stall_obs_silent_on_normal_stream(monkeypatch):
    monkeypatch.delenv("BOK_LLM_STALL_OBS_TPS", raising=False)
    out = _format_llm_metrics(_m(0.9, 21.3, 28))
    assert "LLM_STALL_OBS" not in out


def test_stall_obs_short_stream_filtered(monkeypatch):
    """gen<5 的超短流不打（两三个 token 的 tps 噪声无意义）。"""
    monkeypatch.delenv("BOK_LLM_STALL_OBS_TPS", raising=False)
    out = _format_llm_metrics(_m(0.9, 1.0, 3))
    assert "LLM_STALL_OBS" not in out


def test_stall_obs_env_zero_disables(monkeypatch):
    monkeypatch.setenv("BOK_LLM_STALL_OBS_TPS", "0")
    out = _format_llm_metrics(_m(0.9, 0.5, 40))
    assert "LLM_STALL_OBS" not in out


def test_stall_obs_env_threshold_override(monkeypatch):
    monkeypatch.setenv("BOK_LLM_STALL_OBS_TPS", "30")
    out = _format_llm_metrics(_m(0.9, 21.3, 28))
    assert "LLM_STALL_OBS tps=21.3" in out
