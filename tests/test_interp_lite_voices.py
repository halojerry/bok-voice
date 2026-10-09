"""W8-A3 interp_lite 音色契约（2026-10-09，Ethan 三令五申「同传音色=按语言选
MiniMax 目录」）+ echo-dedup 回归 lite 装配钉。

- 音色唯一解析序=会话级 voices_json（按语言键）> 设置分语言三键
  （speaker_zh/speaker_cantonese/speaker_en）> 硬编码默认；**人设路线已死**——
  persona 语音路径在 interp_lite 内零残留（worker 对历史 dispatch persona_id
  只打一行忽略告警，绝不回源拉人设）。
- 语言键归一与旧线单源（interp_lite.config.norm_lang is interpret._norm_lang），
  七语键 zh/cantonese/en/de/fr/ja/pt 全表钉死（_norm_lang 2026-10-06 W2 四语扩容）。
- echo-dedup（旧线 _InterpEchoDedup 单源）挂 lite worker user_input 入口：
  kill-switch 同键 BOK_INTERP_ECHO_DEDUP，命中=不落原文行/不进队。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LITE_DIR = ROOT / "apps" / "agent" / "agent_runtime" / "interp_lite"
if str(ROOT / "apps" / "agent") not in sys.path:
    sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.interp_lite.config import norm_lang  # noqa: E402
from agent_runtime.interp_lite.providers.tts_minimax import (  # noqa: E402
    _VOICE_DEFAULTS,
    voice_map_for,
)
from agent_runtime.interpret import (  # noqa: E402
    _echo_dedup_enabled,
    _InterpEchoDedup,
    _norm_lang,
    _parse_session_voices,
)

WORKER_SRC = (LITE_DIR / "worker.py").read_text(encoding="utf-8")

# ---- 语言键归一：七语同源 ----


def test_norm_lang_same_source_as_old_line():
    """lite 的 norm_lang 必须=旧线 _norm_lang 同一对象（单源复出口，不双轨）。"""
    assert norm_lang is _norm_lang


def test_session_voices_seven_lang_keys_normalized():
    raw = json.dumps(
        {
            "ZH": "v-zh",
            "普通话": "v-zh2",
            "cantonese": "v-canto",
            "粤": "v-canto2",
            "EN": "v-en",
            "de": "v-de",
            "German": "v-de2",
            "fr": "v-fr",
            "ja": "v-ja",
            "pt": "v-pt",
        }
    )
    voices = _parse_session_voices(raw)
    assert voices == {
        "zh": "v-zh2",  # 同键归一后写覆盖（dict 迭代序）
        "cantonese": "v-canto2",
        "en": "v-en",
        "de": "v-de2",
        "fr": "v-fr",
        "ja": "v-ja",
        "pt": "v-pt",
    }
    assert set(voices) == {"zh", "cantonese", "en", "de", "fr", "ja", "pt"}


def test_session_voices_bad_shapes_never_raise():
    """坏 JSON/形状不对/空值/未知键 → 丢弃，绝不打死通话（全链回落设置>默认）。"""
    assert _parse_session_voices("") == {}
    assert _parse_session_voices(None) == {}
    assert _parse_session_voices("{broken json") == {}
    assert _parse_session_voices('["zh","en"]') == {}
    assert _parse_session_voices('{"zh": "", "klingon": "v", "en": "v-en"}') == {"en": "v-en"}


# ---- 音色唯一解析序：会话级 > 设置三键 > 默认 ----


def test_voice_map_defaults_three_langs():
    m = voice_map_for({}, "zh", None)
    assert m == dict(_VOICE_DEFAULTS)
    assert set(m) == {"zh", "cantonese", "en"}


def test_voice_map_settings_three_keys():
    cfg = {
        "speaker_zh": "Chinese (Mandarin)_Warm_Girl",
        "speaker_cantonese": "Cantonese_balanced_voice_vv2",
        "speaker_en": "English_magnetic_voiced_man",
    }
    m = voice_map_for(cfg, "en", None)
    assert m["zh"] == cfg["speaker_zh"]
    assert m["cantonese"] == cfg["speaker_cantonese"]
    assert m["en"] == cfg["speaker_en"]


def test_voice_map_session_overrides_settings_per_language():
    """会话级按语言键覆盖——只有点名的那一语换声，其余语言留在设置/默认。"""
    cfg = {"speaker_zh": "settings-zh", "speaker_en": "settings-en"}
    session = {"zh": "session-zh"}
    m = voice_map_for(cfg, "en", session)
    assert m["zh"] == "session-zh"  # 会话级点名 zh → 覆盖设置
    assert m["en"] == "settings-en"  # 未点名的语言留在设置
    assert m["cantonese"] == _VOICE_DEFAULTS["cantonese"]  # 都没配 → 默认


def test_voice_map_session_four_lang_slots():
    """四语目标（W2 扩容）同走会话级槽位；默认表只兜三语，四语不发明。"""
    m = voice_map_for({}, "ja", {"ja": "Japanese_Calm_Woman", "de": "German_Serenity"})
    assert m["ja"] == "Japanese_Calm_Woman"
    assert m["de"] == "German_Serenity"
    assert "fr" not in m and "pt" not in m
    assert set(m) >= {"zh", "cantonese", "en"}


def test_voice_map_local_qwen3_voice_filtered():
    """本地 Qwen3 音色（设置页/会话级误配）必须被过滤防 MiniMax 2054——
    过滤后回落下一层（设置>默认），绝不把本地 id 发上云。"""
    cfg = {"speaker_zh": "Vivian"}  # 本地预设音色
    m = voice_map_for(cfg, "zh", {"en": "agent-clone123"})
    assert m["zh"] == _VOICE_DEFAULTS["zh"]  # 设置层本地音色被滤 → 默认
    assert m["en"] == _VOICE_DEFAULTS["en"]  # 会话层克隆 id 被滤 → 默认
    # 云端有效音色不受过滤
    m2 = voice_map_for({}, "zh", {"zh": "Chinese (Mandarin)_Warm_Girl"})
    assert m2["zh"] == "Chinese (Mandarin)_Warm_Girl"


def test_voice_map_accepts_raw_json_string_session_voices():
    """会话级入参既接受已解析 dict 也接受原始 voices_json 字符串（CP 直传形态）。"""
    m = voice_map_for({}, "zh", '{"zh": "Chinese (Mandarin)_Warm_Girl"}')
    assert m["zh"] == "Chinese (Mandarin)_Warm_Girl"


# ---- persona 语音路径零残留 ----


def test_persona_voice_path_zero_residue_in_lite():
    """人设路线已死：interp_lite 内 persona 语音解析链零残留——不 import
    _persona_voice_map、不调 get_persona、无 persona_voices 形参/变量。"""
    banned = ("_persona_voice_map", "get_persona", "persona_voices")
    for py in sorted(LITE_DIR.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        src = py.read_text(encoding="utf-8")
        for token in banned:
            assert token not in src, f"{py.name} 残留 persona 语音路径 token={token!r}"


def test_worker_metadata_persona_id_ignored_with_warning():
    """历史 dispatch persona_id：只忽略+显式告警行（契约 docstring 同步）。"""
    assert "dispatch persona_id present — ignored" in WORKER_SRC
    assert 'meta.get("persona_id")' in WORKER_SRC
    # 音色唯一序注释钉在装配点
    assert "voices_json" in WORKER_SRC


# ---- echo-dedup 回归 lite：装配源级 pin ----


def test_echo_dedup_lite_wiring_pins():
    """user_input 入口挂旧线单源判重器：实例化+kill-switch+命中整轮丢弃
    （不落原文行/不进队）+译文参考料滚动账。"""
    assert "echo_dedup = _InterpEchoDedup()" in WORKER_SRC
    assert "if _echo_dedup_enabled():" in WORKER_SRC
    assert "INTERP_ECHO_DROP reason=" in WORKER_SRC
    assert "own_translations.append(text)" in WORKER_SRC
    # 命中路径先于落库/入队：check 块位置必须在前（与旧线同语义）
    assert WORKER_SRC.index("echo_dedup.check(") < WORKER_SRC.index('_add_turn(f"原文：')


def test_echo_dedup_single_source_runtime_identity():
    """worker 用的判重器=旧线同一类对象（不 fork 不复制）。"""
    import agent_runtime.interp_lite.worker as lite_worker
    import agent_runtime.interpret as old_interp

    assert lite_worker._InterpEchoDedup is old_interp._InterpEchoDedup
    assert lite_worker._echo_dedup_enabled is old_interp._echo_dedup_enabled


def test_echo_dedup_kill_switch_shared(monkeypatch):
    """kill-switch 与旧线同键 BOK_INTERP_ECHO_DEDUP（缺省开，0=关）。"""
    monkeypatch.delenv("BOK_INTERP_ECHO_DEDUP", raising=False)
    assert _echo_dedup_enabled() is True
    monkeypatch.setenv("BOK_INTERP_ECHO_DEDUP", "0")
    assert _echo_dedup_enabled() is False


def test_echo_dedup_lite_smoke_with_injected_clock():
    """lite 场景冒烟：同文本窗内重复=dup-final；译文回声=self-heard；窗过=放行。"""
    d = _InterpEchoDedup(clock=lambda: 0.0)
    assert d.check("Hello, how are you?", now=0.0) == ""
    assert d.check("Hello. How are you?", now=1.0) == "dup-final"  # 归一后同文本
    d2 = _InterpEchoDedup(clock=lambda: 0.0)
    # self-heard:自家译文被输入侧再转写（sim ≥0.85，语言相同时才可能高相似）
    assert d2.check("hello how are you", now=0.0, own_translations=("Hello. How are you?",)) == "self-heard"
    d3 = _InterpEchoDedup(clock=lambda: 0.0)
    assert d3.check(" totally different words ", now=0.0, own_translations=("你好呀",)) == ""
