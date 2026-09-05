"""P2 语言/音色/ASR 切换：tts.voice_mode 组装、asr.language_mode 钉定、缺键向后兼容。

防回归点：
1. tts.voice_mode 缺键/非法值 → single（今日 collapse 行为，结果与旧内联算法一致）；
   per_language 保留 {zh,cantonese,en} 三键（MiniMaxTTS._resolve_voice 按滞回后
   的 language_state.lang 逐轮换声）。
2. asr.language_mode=fixed → B 线同传同姿势（pin_language=True + 钉定语言态，
   per-request hint 三种全钉）；缺键/未配语言/不合法值 → auto（今日锚定+滞回，绝不哑火）。
3. 设置以 JSON blob 存储：无新键的旧档读出后行为与新默认完全一致（零迁移）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _assemble_minimax_voice_map,
    _collapse_voice_map,
    _parse_voice_map,
    _resolve_asr_language_mode,
    _resolve_tts_voice_mode,
)

# 云端示例音色（非本地 Qwen3 预设/克隆；音色 ID 是不透明标识符，MiniMax 风格）。
_ZH_V = "zhiyan_meet_feminine"
_CA_V = "Cantonese_GentleLady"
_EN_V = "English_trustworthy_lady"


def _persona_with_three_keys() -> dict:
    return {
        "language": "cantonese",
        "reference_audio": json.dumps({"zh": _ZH_V, "cantonese": _CA_V, "en": _EN_V}),
    }


# ---- tts.voice_mode 解析（缺键=single，今日行为零变化） ----


def test_voice_mode_defaults_to_single():
    assert _resolve_tts_voice_mode({}) == "single"
    assert _resolve_tts_voice_mode({"voice_mode": "single"}) == "single"
    assert _resolve_tts_voice_mode({"voice_mode": ""}) == "single"
    assert _resolve_tts_voice_mode({"voice_mode": None}) == "single"
    assert _resolve_tts_voice_mode({"voice_mode": "bogus"}) == "single"


def test_voice_mode_per_language():
    assert _resolve_tts_voice_mode({"voice_mode": "per_language"}) == "per_language"
    assert _resolve_tts_voice_mode({"voice_mode": "PER_LANGUAGE"}) == "per_language"


# ---- voice map 组装：single collapse / per_language 保留三键 ----


def test_voice_map_single_collapses_to_one_voice():
    # single（默认）：persona 三键异值 → 收敛成人设主语言（粤语）那把声放 zh 键。
    m = _assemble_minimax_voice_map(
        persona=_persona_with_three_keys(), tts_cfg={}, greet_lang="zh", voice_mode="single"
    )
    assert m == {"zh": _CA_V}


def test_voice_map_per_language_keeps_three_keys_and_resolves_per_turn():
    m = _assemble_minimax_voice_map(
        persona=_persona_with_three_keys(), tts_cfg={}, greet_lang="zh", voice_mode="per_language"
    )
    assert m == {"zh": _ZH_V, "cantonese": _CA_V, "en": _EN_V}
    # 三键 map 下 MiniMaxTTS._resolve_voice 按滞回后的 language_state.lang 逐轮换声。
    from agent_runtime.providers.livekit_plugins import LanguageState, MiniMaxTTS

    ls = LanguageState()
    ls.lang = "cantonese"
    tts = MiniMaxTTS(voice=m, language_state=ls)
    assert tts._resolve_voice() == _CA_V
    ls.lang = "en"
    assert tts._resolve_voice() == _EN_V
    ls.lang = "zh"
    assert tts._resolve_voice() == _ZH_V


def test_voice_map_absent_key_is_identical_to_legacy_collapse():
    # 设置无 voice_mode 键（旧档 JSON blob）→ single → 与今日 collapse 结果一致。
    persona = _persona_with_three_keys()
    legacy = _collapse_voice_map(_parse_voice_map(persona["reference_audio"]), "cantonese")
    for cfg in ({}, {"voice_mode": "single"}):
        m = _assemble_minimax_voice_map(
            persona=persona, tts_cfg=cfg, greet_lang="zh", voice_mode=_resolve_tts_voice_mode(cfg)
        )
        assert m == legacy == {"zh": _CA_V}


def test_voice_map_settings_only_keys():
    # 无 persona 绑定：设置页三键。single 按锚语言（greet_lang=粤语）取主声。
    tts_cfg = {"speaker_zh": _ZH_V, "speaker_cantonese": _CA_V, "speaker_en": _EN_V}
    single = _assemble_minimax_voice_map(
        persona=None, tts_cfg=tts_cfg, greet_lang="cantonese", voice_mode="single"
    )
    assert single == {"zh": _CA_V}
    per = _assemble_minimax_voice_map(
        persona=None, tts_cfg=tts_cfg, greet_lang="cantonese", voice_mode="per_language"
    )
    assert per == {"zh": _ZH_V, "cantonese": _CA_V, "en": _EN_V}


def test_voice_map_global_single_speaker_stays_single_in_per_language():
    # 全局 speaker 只组 zh 键 → per_language 下解析仍回落 zh 键（等效整场同声，不炸）。
    m = _assemble_minimax_voice_map(
        persona=None, tts_cfg={"speaker": _CA_V}, greet_lang="zh", voice_mode="per_language"
    )
    assert m == {"zh": _CA_V}


def test_voice_map_filters_local_qwen3_voices_both_modes():
    # 本地 Qwen3 预设/克隆音色发给 MiniMax 会 2054 voice not exist：两模式都过滤。
    tts_cfg = {"speaker_zh": "vivian", "speaker_cantonese": _CA_V, "speaker_en": "agent-clone-x"}
    per = _assemble_minimax_voice_map(
        persona=None, tts_cfg=tts_cfg, greet_lang="cantonese", voice_mode="per_language"
    )
    assert per == {"cantonese": _CA_V}
    single = _assemble_minimax_voice_map(
        persona=None, tts_cfg=tts_cfg, greet_lang="cantonese", voice_mode="single"
    )
    assert single == {"zh": _CA_V}


# ---- asr.language_mode 解析（缺键=auto，今日锚定+滞回零变化） ----


def test_asr_language_mode_defaults_to_auto():
    assert _resolve_asr_language_mode({}) == ("auto", "")
    assert _resolve_asr_language_mode({"language_mode": "auto"}) == ("auto", "")
    assert _resolve_asr_language_mode({"language_mode": "AUTO"}) == ("auto", "")
    assert _resolve_asr_language_mode({"language_mode": "bogus"}) == ("auto", "")


def test_asr_language_mode_fixed_pins_language():
    assert _resolve_asr_language_mode({"language_mode": "fixed", "language": "cantonese"}) == ("fixed", "cantonese")
    assert _resolve_asr_language_mode({"language_mode": "fixed", "language": "zh"}) == ("fixed", "zh")
    assert _resolve_asr_language_mode({"language_mode": "fixed", "language": "English"}) == ("fixed", "en")


def test_asr_language_mode_fixed_without_valid_language_falls_back_to_auto():
    # fixed 但没配语言/值不合法 → 安全回落 auto（绝不哑火成无 hint）。
    assert _resolve_asr_language_mode({"language_mode": "fixed"}) == ("auto", "")
    assert _resolve_asr_language_mode({"language_mode": "fixed", "language": ""}) == ("auto", "")
    assert _resolve_asr_language_mode({"language_mode": "fixed", "language": "fr"}) == ("auto", "")


# ---- 钉定语言态 + per-request hint（B 线同传同姿势） ----


def test_pinned_language_state_never_drifts():
    from agent_runtime.providers.livekit_plugins import LanguageState, PinnedLanguageState

    pinned = PinnedLanguageState(lang="cantonese")
    # 强证据（明确标签 + 地道粤语字）也改不了钉定值。
    pinned.update("en", "hello there how are you")
    assert pinned.lang == "cantonese"
    pinned.update("zh", "你好请问有什么可以帮您")
    assert pinned.lang == "cantonese"
    # 对照：auto 模式的共享态强证据会跟随。
    shared = LanguageState()
    shared.lang = "zh"
    shared.update("cantonese", "有冇人知道灣仔活道係點去")
    assert shared.lang == "cantonese"


def test_pinned_state_per_request_hint_all_langs_pinned():
    from agent_runtime.providers.livekit_plugins import PinnedLanguageState, _asr_language_hint

    for lang, expect in (("cantonese", "Cantonese"), ("zh", "Chinese"), ("en", "English")):
        pinned = PinnedLanguageState(lang=lang)
        # pin=True：三种全钉（zh 不再转 auto）——B 线同传/固定模式同口径。
        assert _asr_language_hint(pinned.lang, pin=True) == expect
    # auto 模式对照：zh 保持 auto 容忍夹英文。
    assert _asr_language_hint("zh", pin=False) == ""


def test_qwen3_asr_wiring_fixed_mode():
    from agent_runtime.providers.livekit_plugins import PinnedLanguageState, Qwen3ASRSTT

    pinned = PinnedLanguageState(lang="cantonese")
    asr = Qwen3ASRSTT(base_url="http://127.0.0.1:8787", language_state=pinned, pin_language=True)
    assert asr._pin_language is True
    assert asr._language_state is pinned
    assert asr._language_state.lang == "cantonese"


def test_qwen3_asr_wiring_auto_mode_untouched():
    from agent_runtime.providers.livekit_plugins import LanguageState, Qwen3ASRSTT

    shared = LanguageState()
    shared.lang = "zh"
    asr = Qwen3ASRSTT(base_url="http://127.0.0.1:8787", language_state=shared)
    # 缺省 pin_language=False：今日通话模式口径（cantonese/en 钉、zh auto）。
    assert asr._pin_language is False
    assert asr._language_state is shared


# ---- 设置面：schema 缺键向后兼容 + 默认值单轨 ----


def test_provider_settings_schema_defaults_and_old_blob_compat():
    from control_plane.schemas import ProviderSettings

    fresh = ProviderSettings()
    assert fresh.voice_mode == "single"
    assert fresh.language_mode == "auto"
    # 旧档 JSON blob（无新键）反序列化 → 默认值，与缺键行为一致（零迁移）。
    old = ProviderSettings(**{"provider": "minimax", "api_key": "k"})
    assert old.voice_mode == "single"
    assert old.language_mode == "auto"


def test_repository_default_settings_carry_new_keys():
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

    defaults = SqlAlchemyBusinessRepository.default_settings()
    assert defaults["tts"]["voice_mode"] == "single"
    assert defaults["asr"]["language_mode"] == "auto"
    assert defaults["asr"]["language"] == ""


# ---- P3：单会话记忆默认值 ----


def test_history_turns_default_raised_to_8(monkeypatch):
    # LLM_HISTORY_TURNS 缺省 4→8：30 对 > 2×8 → 一次剪回 8 对（滞回内纯追加命中缓存）。
    monkeypatch.delenv("LLM_HISTORY_TURNS", raising=False)
    from livekit.agents import llm as lk_llm

    from agent_runtime.providers.livekit_plugins import ContextAwareLLM, ContextState

    captured = {}

    class _Inner(lk_llm.LLM):
        def chat(self, *, chat_ctx, **kwargs):  # noqa: ANN001, ANN003
            captured["items"] = list(chat_ctx.items)
            return "sentinel"

    ctx_llm = ContextAwareLLM(_Inner(), ContextState(account_id="acc"))
    chat_ctx = lk_llm.ChatContext()
    # 生产里 agent 的 system 指令永远排第一(PausableAgent instructions);
    # add_message 会把 str content 转成 list,与真实 chat_ctx 同构。
    chat_ctx.add_message(role="system", content="你是客服。")
    for i in range(30):
        chat_ctx.add_message(role="user", content=f"u{i}")
        chat_ctx.add_message(role="assistant", content=f"a{i}")
    out = ctx_llm.chat(chat_ctx=chat_ctx)
    assert out == "sentinel"
    roles = [m.role for m in captured["items"]]
    assert roles[0] == "system"
    dialog = [r for r in roles[1:] if r in ("user", "assistant")]
    assert dialog == ["user", "assistant"] * 8
