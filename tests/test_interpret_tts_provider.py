"""B 线 TTS/LLM provider 组装单测：MiniMax 分支、Qwen3 本地兜底、MT 分支与回退。

不启 worker：直接调 interpret._build_tts_provider / _build_llm_provider 纯装配函数。
env 断言全部走 monkeypatch（终了自动还原，唔污染其他测试）。
"""

from __future__ import annotations

import os

from agent_runtime import interpret

# MiniMax language_boost 外部枚举（TTS 供应商 API 真字面量，粤=普+粤标记）。
# 常量名唔带旧拼写——术语门禁只豁免含字面量值的行。
_MINIMAX_BOOST = "Chinese,Yue"


def test_build_tts_provider_minimax_branch(monkeypatch):
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    cfg = {
        "provider": "minimax",
        "speaker_cantonese": "Cantonese_GentleLady",
        "api_key": "k-test",
        "sample_rate": 24000,
    }
    provider = interpret._build_tts_provider(cfg, "cantonese")
    assert isinstance(provider, MiniMaxTTS)
    # 音色锁口音：粤目标语解析到设置页配的粤语音色。
    assert provider._resolve_voice() == "Cantonese_GentleLady"
    assert provider._language_state.lang == "cantonese"
    # B 线默认 2.8-turbo 档(2026-09-16 起:语气词标记仅 2.8 系支持;原 2.6-turbo)
    # + language_boost 锁目标语——经构造参数下发,唔写进程 env(setdefault 跨会话
    # 驻留已废,评审 follow-up,与采样档 P2-3 同治理)。
    assert provider._model() == "speech-2.8-turbo"
    assert provider._language_boost() == _MINIMAX_BOOST
    assert provider._api_key() == "k-test"
    assert "MINIMAX_MODEL" not in os.environ
    assert "MINIMAX_LANGUAGE_BOOST" not in os.environ


def test_build_tts_provider_minimax_boost_and_voice_per_target(monkeypatch):
    """zh/en 目标语各锁各的 boost 与音色键；设置页没配的键落验证过的默认。

    同 worker 连续装配 zh→en 两通:boost 逐会话解析,唔得停在首通的 Chinese
    （旧 setdefault 跨会话泄漏的回归断言）。"""
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    cfg = {"provider": "minimax_streaming", "speaker_en": "my-en-voice"}

    zh = interpret._build_tts_provider(cfg, "zh")
    assert zh._language_boost() == "Chinese"
    assert zh._resolve_voice() == "Chinese (Mandarin)_News_Anchor"

    en = interpret._build_tts_provider(cfg, "en")
    assert en._language_boost() == "English"
    assert en._resolve_voice() == "my-en-voice"
    assert "MINIMAX_LANGUAGE_BOOST" not in os.environ


def test_build_tts_provider_qwen3_fallback(monkeypatch):
    """provider=qwen3_tts / 未指定 → 本地 Qwen3TTSTTS，唔触发 MiniMax env。"""
    from agent_runtime.providers.livekit_plugins import Qwen3TTSTTS

    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    provider = interpret._build_tts_provider({"provider": "qwen3_tts", "speaker": "wan2"}, "zh")
    assert isinstance(provider, Qwen3TTSTTS)
    assert provider._resolve_voice() == "wan2"
    # 未指定 provider 也走本地兜底；全局 speaker 缺省时按目标语取分语言键。
    assert isinstance(interpret._build_tts_provider({}, "en"), Qwen3TTSTTS)
    assert interpret._build_tts_provider({"speaker_zh": "zh-voice"}, "zh")._resolve_voice() == "zh-voice"
    assert "MINIMAX_MODEL" not in os.environ
    assert "MINIMAX_LANGUAGE_BOOST" not in os.environ


def test_parse_session_voices_shapes():
    """会话级音色 JSON 解析（纯函数）：坏 JSON/形状不对/空值/未知键 → 丢弃不炸。"""
    assert interpret._parse_session_voices("") == {}
    assert interpret._parse_session_voices(None) == {}
    assert interpret._parse_session_voices("not-json") == {}
    assert interpret._parse_session_voices("[]") == {}
    # 空音色值丢弃；键经 _norm_lang 归一（未知语言键丢弃,唔进 map）。
    assert interpret._parse_session_voices('{"zh":"v-zh","en":""}') == {"zh": "v-zh"}
    assert interpret._parse_session_voices('{"EN":" v-en "}') == {"en": "v-en"}


def test_resolve_minimax_model_env_priority_and_default(monkeypatch):
    """合成档单点解析(纯读 env):显式 env 优先(2.6 回退档→voice_tags 门熄火);
    缺省 B 线 2.8-turbo;解析结果经 model_override 落进实例(_model() 生效)。"""
    monkeypatch.delenv("MINIMAX_MODEL", raising=False)
    assert interpret._resolve_minimax_model() == "speech-2.8-turbo"
    assert interpret._voice_tags_supported(interpret._resolve_minimax_model()) is True

    monkeypatch.setenv("MINIMAX_MODEL", "speech-2.6-turbo")
    assert interpret._resolve_minimax_model() == "speech-2.6-turbo"
    assert interpret._voice_tags_supported(interpret._resolve_minimax_model()) is False
    provider = interpret._build_tts_provider({"provider": "minimax", "api_key": "k"}, "zh")
    assert provider._model() == "speech-2.6-turbo"
    assert "MINIMAX_MODEL" in os.environ  # 只读,唔删用户显式部署档


