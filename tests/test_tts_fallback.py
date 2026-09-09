"""TTS 同音色换档回退链单测(2026-09-09,官方 tts.FallbackAdapter)。

用户拍板:回退只准 MiniMax hd↔turbo 换档(音色 ID 跨档通用,换档不换人);
MiniMax→本地 Qwen3 被否——音色两套人,中途换客服违反全场同音色铁律。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _alt_minimax_model  # noqa: E402
from agent_runtime.providers.livekit_plugins import MiniMaxTTS  # noqa: E402
from livekit.agents import tts as agents_tts  # noqa: E402


def test_alt_model_mapping():
    assert _alt_minimax_model("speech-2.8-hd") == "speech-2.6-turbo"
    assert _alt_minimax_model("speech-2.6-turbo") == "speech-2.8-hd"
    assert _alt_minimax_model("") == "speech-2.8-hd"


def test_model_override_beats_env():
    os.environ["MINIMAX_MODEL"] = "speech-2.8-hd"
    try:
        primary = MiniMaxTTS(voice="Cantonese_GentleLady")
        backup = MiniMaxTTS(voice="Cantonese_GentleLady", model_override="speech-2.6-turbo")
        assert primary._model() == "speech-2.8-hd", "主实例仍读 env"
        assert backup._model() == "speech-2.6-turbo", "回退实例用 override(与主实例同 env 会拿同一档)"
        assert primary.resolved_voice() == backup.resolved_voice(), "换档不换人:音色必须一致"
    finally:
        os.environ.pop("MINIMAX_MODEL", None)


def test_fallback_adapter_wiring_order():
    os.environ["MINIMAX_MODEL"] = "speech-2.8-hd"
    try:
        primary = MiniMaxTTS(voice="Cantonese_GentleLady")
        backup = MiniMaxTTS(voice="Cantonese_GentleLady", model_override=_alt_minimax_model(primary._model()))
        adapter = agents_tts.FallbackAdapter([primary, backup])
        # 官方 Adapter 是 TTS 子类,可直接被 CachedTTS 再包
        assert isinstance(adapter, agents_tts.TTS)
        assert adapter.num_channels == primary.num_channels
        assert adapter.sample_rate == max(primary.sample_rate, backup.sample_rate)
    finally:
        os.environ.pop("MINIMAX_MODEL", None)
