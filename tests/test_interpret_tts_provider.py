"""B 线 TTS/LLM provider 组装单测：MiniMax 分支、Qwen3 本地兜底、MT 分支与回退。

不启 worker：直接调 interpret._build_tts_provider / _build_llm_provider 纯装配函数。
env 断言全部走 monkeypatch（终了自动还原，唔污染其他测试）。

MT 分支测试统一注入 `mt_alive=lambda *_: True`（装配期探活 RC-2 的注入口）——
否则本地 :1236 不在场时探活死会落回退链（这正是被测行为,但会让既有分支断言红）。
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_runtime import interpret

ROOT = Path(__file__).resolve().parents[1]

# MiniMax language_boost 外部枚举（TTS 供应商 API 真字面量，粤=普+粤标记）。
# 常量名唔带旧拼写——术语门禁只豁免含字面量值的行。
_MINIMAX_BOOST = "Chinese,Yue"


def _primary(provider):
    """取 TTS 链主档(纯测试辅助)。

    2026-09-27 起 MiniMax 档返回官方 FallbackAdapter(primary=MiniMax,
    backup=本地 Qwen3);断言音色/模型须看主实例。裸实例(TestTTS/本地档/
    测试环境 BOK_LOCAL_TTS=0)原样返回。"""
    return getattr(provider, "_tts_instances", [provider])[0]


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
    assert isinstance(_primary(provider), MiniMaxTTS)
    # 音色锁口音：粤目标语解析到设置页配的粤语音色。
    assert _primary(provider)._resolve_voice() == "Cantonese_GentleLady"
    assert _primary(provider)._language_state.lang == "cantonese"
    # B 线默认 2.8-turbo 档(2026-09-16 起:语气词标记仅 2.8 系支持;原 2.6-turbo)
    # + language_boost 锁目标语——经构造参数下发,唔写进程 env(setdefault 跨会话
    # 驻留已废,评审 follow-up,与采样档 P2-3 同治理)。
    assert _primary(provider)._model() == "speech-2.8-turbo"
    assert _primary(provider)._language_boost() == _MINIMAX_BOOST
    assert _primary(provider)._api_key() == "k-test"
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
    assert _primary(zh)._language_boost() == "Chinese"
    assert _primary(zh)._resolve_voice() == "Chinese (Mandarin)_News_Anchor"

    en = interpret._build_tts_provider(cfg, "en")
    assert _primary(en)._language_boost() == "English"
    assert _primary(en)._resolve_voice() == "my-en-voice"
    assert "MINIMAX_LANGUAGE_BOOST" not in os.environ


def test_build_tts_provider_qwen3_fallback(monkeypatch):
    """provider=qwen3_tts / 未指定 → 本地 Qwen3TTSTTS，唔触发 MiniMax env。"""
    from agent_runtime.providers.livekit_plugins import Qwen3TTSTTS

    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    provider = interpret._build_tts_provider({"provider": "qwen3_tts", "speaker": "wan2"}, "zh")
    assert isinstance(provider, Qwen3TTSTTS)
    # 2026-09-24 新契约：未知本地音色 → 语言档回落（治 MiniMax 音色 id 误入本地线
    # 崩 5/7 轮；契约全集见 tests/test_qwen3_voice_fallback.py）。
    assert provider._resolve_voice() == "vivian"
    # 预设音色原样透传。
    assert (
        interpret._build_tts_provider({"provider": "qwen3_tts", "speaker": "serena"}, "zh")._resolve_voice()
        == "serena"
    )
    # 未指定 provider 也走本地兜底；全局 speaker 缺省时按目标语取分语言键。
    assert isinstance(interpret._build_tts_provider({}, "en"), Qwen3TTSTTS)
    assert interpret._build_tts_provider({"speaker_zh": "sohee"}, "zh")._resolve_voice() == "sohee"
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
    assert _primary(provider)._model() == "speech-2.6-turbo"
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
    assert isinstance(_primary(p), MiniMaxTTS)
    assert _primary(p)._resolve_voice() == "session-en"
    zh = interpret._build_tts_provider(cfg, "zh", {"zh": "session-zh"})
    assert _primary(zh)._resolve_voice() == "session-zh"

    # 未选（None/空 dict）→ 现状：设置键命中，缺省键落硬编码默认。
    assert _primary(interpret._build_tts_provider(cfg, "en", None))._resolve_voice() == "settings-en"
    assert _primary(interpret._build_tts_provider(cfg, "en", {}))._resolve_voice() == "settings-en"
    assert _primary(interpret._build_tts_provider(cfg, "zh", {}))._resolve_voice() == "Chinese (Mandarin)_News_Anchor"
    # 原始 JSON 串也收（防御 entrypoint 忘解析直接透传）。
    assert _primary(interpret._build_tts_provider(cfg, "en", '{"en":"raw-json-voice"}'))._resolve_voice() == "raw-json-voice"


def test_build_tts_provider_session_voice_filters_local_qwen3(monkeypatch):
    """会话级误选本地 Qwen3 音色（预设/克隆前缀）→ 过滤回落设置三键/默认，防 2054。"""
    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    cfg = {"provider": "minimax", "speaker_en": "settings-en", "api_key": "k"}
    p = interpret._build_tts_provider(cfg, "en", {"en": "vivian"})
    assert _primary(p)._resolve_voice() == "settings-en"
    p2 = interpret._build_tts_provider({"provider": "minimax"}, "cantonese", {"cantonese": "agent-clone-x"})
    assert _primary(p2)._resolve_voice() == "Cantonese_crisp_news_anchor_vv2"


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

    # 装配期探活(RC-2)注入口:假活,免网络(真探活行为见下方专门用例)。
    provider = interpret._build_llm_provider({}, "cantonese", mt_alive=lambda *_: True)
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
    # 刀1(D):整句翻译 max_tokens=512 经构造参数下发(entrypoint setdefault 已删)。
    assert body["max_tokens"] == 512
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

    provider = interpret._build_llm_provider({}, "cantonese", mt_alive=lambda *_: True)
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
    assert _primary(zh)._resolve_voice() == "Chinese (Mandarin)_News_Anchor"
    cantonese = interpret._build_tts_provider(cfg, "cantonese")
    assert _primary(cantonese)._resolve_voice() == "Cantonese_crisp_news_anchor_vv2"
    en = interpret._build_tts_provider(cfg, "en")
    assert _primary(en)._resolve_voice() == "male_english_speaker"


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


_MT_CLOUD_ROUTING = '{"lanes":{"mt":{"provider":"openai","base_url":"https://api.deepseek.com/v1","model":"deepseek-flash","api_key":"sk-test"}}}'


def test_mt_cloud_lane_instructed_by_default(monkeypatch):
    """Wave 1(2026-10-08「MT 全量切 DeepSeek 试」):mt 车道 openai 档**指令化**——
    不再包 StatelessMTLLM(它只取最后一条 user 套模板,system 指令/滚动对全丢
    =ASR 纠错能力被锁死,subagent 调研实证)。直接 MlxLlmLLM 吃完整 ctx
    (_build_mt_context:instructions 含纠错行+滚动对+当前句)=回退档既有形状。"""
    from agent_runtime.providers.livekit_plugins import MlxLlmLLM, StatelessMTLLM

    monkeypatch.setenv("BOK_MODEL_ROUTING", "1")
    monkeypatch.delenv("BOK_INTERP_MT_CLOUD_INSTRUCT", raising=False)
    p = interpret._build_llm_provider({}, "en", routing_raw=_MT_CLOUD_ROUTING)
    assert isinstance(p, MlxLlmLLM)
    assert not isinstance(p, StatelessMTLLM)
    assert str(p._client.base_url).rstrip("/") == "https://api.deepseek.com/v1"


def test_mt_cloud_lane_killswitch_restores_template(monkeypatch):
    """kill-switch BOK_INTERP_MT_CLOUD_INSTRUCT=0:回旧 StatelessMTLLM 模板包裹
    (「全量切 DeepSeek 试」的逃生口,逐字节旧形状)。"""
    from agent_runtime.providers.livekit_plugins import StatelessMTLLM

    monkeypatch.setenv("BOK_MODEL_ROUTING", "1")
    monkeypatch.setenv("BOK_INTERP_MT_CLOUD_INSTRUCT", "0")
    p = interpret._build_llm_provider({}, "en", routing_raw=_MT_CLOUD_ROUTING)
    assert isinstance(p, StatelessMTLLM)


def test_translation_instructions_asr_correction_rule():
    """Wave 1 纠错行在场:ASR 同音误听结合上下文纠+绝不虚构/不硬译碎片
    (c3ed3ef3 云端脑补反例的负例锚)。"""
    s = interpret._translation_instructions("zh", "en")
    assert "homophone mishearings" in s
    assert "Never answer" in s and "never add" in s


def test_direction_audio_enabled_rev_text_only_by_default(monkeypatch):
    """2026-10-08 用户翻案(「对方说英文 我要听到英文转普通话的翻译!」):同传
    **双向出声**——fwd(听 me,译文给对方)恒出声;rev(听 other,对方→我)默认
    也合成 TTS(译员耳语);BOK_INTERP_REV_AUDIO=0 回退旧单向化档(rev 纯字幕)。"""
    monkeypatch.delenv("BOK_INTERP_REV_AUDIO", raising=False)
    assert interpret._direction_audio_enabled("me") is True
    assert interpret._direction_audio_enabled("other") is True
    monkeypatch.setenv("BOK_INTERP_REV_AUDIO", "0")
    assert interpret._direction_audio_enabled("other") is False
    monkeypatch.setenv("BOK_INTERP_REV_AUDIO", "1")
    assert interpret._direction_audio_enabled("other") is True


# ---------------------------------------------------------------------------
# 刀1(C) MT 装配期探活 + 刀1(D) max_tokens 构造参
# ---------------------------------------------------------------------------


def _mt_env(monkeypatch, tmp_path):
    """本地 MT 分支齐全配置(model=真实在盘绝对路径),探活注入口留给调用方。"""
    mt_model = tmp_path / "Hy-MT2-8bit"
    mt_model.mkdir()
    monkeypatch.setenv("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
    monkeypatch.setenv("MT_LLM_MODEL", str(mt_model))
    monkeypatch.delenv("BOK_INTERP_MT_PROBE", raising=False)
    monkeypatch.delenv("MLX_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return mt_model


def test_build_llm_provider_mt_probe_dead_falls_back(monkeypatch, tmp_path, capsys):
    """装配期探活(RC-2)::1236 死 → 不返回 MT provider,落回退链 + 日志一行。

    死端点若进 MT 分支=整通每句走异常兜底(装配期不探活的旧病);现探活失败即
    回退到既有 DeepSeek/主 LLM 链。
    """
    from agent_runtime.providers.livekit_plugins import MlxLlmLLM, StatelessMTLLM

    _mt_env(monkeypatch, tmp_path)
    provider = interpret._build_llm_provider({}, "cantonese", mt_alive=lambda *_: False)
    assert isinstance(provider, MlxLlmLLM)
    assert not isinstance(provider, StatelessMTLLM)
    assert str(provider._client.base_url).rstrip("/") == "http://127.0.0.1:1235/v1"
    assert (provider._opts.extra_body or {})["max_tokens"] == 512  # 刀1(D) 回退链同口径
    out = capsys.readouterr().out
    assert "[interp] mt endpoint dead (http://127.0.0.1:1236/v1) — fallback chain" in out
    assert "mt model invalid" not in out  # 探活死已单独报,不重复误导
    assert "llm=hy-mt2" not in out


def test_build_llm_provider_mt_probe_disabled_trusts_config(monkeypatch, tmp_path):
    """BOK_INTERP_MT_PROBE=0 = 旧行为:不探活,信任配置直接给 MT provider。

    注入口给「一调即炸」证明探活确实被跳过(kill-switch 逐字节回旧)。"""
    from agent_runtime.providers.livekit_plugins import StatelessMTLLM

    _mt_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BOK_INTERP_MT_PROBE", "0")

    def _boom(*_args):  # pragma: no cover - 被调用即失败
        raise AssertionError("probe must not run when BOK_INTERP_MT_PROBE=0")

    provider = interpret._build_llm_provider({}, "cantonese", mt_alive=_boom)
    assert isinstance(provider, StatelessMTLLM)


def test_build_llm_provider_mt_probe_dead_only_for_local_branch(monkeypatch, tmp_path, capsys):
    """探活只挂本地 MT 分支:模型非法(未进分支)不探——注入口一调即炸。"""
    from agent_runtime.providers.livekit_plugins import MlxLlmLLM

    monkeypatch.setenv("MT_LLM_BASE_URL", "http://127.0.0.1:1236/v1")
    monkeypatch.setenv("MT_LLM_MODEL", "repo-id-not-a-path")  # 非本地路径=挂死防线拦下
    monkeypatch.delenv("BOK_INTERP_MT_PROBE", raising=False)

    def _boom(*_args):  # pragma: no cover - 被调用即失败
        raise AssertionError("probe must not run when model gate fails first")

    provider = interpret._build_llm_provider({}, "cantonese", mt_alive=_boom)
    assert isinstance(provider, MlxLlmLLM)
    assert "mt model invalid" in capsys.readouterr().out


def test_mt_endpoint_alive_any_http_response_is_alive(monkeypatch):
    """镜像 CP probe_endpoint:401/404 等 HTTPError 也是「端点在场」(不 raise)。

    http.client 形状下 4xx 不抛——状态码经 getresponse 回来即「有响应」；
    stub 必须打在 http.client.HTTPConnection 上（旧 urlopen stub 是死 seam，
    本机 :1236 有真 MLX 服务在听时测试会发真请求假绿——CI 无监听才红，
    2026-10-04 PR #175 CI 实证）。"""
    import http.client

    seen_status: list[int] = []

    class _Conn:
        def __init__(self, host, port, timeout=None):
            pass

        def request(self, method, path):
            pass

        def getresponse(self):
            class _R:
                status = 401

                def read(self):
                    seen_status.append(401)
                    return b"unauthorized"

            return _R()

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", _Conn)
    assert interpret._mt_endpoint_alive("http://127.0.0.1:1236/v1") is True
    assert seen_status == [401]  # 响应体确实被消费(4xx 也算在场)


