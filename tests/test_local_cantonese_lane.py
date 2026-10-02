"""本地粤语 TTS 车道接线 pin 测试（2026-09-28）。

全链实测结论：Base-8bit 克隆 + 粤语参考音频 + `language="cantonese"` 请求
= 地道口语粤语（E2E_ONLY=cantonese 真栈 PASS，TTFA 167-374ms）。链路四段：

  web personas 上传（language=activeLang）
    → CP /api/tts/voices 透传 language → sidecar /v1/voices/register
  persona.reference_audio = {lang: voice_id} JSON
    → agent `_parse_voice_map` → `_collapse_voice_map`（整场同声按 persona 语言）
    → Qwen3TTSTTS 按 LanguageState.lang 取键（缺省回落 zh）
  渲染 payload language="cantonese"（原样透传）
    → sidecar `TTSService._normalize_language` → "chinese"（官方枚举无 cantonese；
      粤语音系由参考音频携带——社区实证 + 本仓 2026-09-28 E2E 实证）

本测钉住归一映射与音色图解析两处纯逻辑，防「官方枚举变化/重构」静默破坏
粤语车道。真栈读数见 AGENTS.md 2026-09-28 条目。
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "qwen3-tts-sidecar"))
sys.path.insert(0, str(ROOT / "apps" / "agent"))

app = pytest.importorskip("fastapi")  # sidecar 依赖 fastapi；缺失则整模块跳过
_mod = importlib.import_module("app")
TTSService = _mod.TTSService


class _Model:
    """替身：模拟 mlx 侧 get_supported_languages（官方 11 枚举，无 cantonese）。"""

    @staticmethod
    def get_supported_languages():
        return ["auto", "chinese", "english", "french", "german", "italian",
                "japanese", "korean", "portuguese", "russian", "spanish"]


class _NoGetter:
    """无 get_supported_languages 的模型（归一走内建映射）。"""


def test_cantonese_maps_to_chinese():
    assert TTSService._normalize_language(_Model(), "cantonese") == "chinese"
    assert TTSService._normalize_language(_Model(), "cantonese_chinese") == "chinese"


def test_supported_language_passthrough():
    assert TTSService._normalize_language(_Model(), "chinese") == "chinese"
    assert TTSService._normalize_language(_Model(), "english") == "english"


def test_unknown_falls_back_auto():
    assert TTSService._normalize_language(_Model(), "klingon") == "Auto"
    assert TTSService._normalize_language(_NoGetter(), "cantonese") == "chinese"
    assert TTSService._normalize_language(_NoGetter(), "") == "Auto"


def test_agent_voice_map_parse_and_collapse():
    """persona.reference_audio 三形态 → 分语言图 → 整场同声收敛。"""
    from agent_runtime.agent import _parse_voice_map, _collapse_voice_map

    assert _parse_voice_map({"cantonese": "v1"}) == {"cantonese": "v1"}
    assert _parse_voice_map('{"cantonese": "v1", "zh": "v2"}') == {"cantonese": "v1", "zh": "v2"}
    assert _parse_voice_map("legacy-id") == {"zh": "legacy-id"}
    # 粤语人设：cantonese 键优先收敛为整场同声。
    collapsed = _collapse_voice_map({"cantonese": "v1", "zh": "v2"}, "cantonese")
    assert collapsed.get("zh") == "v1"
    # 粤语键缺失时回落链 zh→cantonese→en→首个非空。
    collapsed2 = _collapse_voice_map({"zh": "v2"}, "cantonese")
    assert collapsed2.get("zh") == "v2"


def test_plugin_payload_sends_raw_cantonese():
    """源级 pin：Qwen3TTSTTS payload 原样下发 language（归一是 sidecar 单点）。"""
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
    assert '"language": tts_._language_state.lang' in src
    side = (ROOT / "services" / "qwen3-tts-sidecar" / "app.py").read_text(encoding="utf-8")
    assert 'requested in {"cantonese", "cantonese_chinese"}' in side


def test_local_lane_strips_minimax_markers_at_post_frames():
    """本地车道 MiniMax 标记剥离单点 pin（2026-09-28 语气手册接线）。

    Qwen3-TTS 无标记解析层（源码证），`(breath)`/`<#0.3#>` 会被逐字照念；
    全部文本 POST 收敛在 `_qwen3_tts_post_frames` 单点，必须在入口剥净。
    """
    from agent_runtime.voice_style import strip_voice_style

    # 白名单标记剥净。
    assert strip_voice_style("(breath)你好。") == "你好。"
    # 停顿 token 剥净。
    assert "<#" not in strip_voice_style("好。<#0.3#>我帮你查。")
    # 无标记文本逐字保留。
    assert strip_voice_style("嗯，我帮你查一下。") == "嗯，我帮你查一下。"
    # 源级 pin：剥离在 post_frames 入口（覆盖直念+LLM 流全部四个调用点）。
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py").read_text(encoding="utf-8")
    anchor = src.index("async def _qwen3_tts_post_frames")
    body = src[anchor : anchor + 2500]
    assert "text = strip_voice_style(text)" in body
