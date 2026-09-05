"""A 线每通对话语言固定（产品决策，取代逐轮语言跟随）。

粤语通话全程粤语、中文全程中文、英文全程英文，中途不切换。会话装配时把
ASR/LLM/TTS 三方语言变量一次钉死（消灭 zh→en 切换双胞胎 ~4.3-10s p95 与
每轮【用户语言】前缀改写的 KV-cache 失配）。防回归点：

1. `_call_language` 装配解析（人设→对象→zh），ASR/LLM/TTS 共用同一决定；
2. ASR 恒钉定：PinnedLanguageState + pin_language=True，hint 整场=通话语言
   （zh 也下发 Chinese；sidecar /api/start language= → finish hint 随开语言）。
   设置 asr.language_mode=fixed + 显式 language 仍优先；
3. TTS 语言态钉死通话语言：_resolve_voice/_speech_lang 整通恒定；
   MiniMax language_boost 按通话语言注入（部署显式设置优先）；
4. 逐轮钩子不再做语言锚定：无 set_user_language、无 sticky 跟随、无语言切换
   标记——【用户语言】规则整通字节静态（流程推进/REFUSE 标记保留不动）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _apply_minimax_language_boost,
    _call_asr_pin_language,
    _call_language,
    _language_boost_for,
)

_AGENT_SRC = Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py"


# ---- 1. 装配解析：人设(AI)语言 → 对象(客户)语言 → zh ----


def test_call_language_resolution_precedence():
    # 人设优先（用户选了它，就是期望 AI 全程用它说话）。
    assert _call_language({"language": "English"}, {"language": "cantonese"}) == "en"
    # 未设人设 → 对象语言（中文写法/别名经 _normalize_lang 归一）。
    assert _call_language(None, {"language": "粤"}) == "cantonese"
    assert _call_language({}, {"language": "普通话"}) == "zh"
    # 都未设 / 不支持语言（vi 等）→ 普通话兜底。
    assert _call_language({}, {}) == "zh"
    assert _call_language(None, None) == "zh"
    assert _call_language({"language": "vi"}, {"language": "vi"}) == "zh"


# ---- 2. ASR 恒钉定：显式设置优先，否则钉到通话语言 ----


def test_settings_fixed_language_overrides_call_language():
    # asr.language_mode=fixed + asr.language 显式合法值 → 部署级覆盖仍赢。
    assert _call_asr_pin_language({"language_mode": "fixed", "language": "English"}, "cantonese") == "en"
    assert _call_asr_pin_language({"language_mode": "fixed", "language": "cantonese"}, "zh") == "cantonese"
    # mode=auto（缺键同）在 A 线=「钉到本通语言」（滞回跟随已退役）。
    assert _call_asr_pin_language({}, "cantonese") == "cantonese"
    assert _call_asr_pin_language({"language_mode": "auto"}, "en") == "en"
    # fixed 但值不合法 → 回落通话语言（绝不哑火）。
    assert _call_asr_pin_language({"language_mode": "fixed", "language": "fr"}, "zh") == "zh"


def test_asr_pinned_state_and_hint_all_call_languages():
    from agent_runtime.providers.livekit_plugins import PinnedLanguageState, _asr_language_hint

    # 客户中途讲其它语言的强证据也改不了钉定值；hint 三语全钉（zh 也下发 Chinese，
    # 不再 auto）——sidecar /api/start language= 开语言即 finish hint（en 无需额外接线）。
    other_evidence = {
        "cantonese": ("en", "hello there how are you doing today"),
        "zh": ("cantonese", "有冇人知道灣仔活道係點去？"),
        "en": ("zh", "你好，请问这个理赔流程是怎么办理的？"),
    }
    for lang, hint in (("cantonese", "Cantonese"), ("zh", "Chinese"), ("en", "English")):
        pinned = PinnedLanguageState(lang=lang)
        olang, otext = other_evidence[lang]
        pinned.update(olang, otext)
        assert pinned.lang == lang, lang
        assert _asr_language_hint(pinned.lang, True) == hint, lang


def test_asr_wiring_uses_pinned_state_and_full_pin():
    from agent_runtime.providers.livekit_plugins import PinnedLanguageState, Qwen3ASRSTT

    pinned = PinnedLanguageState(lang="cantonese")
    asr = Qwen3ASRSTT(base_url="http://127.0.0.1:8787", language_state=pinned, pin_language=True)
    assert asr._language_state is pinned
    assert asr._pin_language is True


# ---- 3. TTS 语言态整通钉死 + language_boost 按通话语言 ----


def test_tts_language_state_pinned_whole_call():
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS, PinnedLanguageState

    tts = MiniMaxTTS(
        voice={"zh": "zh-voice-id", "cantonese": "ca-voice-id"},
        language_state=PinnedLanguageState(lang="cantonese"),
    )
    assert tts._resolve_voice() == "ca-voice-id"
    # ASR 强证据 update 进不来（update 是 no-op）→ 音色/罐头语言整通恒定。
    tts._language_state.update("zh", "你好，我想问一下理赔的流程是怎么办理的")
    assert tts._language_state.lang == "cantonese"
    assert tts._resolve_voice() == "ca-voice-id"
    assert tts._speech_lang() == "cantonese"


def test_language_boost_mapping_matches_b_line():
    # MiniMax 外部枚举字面量（术语门禁白名单范畴），与 interpret.py boost_map 同值。
    assert _language_boost_for("zh") == "Chinese"
    assert _language_boost_for("cantonese") == "Chinese,Yue"
    assert _language_boost_for("en") == "English"
    assert _language_boost_for("vi") == ""
    assert _language_boost_for("") == ""


def test_apply_boost_injects_per_call(monkeypatch):
    # 每段各自 delenv:livekit 默认每 job 一进程,新 job 启动时 env 缺失才注入。
    monkeypatch.delenv("MINIMAX_LANGUAGE_BOOST", raising=False)
    assert _apply_minimax_language_boost("cantonese") == "Chinese,Yue"
    monkeypatch.delenv("MINIMAX_LANGUAGE_BOOST", raising=False)
    assert _apply_minimax_language_boost("en") == "English"
    monkeypatch.delenv("MINIMAX_LANGUAGE_BOOST", raising=False)
    assert _apply_minimax_language_boost("zh") == "Chinese"
    # 已注入后本进程内再调(同进程跑多 job 的非默认场景):preset 留存唔重设。
    assert _apply_minimax_language_boost("en") == "Chinese"


def test_apply_boost_env_preset_wins(monkeypatch):
    # 部署显式预设 → agent 不覆盖（deployment-override：只在 env 缺失时注入）。
    monkeypatch.setenv("MINIMAX_LANGUAGE_BOOST", "English")
    assert _apply_minimax_language_boost("cantonese") == "English"
    # 空串预设 = 有意禁用 boost，同样唔覆盖。
    monkeypatch.setenv("MINIMAX_LANGUAGE_BOOST", "")
    assert _apply_minimax_language_boost("zh") == ""


# ---- 4. 逐轮钩子退役语言锚定：【用户语言】整通字节静态 ----


def _turn_hook_body() -> str:
    src = _AGENT_SRC.read_text(encoding="utf-8")
    start = src.index("async def on_user_turn_completed")
    end = src.index("async def on_user_turn_exceeded", start)
    return src[start:end]


def test_turn_hook_has_no_language_switching():
    body = _turn_hook_body()
    # 无逐轮 set_user_language、无 sticky 跟随、无语言切换标记。
    assert "set_user_language" not in body
    assert "_sticky_reply_language" not in body
    assert "回复语言切换" not in body
    # 流程逻辑原样保留：WhatsApp 侦测/规则推进/收尾标记都还在。
    assert "detect_whatsapp_signal" in body
    assert "rule_verdict" in body
    assert "_invalidate_stale_preemptive" in body
    # set_user_language 全文件只出现一次（会话装配钉死那一处）。
    src = _AGENT_SRC.read_text(encoding="utf-8")
    assert src.count("context_state.set_user_language") == 1


def test_prefix_byte_stable_across_mixed_language_turns():
    from agent_runtime.providers.livekit_plugins import ContextState

    ctx = ContextState(account_id="acc-001")
    ctx.set_user_language("cantonese")  # 装配时一次，之后无任何逐轮改写
    ctx.set_flow("对话按 2 步流程推进:\n第1步:确认", "流程第 1/2 步")
    p0 = ctx.render_instruction_prefix()
    # 混语言轮次（客户中途讲普通话/英文再切回粤语）——旧 sticky 会改写【用户语言】
    # 并落语言切换标记；新政策前缀逐轮字节不变（记忆/当前步只在尾部）。
    turns = [
        ("你好，我想问一下理赔怎么办", "好的，您讲"),
        ("hello, can you hear me", "yes, please go ahead"),
        ("唔該，我想查下單號", "收到，您講"),
    ]
    for user, assistant in turns:
        ctx.add_summary("user", user)
        ctx.add_summary("assistant", assistant)
        ctx.set_flow_current("流程第 2/2 步")
        assert ctx.render_instruction_prefix() == p0, user
        full = ctx.render_system_message()
        assert full.startswith(p0) and full.endswith(ctx.render_context_tail())