def test_mt_endpoint_alive_2xx_via_stub(monkeypatch):
    """2xx(有响应体)=活;空 base=死(不发起请求)。"""
    import http.client

    calls: list[str] = []

    class _Conn:
        def __init__(self, host, port, timeout=None):
            self._host = host

        def request(self, method, path):
            calls.append(path)

        def getresponse(self):
            class _R:
                def read(self):
                    return b"ok"

            return _R()

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", _Conn)
    assert interpret._mt_endpoint_alive("http://127.0.0.1:1236/v1/") is True
    assert calls == ["/v1/models"]
    assert interpret._mt_endpoint_alive("") is False
    assert calls == ["/v1/models"]  # 空 base 短路


def test_mt_endpoint_alive_connection_error_is_dead(monkeypatch):
    """连接错误/超时=死(探活失败是数据不是异常,绝不外抛)。"""
    import http.client

    class _Refused:
        def __init__(self, *a, **kw):
            pass

        def request(self, method, path):
            raise ConnectionRefusedError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", _Refused)
    assert interpret._mt_endpoint_alive("http://127.0.0.1:1236/v1") is False


def test_mlx_llm_max_tokens_param_and_env_fallback(monkeypatch):
    """刀1(D):构造参优先,None=env 读(缺省 160)——既有调用零漂移。"""
    from agent_runtime.providers.livekit_plugins import MlxLlmLLM

    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    explicit = MlxLlmLLM(base_url="http://127.0.0.1:1235/v1", model="m", max_tokens=512)
    assert (explicit._opts.extra_body or {})["max_tokens"] == 512
    assert (MlxLlmLLM(base_url="http://127.0.0.1:1235/v1", model="m")._opts.extra_body or {})[
        "max_tokens"
    ] == 160
    monkeypatch.setenv("LLM_MAX_TOKENS", "256")
    assert (MlxLlmLLM(base_url="http://127.0.0.1:1235/v1", model="m")._opts.extra_body or {})[
        "max_tokens"
    ] == 256
    # 显式参压过 env(构造参数是唯一真源)。
    assert (
        MlxLlmLLM(base_url="http://127.0.0.1:1235/v1", model="m", max_tokens=512)._opts.extra_body or {}
    )["max_tokens"] == 512


