"""本地 TTS 音色解析闸（2026-09-25）：MiniMax ID 勿直传 sidecar。

实弹背景：persona/设置三键常驻 MiniMax 音色 ID（Cantonese_GentleLady），
Qwen3 sidecar `_validate_speakers` ValueError → 整轮 0 字节哑轮（5/7 轮哑）。
解析链=预置∪已注册克隆放行 / 其余回落语言档（env 覆写）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    _qwen3_speaker_union,
    _QWEN3_FALLBACK_SEEN,
    _QWEN3_SPEAKER_CACHE,
    _QWEN3_TTS_PRESETS,
    _resolve_local_voice,
)

DOWN = "http://127.0.0.1:59999"  # 无服务 → 并集退纯预置


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.delitem(_QWEN3_SPEAKER_CACHE, DOWN, raising=False)
    _QWEN3_FALLBACK_SEEN.clear()
    yield
    monkeypatch.delitem(_QWEN3_SPEAKER_CACHE, DOWN, raising=False)
    _QWEN3_FALLBACK_SEEN.clear()


def test_preset_passes_through() -> None:
    assert _resolve_local_voice("vivian", "cantonese", DOWN) == "vivian"
    assert _resolve_local_voice("serena", "en", DOWN) == "serena"


def test_minimax_id_falls_back_per_language() -> None:
    assert _resolve_local_voice("Cantonese_GentleLady", "cantonese", DOWN) == "vivian"
    assert _resolve_local_voice("Chinese_wenrounvxing", "zh", DOWN) == "vivian"
    assert _resolve_local_voice("socialmedia_female_2_v1", "en", DOWN) == "serena"
    # 未知语言键 → zh 档回落
    assert _resolve_local_voice("whatever", "jp", DOWN) == "vivian"


def test_empty_falls_back_to_default() -> None:
    assert _resolve_local_voice("", "en", DOWN) == "serena"


def test_env_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("QWEN3_TTS_VOICE_CANTONESE", "uncle_fu")
    assert _resolve_local_voice("Cantonese_GentleLady", "cantonese", DOWN) == "uncle_fu"


def test_registered_clone_passes(monkeypatch) -> None:
    monkeypatch.setitem(
        _QWEN3_SPEAKER_CACHE, DOWN, _QWEN3_TTS_PRESETS | {"persona-abc123"}
    )
    assert _resolve_local_voice("persona-abc123", "zh", DOWN) == "persona-abc123"


def test_fallback_logged_once(capsys) -> None:
    _resolve_local_voice("Cantonese_GentleLady", "cantonese", DOWN)
    _resolve_local_voice("Cantonese_GentleLady", "cantonese", DOWN)
    out = capsys.readouterr().out
    assert out.count("voice fallback") == 1
    assert "Cantonese_GentleLady -> vivian" in out


def test_union_down_sidecar_is_presets_only() -> None:
    assert _qwen3_speaker_union(DOWN) == set(_QWEN3_TTS_PRESETS)
