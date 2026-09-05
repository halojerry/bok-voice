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
    # B 线默认 turbo 档 + language_boost 锁目标语。
    assert os.environ["MINIMAX_MODEL"] == "speech-2.6-turbo"
    assert os.environ["MINIMAX_LANGUAGE_BOOST"] == _MINIMAX_BOOST
    assert provider._language_boost() == _MINIMAX_BOOST
    assert provider._api_key() == "k-test"


def test_build_tts_provider_minimax_boost_and_voice_per_target(monkeypatch):
    """zh/en 目标语各锁各的 boost 与音色键；设置页没配的键落验证过的默认。"""
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    cfg = {"provider": "minimax_streaming", "speaker_en": "my-en-voice"}

    zh = interpret._build_tts_provider(cfg, "zh")
    assert os.environ["MINIMAX_LANGUAGE_BOOST"] == "Chinese"
    assert zh._resolve_voice() == "Chinese (Mandarin)_News_Anchor"

    monkeypatch.delenv("MINIMAX_LANGUAGE_BOOST", raising=False)
    en = interpret._build_tts_provider(cfg, "en")
    assert os.environ["MINIMAX_LANGUAGE_BOOST"] == "English"
    assert en._resolve_voice() == "my-en-voice"


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


def test_build_llm_provider_mt_branch(monkeypatch):
    """MT_LLM_BASE_URL 有值 → StatelessMTLLM 包 MlxLlmLLM(:1236) + 官方推荐采样。"""
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
    monkeypatch.setenv("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
    monkeypatch.setenv("MT_LLM_MODEL", "mlx-community/Hy-MT2-1.8B-Abliterated-8bit")

    provider = interpret._build_llm_provider({}, "cantonese")
    assert isinstance(provider, StatelessMTLLM)
    assert provider._target_lang == "cantonese"
    assert provider.provider == "mlx"
    inner = provider._inner
    assert isinstance(inner, MlxLlmLLM)
    # base_url 落在官方内芯的 AsyncClient 上（_opts 不存它;httpx 会补尾斜杠）。
    assert str(inner._client.base_url).rstrip("/") == "http://127.0.0.1:1236/v1"
    assert inner._opts.model == "mlx-community/Hy-MT2-1.8B-Abliterated-8bit"
    # 官方推荐采样经 env 落进 extra_body（MlxLlmLLM 构造时读）。
    body = inner._opts.extra_body or {}
    assert body["top_p"] == 0.6
    assert body["top_k"] == 20
    assert body["repetition_penalty"] == 1.05
    assert os.environ["LLM_TEMPERATURE"] == "0.7"


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