def test_interpret_source_has_no_llm_max_tokens_setdefault():
    """刀1(D) 卫生 pin:进程 env 写入绝迹(常驻 worker 跨会话驻留的老病)。"""
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(encoding="utf-8")
    # 只扫代码面(注释里保留旧形态说明是有意为之,不算 env 写入)。
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert 'setdefault("LLM_MAX_TOKENS"' not in code
    assert "setdefault('LLM_MAX_TOKENS'" not in code
    # 四个 MlxLlmLLM 构造点(本地 MT/云端旧模板/云端指令化 Wave1/a_reply 兜底)
    # + DeepSeek 回退点都显式 512(代码面,注释剥后)。
    assert code.count("max_tokens=512") == 5


def test_mt_worker_exception_path_says_fallback_source_pinned():
    """刀1(B):MT 异常(连接错误/装配异常)与超时同款出声兜底——不静默丢句。

    :1236 死亡时每句都走泛异常分支;旧版只 print=整通只有日志没有声音(与已修的
    超时静默同构)。worker 是 entrypoint 闭包不可直调 → 源码级 pin。
    """
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(encoding="utf-8")
    worker = src[src.index("async def _mt_say_worker"):]
    # 超时 + 泛异常两分支都要 say 兜底句(目标语中性请示语,绝不回放源文)。
    assert worker.count("session.say(_mt_fail_line(target_lang))") >= 2
    generic = worker[worker.index("except Exception as exc:  # 单句失败不阻后续"):]
    # 原有打点保留 + 兜底 say 再包 try(兜底失败不阻后续句)。
    assert 'print(f"[interp] mt/say failed: {exc!r}", flush=True)' in generic
    assert "session.say(_mt_fail_line(target_lang))" in generic
    assert "mt fail fallback say failed" in generic


