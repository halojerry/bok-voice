"""Agent ASR→language normalization: Cantonese mis-tag correction + no false positives."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import _normalize_asr_language  # noqa: E402


def test_explicit_language_tags():
    assert _normalize_asr_language("Cantonese", "有冇人知道？") == "yue"
    assert _normalize_asr_language("YUE", "我哋聽日見") == "yue"
    assert _normalize_asr_language("English", "hello there") == "en"
    assert _normalize_asr_language("Chinese", "你好") == "zh"


def test_cantonese_text_corrects_mis_tagged_chinese():
    # Qwen3-ASR 偶发把粤语判成 Chinese：文本地道粤语应纠偏为 yue。
    assert _normalize_asr_language("Chinese", "有冇人知道灣仔活道係點去？") == "yue"
    assert _normalize_asr_language("Chinese", "喂，我係想問下你哋公司有咩服務") == "yue"
    assert _normalize_asr_language("Chinese", "你唔好咁講啦") == "yue"


def test_mandarin_not_misclassified():
    # 普通话文本（含普粤共用字 下/好/系 等）不得被误判成粤语。
    assert _normalize_asr_language("Chinese", "你好，我想问一下你们公司的情况") == "zh"
    assert _normalize_asr_language("Chinese", "好的，那我等一下再联系你") == "zh"
    assert _normalize_asr_language("Chinese", "请问这个系统怎么使用？") == "zh"


def test_more_cantonese_common_words_correct():
    # 短句/常用粤语词（唔係、好嘅、係咪、聽日…）标签误标 zh 也应纠偏为 yue。
    assert _normalize_asr_language("Chinese", "唔係呀，我聽日先得") == "yue"
    assert _normalize_asr_language("Chinese", "係咪有得賠先") == "yue"
    assert _normalize_asr_language("Chinese", "搞掂晒啦") == "yue"
    assert _normalize_asr_language("Chinese", "好嘅，冇問題") == "yue"


# ---- LanguageState 滞后锁定：模糊短轮次不得把当前语言拉走 ----
def _mk_state(initial: str):
    from agent_runtime.providers.livekit_plugins import LanguageState
    s = LanguageState()
    s.lang = initial
    return s


def test_hysteresis_yue_customer_short_ambiguous_utterance_keeps_yue():
    # 粤语客户回「好」「嗯」「OK」：无粤语特征、短、标签 zh——不得拉回普通话。
    s = _mk_state("yue")
    s.update("zh", "好")
    assert s.lang == "yue"
    s.update("zh", "嗯")
    assert s.lang == "yue"
    s.update("Chinese", "係")
    assert s.lang == "yue"
    s.update("zh", "OK 冇問題")
    assert s.lang == "yue"


def test_hysteresis_real_switch_still_works():
    # 客户真切换语言时仍能跟随：够长/够明确的句子。
    s = _mk_state("yue")
    s.update("zh", "好的，那我晚点再打给你，谢谢你啊")
    assert s.lang == "zh"
    s.update("en", "that sounds great, let me check and call you back")
    assert s.lang == "en"
    # 粤语客户在普/粤交界回一句地道粤语 → 锁回粤语。
    s = _mk_state("zh")
    s.update("zh", "唔該，我想問下幾時有得賠")
    assert s.lang == "yue"


def test_hysteresis_english_short_ack_keeps_prior_lang():
    # zh 客户回「ok / yes」短词：en 标签非强 → 保持 zh。
    s = _mk_state("zh")
    s.update("en", "ok")
    assert s.lang == "zh"


def test_language_state_update_now_accepts_text():
    from agent_runtime.providers.livekit_plugins import LanguageState
    s = LanguageState()
    s.lang = "yue"
    # text 带粤语特征但标签模糊 → 提升为 yue。
    s.update("zh", "冇人知")
    assert s.lang == "yue"


# ---- MiniMaxTTS 配置解析(不发起真实请求) ----
import os as _os


def test_minimax_endpoint_region(monkeypatch):
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS, LanguageState
    ls = LanguageState()
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)

    monkeypatch.setenv("MINIMAX_REGION", "cn")
    tts = MiniMaxTTS(voice="x", language_state=ls)
    assert tts._endpoint() == "https://api.minimax.cn/v1/t2a_v2"

    monkeypatch.setenv("MINIMAX_REGION", "intl")
    tts = MiniMaxTTS(voice="x", language_state=ls)
    assert tts._endpoint() == "https://api.minimax.chat/v1/t2a_v2"

    monkeypatch.setenv("MINIMAX_BASE_URL", "https://example.com/tts")
    tts = MiniMaxTTS(voice="x", language_state=ls)
    assert tts._endpoint() == "https://example.com/tts"


def test_minimax_voice_by_language():
    from agent_runtime.providers.livekit_plugins import MiniMaxTTS, LanguageState
    ls = LanguageState(); ls.lang = "yue"
    tts = MiniMaxTTS(voice={"zh": "male-qn-qingse", "yue": "Cantonese_Male_news_anchor_vv2"}, language_state=ls)
    assert tts._resolve_voice() == "Cantonese_Male_news_anchor_vv2"
    ls.lang = "zh"
    assert tts._resolve_voice() == "male-qn-qingse"
    ls.lang = "en"
    # en 未配 → 回落 zh
    assert tts._resolve_voice() == "male-qn-qingse"
