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


def test_garbage_non_han_does_not_drag_language():
    # ASR 对损坏音频可能吐出越南文/乱码 + zh 标签：不得当成"强普通话"把粤语拉走。
    from agent_runtime.providers.livekit_plugins import LanguageState
    s = LanguageState()
    s.lang = "yue"
    s.update("zh", "hổ cũng mong tổng địa thị xoa dịu, không cai sai.")
    assert s.lang == "yue", "非汉字乱码不应把当前语言拉成普通话"
    assert _normalize_asr_language("zh", "hổ cũng mong tổng địa thị") == "zh"


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
    # 整场同声：voice 只配 zh 键（主音色），无论当前语言态为何都解析到同一把声。
    tts = MiniMaxTTS(voice={"zh": "Cantonese_Male_news_anchor_vv2"}, language_state=ls)
    assert tts._resolve_voice() == "Cantonese_Male_news_anchor_vv2"
    ls.lang = "zh"
    assert tts._resolve_voice() == "Cantonese_Male_news_anchor_vv2"
    ls.lang = "en"
    assert tts._resolve_voice() == "Cantonese_Male_news_anchor_vv2"


# ---- 整场同声：persona 旧分语言 map 收敛成单主音色 ----
def test_collapse_voice_map_uses_persona_language():
    from agent_runtime.agent import _collapse_voice_map
    # 小林:persona.language=yue,旧 {zh:男声,yue:女声} → 应取粤语女声作全场主音色。
    m = _collapse_voice_map({"zh": "male-qn-qingse", "yue": "Cantonese_GentleLady"}, "yue")
    assert m == {"zh": "Cantonese_GentleLady"}
    # persona.language=zh → 取普通话男声。
    m = _collapse_voice_map({"zh": "male-qn-qingse", "yue": "Cantonese_GentleLady"}, "zh")
    assert m == {"zh": "male-qn-qingse"}
    # en 未配置 / persona 语言缺失 → zh 兜底。
    m = _collapse_voice_map({"zh": "x", "en": "y"}, "")
    assert m == {"zh": "x"}
    # 空 map → 空。
    assert _collapse_voice_map({}, "yue") == {}


def test_build_default_voice_map_single_speaker_wins():
    from agent_runtime.agent import _build_default_voice_map
    # 全局配了 speaker（单音色整场同声）→ 各语言都用它。
    m = _build_default_voice_map({"speaker": "Cantonese_crisp_news_anchor_vv2", "speaker_zh": "old-zh", "speaker_yue": "old-yue"})
    assert m == {"zh": "Cantonese_crisp_news_anchor_vv2"}
    # 未配 speaker → 回落旧分语言。
    m = _build_default_voice_map({"speaker": "", "speaker_zh": "zh-a", "speaker_yue": "yue-b"})
    assert m == {"zh": "zh-a", "yue": "yue-b"}


# ---- LLM 输出：港式自然英夹（M3） ----
def test_yue_rule_requires_hk_style_code_mixing():
    from agent_runtime.providers.livekit_plugins import ContextState
    ctx = ContextState(account_id="acc-001")
    ctx.set_user_language("yue")
    text = ctx.render_system_message()
    assert "港式粤语" in text
    assert "refund" in text and "check" in text  # 允许自然夹英文服务词
    assert "唔好解釋" in text or "唔好解释" in text  # 防泄漏仍保留
    # 数字要用粤语读法：单号逐字读汉字、0 读零、不用阿拉伯数字串。
    assert "七八九零" in text and "粵語數字" in text
    # 高频普→粤词表：这个→呢個、什么→乜嘢、现在→而家 等硬性替换清单。
    assert "呢個" in text and "乜嘢" in text and "而家" in text and "點解" in text
    # 行业词：快递→速遞、包裹→集運件(香港集运业务语境),并点明不用内地讲法。
    assert "速遞" in text and "集運件" in text and "唔好用「包裹」「快遞」" in text


def test_zh_rule_has_no_hk_style():
    from agent_runtime.providers.livekit_plugins import ContextState
    ctx = ContextState(account_id="acc-001")
    ctx.set_user_language("zh")
    text = ctx.render_system_message()
    assert "港式" not in text


# ---- 粤语客服语言锚定：始终讲粤语，仅连续多轮才跟随客户切语言 ----
def test_sticky_yue_agent_single_mandarin_turn_stays_yue():
    from agent_runtime.agent import _sticky_reply_language
    # 锚 yue；客户单轮普通话 → 仍回 yue(streak 记 1)。
    rl, sticky, streak = _sticky_reply_language("yue", "zh", "yue", 0)
    assert rl == "yue" and sticky == "yue" and streak == 1
    # 下一轮仍是普通话 → 跟随切 zh。
    rl, sticky, streak = _sticky_reply_language("yue", "zh", sticky, streak)
    assert rl == "zh" and sticky == "zh"


def test_sticky_yue_back_to_yue_resets():
    from agent_runtime.agent import _sticky_reply_language
    # 已切到 zh，客户回粤语 → 立刻回锚 yue。
    rl, sticky, streak = _sticky_reply_language("yue", "yue", "zh", 0)
    assert rl == "yue" and sticky == "yue" and streak == 0


def test_sticky_no_anchor_follows_asr():
    from agent_runtime.agent import _sticky_reply_language
    # 无有效锚(空) → 退化为跟随 ASR。
    rl, sticky, streak = _sticky_reply_language("", "zh", "", 0)
    assert rl == "zh"