# ---- B 线人设音色复用（2026-10-08：音色链插人设层） ----


def test_build_tts_provider_persona_layer(monkeypatch):
    """人设层链位=会话级 voices > 人设 > 设置三键 > 默认；本地 qwen3 音色 ID
    同样过滤；人设 map 只有三语键，四语目标天然缺席回落下层。"""
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS

    for key in ("MINIMAX_MODEL", "MINIMAX_LANGUAGE_BOOST"):
        monkeypatch.delenv(key, raising=False)
    cfg = {"provider": "minimax", "speaker_zh": "settings-zh", "speaker_en": "settings-en"}

    # ① 人设覆盖设置；未涉及的键（en）走设置。
    p = interpret._build_tts_provider(
        cfg, "zh", persona_voices={"zh": "persona-zh", "cantonese": "persona-canto"}
    )
    assert isinstance(_primary(p), MiniMaxTTS)
    p_ls = _primary(p)._language_state
    p_ls.lang = "zh"
    assert _primary(p)._resolve_voice() == "persona-zh"
    en = interpret._build_tts_provider(cfg, "en", persona_voices={"zh": "persona-zh"})
    _primary(en)._language_state.lang = "en"
    assert _primary(en)._resolve_voice() == "settings-en"

    # ② 会话级仍最优先（覆盖人设）。
    both = interpret._build_tts_provider(
        cfg, "zh", {"zh": "session-zh"}, persona_voices={"zh": "persona-zh"}
    )
    _primary(both)._language_state.lang = "zh"
    assert _primary(both)._resolve_voice() == "session-zh"

    # ③ 人设里的本地 Qwen3 音色 ID 过滤（误配防 2054）→ 回落设置。
    local_id = interpret._build_tts_provider(
        cfg, "zh", persona_voices={"zh": "vivian"}
    )
    _primary(local_id)._language_state.lang = "zh"
    assert _primary(local_id)._resolve_voice() == "settings-zh"

    # ④ persona_voices 收 JSON 字符串（parse_voice_map 同源解析）。
    as_json = interpret._build_tts_provider(cfg, "zh", persona_voices='{"zh": "persona-json"}')
    _primary(as_json)._language_state.lang = "zh"
    assert _primary(as_json)._resolve_voice() == "persona-json"

    # ⑤ 不传人设=链路逐字节旧档（设置生效）。
    legacy = interpret._build_tts_provider(cfg, "zh")
    _primary(legacy)._language_state.lang = "zh"
    assert _primary(legacy)._resolve_voice() == "settings-zh"


