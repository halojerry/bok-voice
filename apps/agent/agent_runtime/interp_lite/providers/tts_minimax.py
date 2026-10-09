"""interp_lite MiniMax TTS：装配层（复用在役插件，docs-first 核对）。

官方文档基线（本地副本 ``/Users/halo/Documents/bok/minimax-api_副本.md``，2026-10-09 核对）：
- 端点：``wss://api.minimax.cn/ws/v1/t2a_v2_bidi``（Bearer header；region intl=.chat）。
- 在役 ``providers/livekit_plugins.MiniMaxTTS`` + ``_MiniMaxBidiStream`` **已按官方
  bidi 契约实现**（持久连接一通一 task、服务端攒句逐字喂、task_flush 头段催产=
  BOK_TTS_FIRST_CHUNK_CHARS、task_cancel 打断可续、2205 原样重发、30s ping、hex
  解码、2201 重连自愈）——薄线复用而非重写（重写=10-09 审计否决的重复造轮子；
  计划档 §6 偏差记录）。
- 官方要点本层对齐：``emotion`` 不下发（官方「模型自动匹配，一般无需手动指定」，
  与旧线翻默认决议同源）；``language_boost`` 按目标语言构造参数显式下发（不写
  进程 env，旧评审 P2-3 同治理）；语速单源 ``minimax_speed_for``（zh/cantonese
  1.2、其余 1.0，插件内 resolved_speed 消费）。
- 音色优先级（镜像旧线 _build_tts_provider）：会话级 voices > 人设层 > 设置三键 >
  硬编码默认；本地 Qwen3 音色 id 过滤防 2054（复用 ``interpret._cloud_voice``）。

不带项（计划档 §8）：PrewarmFallbackTTS 本地备档——cloud-only 姿势不拉 :8788；
触发补带条件=实弹整通静音且归因 MiniMax 全挂。prewarm 保留（纯增益）。
"""

from __future__ import annotations

import asyncio

from ...interpret import _cloud_voice, _norm_lang, _parse_session_voices, _resolve_minimax_model
from ...providers import livekit_plugins as _lp

# 目标语 → MiniMax language_boost 官方枚举（外部字面量，术语门禁白名单单点）。
BOOST_MAP = {
    "zh": "Chinese",
    "cantonese": "Chinese,Yue",
    "en": "English",
    "de": "German",
    "fr": "French",
    "ja": "Japanese",
    "pt": "Portuguese",
}

# 各语种默认音色（与旧线同值——母语口音不串；en 默认=预验过的在库 id）。
_VOICE_DEFAULTS = {
    "zh": "Chinese (Mandarin)_News_Anchor",
    "cantonese": "Cantonese_crisp_news_anchor_vv2",
    "en": "English_magnetic_voiced_man",
}


def voice_map_for(tts_cfg: dict, target_lang: str, session_voices, persona_voices) -> dict:
    """三层音色叠加（镜像旧线语义）：设置三键 < 人设 < 会话级；本地音色 id 过滤。"""
    keymap = {"zh": "speaker_zh", "cantonese": "speaker_cantonese", "en": "speaker_en"}
    voice_map: dict = {}
    for lang, key in keymap.items():
        vid = _cloud_voice(str(tts_cfg.get(key) or ""))
        if vid:
            voice_map[lang] = vid
    if isinstance(persona_voices, str):
        from bok_voice_core.voice_map import parse_voice_map

        persona_voices = parse_voice_map(persona_voices)
    for lang_raw, vid_raw in (persona_voices or {}).items():
        lang = _norm_lang(str(lang_raw), default="")
        if lang not in keymap:
            continue
        vid = _cloud_voice(str(vid_raw or ""))
        if vid:
            voice_map[lang] = vid
    if isinstance(session_voices, str):
        session_voices = _parse_session_voices(session_voices)
    for lang_raw, vid_raw in (session_voices or {}).items():
        lang = _norm_lang(str(lang_raw), default="")
        if not lang:
            continue
        cloud = _cloud_voice(str(vid_raw or "").strip())
        if cloud:
            voice_map[lang] = cloud
    for lang, vid in _VOICE_DEFAULTS.items():
        voice_map.setdefault(lang, vid)
    return voice_map


def build(tts_cfg: dict, target_lang: str, session_voices=None, persona_voices=None):
    """组装 MiniMaxTTS（2.8-turbo 档=语气标记支持；prewarm 后台跑，失败零影响）。"""
    tts_ls = _lp.LanguageState()
    tts_ls.lang = target_lang
    tts = _lp.MiniMaxTTS(
        voice=voice_map_for(tts_cfg, target_lang, session_voices, persona_voices),
        language_state=tts_ls,
        sample_rate=int(tts_cfg.get("sample_rate") or 24000),
        api_key=str(tts_cfg.get("api_key") or ""),
        model_override=_resolve_minimax_model(),  # B 线缺省 speech-2.8-turbo
        language_boost=BOOST_MAP.get(target_lang) or None,
    )
    try:
        pw = tts.prewarm()
        if asyncio.iscoroutine(pw):
            try:
                asyncio.get_running_loop().create_task(pw)
            except RuntimeError:
                pw.close()
    except Exception:  # noqa: BLE001 - 预热失败零影响
        pass
    return tts