def test_minimax_tts_language_boost_param_precedence(monkeypatch):
    """构造 language_boost 参数优先于 env;未传(None)透传 env;env 也没有=不下发。"""
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    monkeypatch.setenv("MINIMAX_LANGUAGE_BOOST", "Chinese")
    assert MiniMaxTTS(voice="v", language_boost="English")._language_boost() == "English"
    passthrough = MiniMaxTTS(voice="v")
    assert passthrough._language_boost() == "Chinese"
    monkeypatch.delenv("MINIMAX_LANGUAGE_BOOST", raising=False)
    assert passthrough._language_boost() == ""


def test_build_tts_provider_session_voice_overrides_settings_and_defaults(monkeypatch):
    """会话级音色最优先：> 设置三键 > 硬编码默认；未传/空 map 行为与现状逐字节一致。"""
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    cfg = {"provider": "minimax", "speaker_en": "settings-en", "api_key": "k"}

    p = interpret._build_tts_provider(cfg, "en", {"en": "session-en"})
    assert isinstance(p, MiniMaxTTS)
    assert p._resolve_voice() == "session-en"
    zh = interpret._build_tts_provider(cfg, "zh", {"zh": "session-zh"})
    assert zh._resolve_voice() == "session-zh"

    # 未选（None/空 dict）→ 现状：设置键命中，缺省键落硬编码默认。
    assert interpret._build_tts_provider(cfg, "en", None)._resolve_voice() == "settings-en"
    assert interpret._build_tts_provider(cfg, "en", {})._resolve_voice() == "settings-en"
    assert interpret._build_tts_provider(cfg, "zh", {})._resolve_voice() == "Chinese (Mandarin)_News_Anchor"
    # 原始 JSON 串也收（防御 entrypoint 忘解析直接透传）。
    assert interpret._build_tts_provider(cfg, "en", '{"en":"raw-json-voice"}')._resolve_voice() == "raw-json-voice"


def test_build_tts_provider_session_voice_filters_local_qwen3(monkeypatch):
    """会话级误选本地 Qwen3 音色（预设/克隆前缀）→ 过滤回落设置三键/默认，防 2054。"""
    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    cfg = {"provider": "minimax", "speaker_en": "settings-en", "api_key": "k"}
    p = interpret._build_tts_provider(cfg, "en", {"en": "vivian"})
    assert p._resolve_voice() == "settings-en"
    p2 = interpret._build_tts_provider({"provider": "minimax"}, "cantonese", {"cantonese": "agent-clone-x"})
    assert p2._resolve_voice() == "Cantonese_crisp_news_anchor_vv2"