def test_persona_voice_assembly_wiring_pins():
    """接线 pin：装配点拉 persona（meta.persona_id → cp.get_persona →
    _persona_voice_map=A 线 collapse 同款三语同把声）并传入 _build_tts_provider；
    agent 侧 A 线 _parse_voice_map/_collapse_voice_map 别名=core 单源。"""
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(encoding="utf-8")
    assert '_persona_id = str(meta.get("persona_id") or "").strip()' in src
    assert "await cp.get_persona(_persona_id)" in src
    assert "_persona_voices = _persona_voice_map(_persona)" in src
    assert "persona_voices=_persona_voices" in src

    from agent_runtime.agent import _collapse_voice_map, _parse_voice_map
    from bok_voice_core.voice_map import collapse_voice_map, parse_voice_map

    assert _parse_voice_map is parse_voice_map  # A 线别名=core 单源对象
    assert _collapse_voice_map is collapse_voice_map


def test_persona_voice_map_collapses_to_single_voice():
    """B2(2026-10-08 用户拍板「三语言跟 A 线一样的人设音色」):collapse 整场同声
    ——取人设主语言音色,三语目标同把声;非按语言分把。"""
    # 主语言 zh → zh 键音色三语同用
    m = interpret._persona_voice_map(
        {"language": "zh", "reference_audio": '{"zh": "v-zh", "cantonese": "v-canto", "en": "v-en"}'}
    )
    assert m == {"zh": "v-zh", "cantonese": "v-zh", "en": "v-zh"}
    # 主语言 cantonese → 粤键优先,缺 zh 回落链
    m2 = interpret._persona_voice_map(
        {"language": "cantonese", "reference_audio": '{"cantonese": "v-canto", "en": "v-en"}'}
    )
    assert set(m2.values()) == {"v-canto"}
    # 裸字符串单音色
    assert set(interpret._persona_voice_map({"reference_audio": "single-id"}).values()) == {"single-id"}
    # 空/坏形状 → 空 map(层缺席回落)
    assert interpret._persona_voice_map({}) == {}
    assert interpret._persona_voice_map(None) == {}
    assert interpret._persona_voice_map({"reference_audio": "  "}) == {}


