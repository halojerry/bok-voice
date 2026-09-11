"""语速统一门禁(2026-09-12 用户定档:开场白/回复/罐头/垫音同速)。

四层出声必须同源解析 minimax_speed_for(zh/cantonese 1.2、en 1.0):
  1. 运行时回复(bidi/classic _ws_voice_setting.speed)
  2. 脚本直念线(_say_script/tts_cache 查找带 speed——tts_cache 自测另见)
  3. pregen 物化(scripts/pregen_tts speed=minimax_speed_for)
  4. 垫话资产(scripts/gen_filler_assets FILLERS cfg.speed)
任何一层偏离=同一通里「开场白快、回复慢」的听感分裂(call-a2705ed2 实证族)。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import LanguageState, MiniMaxTTS, minimax_speed_for  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, _REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_speed_for_language_tiers():
    assert minimax_speed_for("zh") == 1.2
    assert minimax_speed_for("cantonese") == 1.2
    assert minimax_speed_for("en") == 1.0
    assert minimax_speed_for("") == 1.0


def test_speed_env_kill_switch():
    old = os.environ.get("MINIMAX_SPEED")
    try:
        os.environ["MINIMAX_SPEED"] = "1.0"
        assert minimax_speed_for("zh") == 1.0
        os.environ["MINIMAX_SPEED"] = "1.5"
        assert minimax_speed_for("cantonese") == 1.5
    finally:
        if old is None:
            os.environ.pop("MINIMAX_SPEED", None)
        else:
            os.environ["MINIMAX_SPEED"] = old


def test_ws_voice_setting_carries_resolved_speed():
    # 运行时三条合成路径(classic WS/bidi/HTTP)共用 _ws_voice_setting——
    # speed 必须来自 resolved_speed(语言档),不是写死 1.0。
    os.environ.pop("MINIMAX_SPEED", None)
    zh = MiniMaxTTS(voice="v", language_state=LanguageState(lang="zh"))
    canto = MiniMaxTTS(voice="v", language_state=LanguageState(lang="cantonese"))
    en = MiniMaxTTS(voice="v", language_state=LanguageState(lang="en"))
    assert zh._ws_voice_setting("v")["speed"] == 1.2
    assert canto._ws_voice_setting("v")["speed"] == 1.2
    assert en._ws_voice_setting("v")["speed"] == 1.0


def test_filler_assets_speed_matches_runtime():
    # 垫话资产层烧死的 speed 必须与运行时同档(assets 与 pregen 物化共用)。
    gen = _load_script("gen_filler_assets")
    for lang, cfg in gen.FILLERS.items():
        assert float(cfg["speed"]) == minimax_speed_for(lang), (
            f"filler assets {lang} speed={cfg['speed']} != runtime {minimax_speed_for(lang)}"
        )