def test_build_llm_provider_mt_branch(monkeypatch, tmp_path):
    """MT_LLM_BASE_URL 有值 + model 为真实本地绝对路径 → StatelessMTLLM 包 MlxLlmLLM(:1236)
    + 官方推荐采样。(repo-id 等非本地路径会被 mlx_lm server 挂死,已由 _mt_model_valid
    门禁拦下走回退——见 test_mt_model_guard.py。)"""
    from agent_runtime.providers.livekit_plugins import MlxLlmLLM, StatelessMTLLM

    for key in (
        "MT_LLM_BASE_URL",
        "MT_LLM_MODEL",
        "LLM_TOP_P",
        "LLM_TOP_K",
        "LLM_REPETITION_PENALTY",
        "LLM_TEMPERATURE",
        "LLM_MAX_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)
    mt_model = tmp_path / "Hy-MT2-8bit"
    mt_model.mkdir()
    monkeypatch.setenv("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
    monkeypatch.setenv("MT_LLM_MODEL", str(mt_model))

    provider = interpret._build_llm_provider({}, "cantonese")
    assert isinstance(provider, StatelessMTLLM)
    assert provider._target_lang == "cantonese"
    assert provider.provider == "mlx"
    inner = provider._inner
    assert isinstance(inner, MlxLlmLLM)
    # base_url 落在官方内芯的 AsyncClient 上（_opts 不存它;httpx 会补尾斜杠）。
    assert str(inner._client.base_url).rstrip("/") == "http://127.0.0.1:1236/v1"
    assert inner._opts.model == str(mt_model)
    # 官方推荐采样经构造参数落进 extra_body/温度（评审 P2-3:唔再写进程 env——
    # setdefault 会跨会话驻留,MT 失效落回主 LLM 时采样档跟着泄漏）。
    body = inner._opts.extra_body or {}
    assert body["top_p"] == 0.6
    assert body["top_k"] == 20 and isinstance(body["top_k"], int)
    assert body["repetition_penalty"] == 1.05
    assert inner._opts.temperature == 0.7
    # 四键不得出现在进程 env（泄漏防线,monkeypatch 终了自动还原）。
    for key in ("LLM_TEMPERATURE", "LLM_TOP_P", "LLM_TOP_K", "LLM_REPETITION_PENALTY"):
        assert key not in os.environ


def test_build_llm_provider_mt_env_override(monkeypatch, tmp_path):
    """用户显式 env 优先于 MT 推荐默认（保持旧行为）,但同样唔写回 env。

    LLM_TEMPERATURE=0.1 → 内芯温度 0.1;未设的其余三键仍落 MT 推荐档。"""
    from agent_runtime.providers.livekit_plugins import MlxLlmLLM, StatelessMTLLM

    for key in (
        "MT_LLM_BASE_URL",
        "MT_LLM_MODEL",
        "LLM_TOP_P",
        "LLM_TOP_K",
        "LLM_REPETITION_PENALTY",
        "LLM_TEMPERATURE",
    ):
        monkeypatch.delenv(key, raising=False)
    mt_model = tmp_path / "Hy-MT2-8bit"
    mt_model.mkdir()
    monkeypatch.setenv("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
    monkeypatch.setenv("MT_LLM_MODEL", str(mt_model))
    monkeypatch.setenv("LLM_TEMPERATURE", "0.1")

    provider = interpret._build_llm_provider({}, "cantonese")
    assert isinstance(provider, StatelessMTLLM)
    inner = provider._inner
    assert isinstance(inner, MlxLlmLLM)
    assert inner._opts.temperature == 0.1
    body = inner._opts.extra_body or {}
    assert body["top_p"] == 0.6
    assert body["top_k"] == 20
    assert body["repetition_penalty"] == 1.05
    # 显式 env 同样唔落进程 env 残留。
    assert "LLM_TOP_P" not in os.environ


def test_build_llm_provider_mt_unset_or_empty_falls_back(monkeypatch):
    """回退开关二态:MT_LLM_BASE_URL unset / 空串(bok.py 缺模型唔下发)→ 纯 MlxLlmLLM。"""
    from agent_runtime.providers.livekit_plugins import MlxLlmLLM, StatelessMTLLM

    monkeypatch.delenv("MT_LLM_MODEL", raising=False)
    monkeypatch.delenv("MT_LLM_BASE_URL", raising=False)
    assert isinstance(interpret._build_llm_provider({}, "cantonese"), MlxLlmLLM)

    # 空串与 unset 同回退(interpret 侧按 .strip() 判),唔会指去 :1236 死端口。
    monkeypatch.setenv("MT_LLM_BASE_URL", "")
    provider = interpret._build_llm_provider({}, "cantonese")
    assert isinstance(provider, MlxLlmLLM)
    assert not isinstance(provider, StatelessMTLLM)
    assert str(provider._client.base_url).rstrip("/") == "http://127.0.0.1:1235/v1"


def test_build_tts_provider_minimax_filters_local_qwen3_voices(monkeypatch):
    """设置页误配本地 Qwen3 音色(预设 9 个/克隆 agent-*)→ 过滤落验证过默认,防 2054。"""
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    cfg = {
        "provider": "minimax",
        "speaker_zh": "vivian",  # 本地预设音色 → 过滤
        "speaker_cantonese": "agent-clone-x",  # 克隆音色前缀 → 过滤
        "speaker_en": "male_english_speaker",  # 云端音色原样保留
        "api_key": "k-test",
    }
    zh = interpret._build_tts_provider(cfg, "zh")
    assert zh._resolve_voice() == "Chinese (Mandarin)_News_Anchor"
    cantonese = interpret._build_tts_provider(cfg, "cantonese")
    assert cantonese._resolve_voice() == "Cantonese_crisp_news_anchor_vv2"
    en = interpret._build_tts_provider(cfg, "en")
    assert en._resolve_voice() == "male_english_speaker"


def test_build_llm_provider_fallback(monkeypatch):
    """回退开关 = unset MT_LLM_BASE_URL：老 DeepSeek/主 LLM 路径原样保留。"""
    from agent_runtime.providers.livekit_plugins import DeepSeekLLM, MlxLlmLLM

    monkeypatch.delenv("MT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("MT_LLM_MODEL", raising=False)

    provider = interpret._build_llm_provider({}, "en")
    assert isinstance(provider, MlxLlmLLM)
    assert str(provider._client.base_url).rstrip("/") == "http://127.0.0.1:1235/v1"

    ds = interpret._build_llm_provider({"provider": "deepseek", "api_key": "sk-test"}, "en")
    assert isinstance(ds, DeepSeekLLM)
    assert ds._opts.model == "deepseek-chat"

def test_direction_audio_enabled_rev_text_only_by_default(monkeypatch):
    """2026-09-12 用户拍板:同传出声单向——fwd(听 me,译文给对方)恒出声;
    rev(听 other,对方→我)默认纯字幕零 TTS;BOK_INTERP_REV_AUDIO=1 恢复双向。"""
    monkeypatch.delenv("BOK_INTERP_REV_AUDIO", raising=False)
    assert interpret._direction_audio_enabled("me") is True
    assert interpret._direction_audio_enabled("other") is False
    monkeypatch.setenv("BOK_INTERP_REV_AUDIO", "1")
    assert interpret._direction_audio_enabled("other") is True
