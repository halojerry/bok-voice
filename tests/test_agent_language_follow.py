"""Agent ASR→language normalization: Cantonese mis-tag correction + no false positives."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import _normalize_asr_language  # noqa: E402


def test_explicit_language_tags():
    assert _normalize_asr_language("Cantonese", "有冇人知道？") == "cantonese"
    # SenseVoice 的大写供应商标签在其插件边界(_repl 标签清洗)已归一 cantonese，
    # 不会裸传进本函数；这里只验规范标签路径。
    assert _normalize_asr_language("English", "hello there") == "en"
    assert _normalize_asr_language("Chinese", "你好") == "zh"


def test_cantonese_text_corrects_mis_tagged_chinese():
    # Qwen3-ASR 偶发把粤语判成 Chinese：文本地道粤语应纠偏为 cantonese。
    assert _normalize_asr_language("Chinese", "有冇人知道灣仔活道係點去？") == "cantonese"
    assert _normalize_asr_language("Chinese", "喂，我係想問下你哋公司有咩服務") == "cantonese"
    assert _normalize_asr_language("Chinese", "你唔好咁講啦") == "cantonese"


def test_mandarin_not_misclassified():
    # 普通话文本（含普粤共用字 下/好/系 等）不得被误判成粤语。
    assert _normalize_asr_language("Chinese", "你好，我想问一下你们公司的情况") == "zh"
    assert _normalize_asr_language("Chinese", "好的，那我等一下再联系你") == "zh"


# ---- ASR 语言提示:值=模型 config support_languages 规范名(大小写不敏感回填) ----
def test_asr_language_hint_call_mode():
    # 通话模式:粤语钉(防 auto 误判)、英语钉(支持纯英语会话);zh 保持 auto
    # 容忍夹英文 code-switching(「我哋 check 個 status」)。
    from agent_runtime.providers.livekit_plugins import _asr_language_hint

    assert _asr_language_hint("cantonese", pin=False) == "Cantonese"
    assert _asr_language_hint("en", pin=False) == "English"
    assert _asr_language_hint("zh", pin=False) == ""
    assert _asr_language_hint("", pin=False) == ""


def test_asr_language_hint_pinned_mode():
    # 同传模式:源语言是用户建房时选定的,三种全钉,不吃 auto 漂移。
    from agent_runtime.providers.livekit_plugins import _asr_language_hint

    assert _asr_language_hint("zh", pin=True) == "Chinese"
    assert _asr_language_hint("en", pin=True) == "English"
    assert _asr_language_hint("cantonese", pin=True) == "Cantonese"
    assert _asr_language_hint("", pin=True) == ""
    assert _normalize_asr_language("Chinese", "请问这个系统怎么使用？") == "zh"


def test_more_cantonese_common_words_correct():
    # 短句/常用粤语词（唔係、好嘅、係咪、聽日…）标签误标 zh 也应纠偏为 cantonese。
    assert _normalize_asr_language("Chinese", "唔係呀，我聽日先得") == "cantonese"
    assert _normalize_asr_language("Chinese", "係咪有得賠先") == "cantonese"
    assert _normalize_asr_language("Chinese", "搞掂晒啦") == "cantonese"
    assert _normalize_asr_language("Chinese", "好嘅，冇問題") == "cantonese"


# ---- LanguageState 滞后锁定：模糊短轮次不得把当前语言拉走 ----
def _mk_state(initial: str):
    from agent_runtime.providers.livekit_plugins import LanguageState
    s = LanguageState()
    s.lang = initial
    return s


def test_hysteresis_cantonese_customer_short_ambiguous_utterance_keeps_cantonese():
    # 粤语客户回「好」「嗯」「OK」：无粤语特征、短、标签 zh——不得拉回普通话。
    s = _mk_state("cantonese")
    s.update("zh", "好")
    assert s.lang == "cantonese"
    s.update("zh", "嗯")
    assert s.lang == "cantonese"
    s.update("Chinese", "係")
    assert s.lang == "cantonese"
    s.update("zh", "OK 冇問題")
    assert s.lang == "cantonese"


def test_hysteresis_real_switch_still_works():
    # 客户真切换语言时仍能跟随：够长/够明确的句子。
    s = _mk_state("cantonese")
    s.update("zh", "好的，那我晚点再打给你，谢谢你啊")
    assert s.lang == "zh"
    s.update("en", "that sounds great, let me check and call you back")
    assert s.lang == "en"
    # 粤语客户在普/粤交界回一句地道粤语 → 锁回粤语。
    s = _mk_state("zh")
    s.update("zh", "唔該，我想問下幾時有得賠")
    assert s.lang == "cantonese"


def test_hysteresis_english_short_ack_keeps_prior_lang():
    # zh 客户回「ok / yes」短词：en 标签非强 → 保持 zh。
    s = _mk_state("zh")
    s.update("en", "ok")
    assert s.lang == "zh"


def test_language_state_update_now_accepts_text():
    from agent_runtime.providers.livekit_plugins import LanguageState
    s = LanguageState()
    s.lang = "cantonese"
    # text 带粤语特征但标签模糊 → 提升为 cantonese。
    s.update("zh", "冇人知")
    assert s.lang == "cantonese"


def test_garbage_non_han_does_not_drag_language():
    # ASR 对损坏音频可能吐出越南文/乱码 + zh 标签：不得当成"强普通话"把粤语拉走。
    from agent_runtime.providers.livekit_plugins import LanguageState
    s = LanguageState()
    s.lang = "cantonese"
    s.update("zh", "hổ cũng mong tổng địa thị xoa dịu, không cai sai.")
    assert s.lang == "cantonese", "非汉字乱码不应把当前语言拉成普通话"
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
    ls = LanguageState(); ls.lang = "cantonese"
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
    # 小林:persona.language=cantonese,旧 {zh:男声,cantonese:女声} → 应取粤语女声作全场主音色。
    m = _collapse_voice_map({"zh": "male-qn-qingse", "cantonese": "Cantonese_GentleLady"}, "cantonese")
    assert m == {"zh": "Cantonese_GentleLady"}
    # persona.language=zh → 取普通话男声。
    m = _collapse_voice_map({"zh": "male-qn-qingse", "cantonese": "Cantonese_GentleLady"}, "zh")
    assert m == {"zh": "male-qn-qingse"}
    # en 未配置 / persona 语言缺失 → zh 兜底。
    m = _collapse_voice_map({"zh": "x", "en": "y"}, "")
    assert m == {"zh": "x"}
    # 空 map → 空。
    assert _collapse_voice_map({}, "cantonese") == {}
    # 未知语言键不参与收敛（旧拼写键已由 DB 迁移清零，不再兜别名）。
    m = _collapse_voice_map({"zh": "male-qn-qingse", "cantonese": "Cantonese_GentleLady", "fr": "x"}, "cantonese")
    assert m == {"zh": "Cantonese_GentleLady"}


def test_build_default_voice_map_single_speaker_wins():
    from agent_runtime.agent import _build_default_voice_map
    # 全局配了 speaker（单音色整场同声）→ 各语言都用它。
    m = _build_default_voice_map({"speaker": "Cantonese_crisp_news_anchor_vv2", "speaker_zh": "old-zh", "speaker_cantonese": "old-ca"})
    assert m == {"zh": "Cantonese_crisp_news_anchor_vv2"}
    # 未配 speaker → 回落旧分语言。
    m = _build_default_voice_map({"speaker": "", "speaker_zh": "zh-a", "speaker_cantonese": "ca-b"})
    assert m == {"zh": "zh-a", "cantonese": "ca-b"}
    # 只配 cantonese 分语言键 → 只组 cantonese 键。
    m = _build_default_voice_map({"speaker": "", "speaker_cantonese": "ca-b"})
    assert m == {"cantonese": "ca-b"}


# ---- LLM 输出：港式自然英夹（M3） ----
def test_cantonese_rule_requires_hk_style_code_mixing():
    from agent_runtime.providers.livekit_plugins import ContextState
    ctx = ContextState(account_id="acc-001")
    ctx.set_user_language("cantonese")
    text = ctx.render_system_message()
    assert "港式粵語" in text
    assert "直接輸出繁體" in text and "唔好寫任何簡體字" in text  # 繁体直接输出
    assert "refund" in text and "check" in text  # 允许自然夹英文服务词
    assert "唔好解釋" in text or "唔好解释" in text  # 防泄漏仍保留
    # 数字要用粤语读法：单号逐字读汉字、0 读零、不用阿拉伯数字串。
    assert "七八九零" in text and "一二三四" in text
    # 数字段改为「报码直接复述确认」+ 硬性禁教学(禁拼音/粵拼/入声课程)。
    assert "覆述確認" in text
    assert "嚴禁輸出任何拼音" in text and "Jyutping" in text and "發音教學" in text
    # 高频普→港式口语词表：这个→呢個、什么→乜嘢、现在→而家 等。
    assert "呢個" in text and "乜嘢" in text and "而家" in text and "點解" in text
    # 行业词：快递→速遞、包裹→集運件(香港集运业务语境),并点明不用内地讲法。
    assert "速遞" in text and "集運件" in text and "唔好用「包裹」「快遞」「貨物」" in text


def test_zh_rule_has_no_hk_style():
    from agent_runtime.providers.livekit_plugins import ContextState
    ctx = ContextState(account_id="acc-001")
    ctx.set_user_language("zh")
    text = ctx.render_system_message()
    assert "港式" not in text
    # zh 数字口语复述确认 + 禁拼音/发音教学。
    assert "复述确认" in text and "拼音" in text and "发音教学" in text


# ---- KV-cache 友好重组：稳定指令前缀在前、易变参考尾部在后 ----
def test_render_split_prefix_has_instructions_tail_has_reference():
    from agent_runtime.providers.livekit_plugins import ContextState
    ctx = ContextState()
    ctx.set_user_language("cantonese")
    ctx.set_flow("对话按 4 步流程推进:\n第1步:确认\n第2步:引导", "流程第 2/4 步\n这一步要达成:核对平台")
    ctx.set_knowledge([{"text": "知识库条目"}, {"text": "另一条"}])
    ctx.set_web(["联网结果"])
    ctx.add_summary("user", "客户第一轮说了内容")
    prefix = ctx.render_instruction_prefix()
    tail = ctx.render_context_tail()
    # 稳定指令(用户语言/节奏/准则/话术总览)全在前缀;当前步已移出前缀(推进只改尾部)
    for sec in ("【用户语言】", "【回复节奏】", "【应答准则】", "【话术流程总览"):
        assert sec in prefix, sec
    assert "【现在这一步】" not in prefix
    # 易变参考(当前步/知识/联网/记忆)全在尾部,当前步放尾部最前
    for sec in ("【现在这一步】", "【实时检索到的资料", "【联网检索到的资料", "【本通对话记忆】"):
        assert sec in tail, sec
    # 完整段 = 前缀在前 拼接 尾部
    full = ctx.render_system_message()
    assert full.startswith(prefix) and full.endswith(tail)


def test_render_prefix_stable_when_only_knowledge_changes():
    from agent_runtime.providers.livekit_plugins import ContextState
    ctx = ContextState()
    ctx.set_user_language("cantonese")
    ctx.set_flow("对话按 2 步流程推进:\n第1步:确认", "流程第 1/2 步")
    p1 = ctx.render_instruction_prefix()
    ctx.set_knowledge([{"text": "第 1 轮检索到的新知识" * 5}])
    p2 = ctx.render_instruction_prefix()
    assert p1 == p2, "知识库变化不应影响稳定指令前缀(否则 KV-cache 前缀断裂)"


def test_knowledge_snippet_truncated_to_cap():
    from agent_runtime.providers.livekit_plugins import ContextState
    ctx = ContextState()
    ctx.set_knowledge([{"text": "很长的知识条目" * 200}])  # >350 字
    tail = ctx.render_context_tail()
    # 单条被截到 ~350
    assert len(tail) < 400
    assert "…" in tail


def test_context_rag_gate():
    from agent_runtime.agent import _context_rag_enabled
    import os as _os
    _os.environ.pop("CONTEXT_RAG", None)
    # 绑话术(has_steps) → 默认关 RAG;无模板 → 开
    assert _context_rag_enabled(True) is False
    assert _context_rag_enabled(False) is True
    # 逃生口:CONTEXT_RAG=1 强制开
    _os.environ["CONTEXT_RAG"] = "1"
    try:
        assert _context_rag_enabled(True) is True
    finally:
        _os.environ.pop("CONTEXT_RAG", None)


# ---- MiniMax 表现力:emotion 映射 + 停顿注入 + 拟声放行 ----
def test_mood_to_minimax_emotion_mapping():
    from agent_runtime.plugins.emotion import EmotionState
    # 安抚/焦虑/共情 → sad(低沉柔和);开心 → happy;愤怒 → angry;默认 calm。
    for m, want in [("empathetic", "sad"), ("anxious", "sad"), ("happy", "happy"),
                    ("excited", "happy"), ("angry", "angry"), ("surprised", "surprised"),
                    ("calm", "calm"), ("whatever", "calm")]:
        s = EmotionState(mood=m)
        assert s.minimax_emotion() == want, m


def test_inject_pauses_long_sentence(monkeypatch):
    from agent_runtime.providers.livekit_plugins import _inject_pauses
    monkeypatch.setenv("MINIMAX_PAUSE", "1")
    monkeypatch.setenv("MINIMAX_PAUSE_SECS", "0.3")
    # 长句(≥16 字)句末加 <#0.30#>
    out = _inject_pauses("唔好意思林先生，我幫你 check 返個 status，件貨運輸途中唔見咗。")
    assert "<#0.30#>" in out
    # 短句不插
    out2 = _inject_pauses("好，冇問題。")
    assert "<#" not in out2
    # 可关
    monkeypatch.setenv("MINIMAX_PAUSE", "0")
    assert "<#" not in _inject_pauses("唔好意思林先生，我幫你 check 返個 status，件貨運輸途中唔見咗。")


def test_strip_stage_dirs_allows_minimax_vocal(monkeypatch):
    from agent_runtime.agent import _strip_stage_dirs
    # MiniMax 拟声标签保留(交给 TTS 转声效)
    assert "(sighs)" in _strip_stage_dirs("唔好意思(sighs)，我幫你跟進。")
    assert "(laughs)" in _strip_stage_dirs("咁搞笑(laughs)")
    # 中文全角舞台括号剥掉
    assert "稍作聽筒聲" not in _strip_stage_dirs("（稍作聽筒聲）唔好意思")
    assert "笑" not in _strip_stage_dirs("（笑）我幫你")
    # 半角舞台词剥掉、普通括注保留
    assert "(pause)" not in _strip_stage_dirs("等我 check 下(pause)")
    assert "(例如)" in _strip_stage_dirs("有啲情況(例如)咁")


# ---- 粤语客服语言锚定：始终讲粤语，仅连续多轮才跟随客户切语言 ----
def test_sticky_cantonese_agent_single_mandarin_turn_stays_cantonese():
    from agent_runtime.agent import _sticky_reply_language
    # 锚 cantonese；客户单轮普通话 → 仍回 cantonese(streak 记 1)。
    rl, sticky, streak = _sticky_reply_language("cantonese", "zh", "cantonese", 0)
    assert rl == "cantonese" and sticky == "cantonese" and streak == 1
    # 下一轮仍是普通话 → 跟随切 zh。
    rl, sticky, streak = _sticky_reply_language("cantonese", "zh", sticky, streak)
    assert rl == "zh" and sticky == "zh"


def test_sticky_cantonese_back_to_cantonese_resets():
    from agent_runtime.agent import _sticky_reply_language
    # 已切到 zh，客户回粤语 → 立刻回锚 cantonese。
    rl, sticky, streak = _sticky_reply_language("cantonese", "cantonese", "zh", 0)
    assert rl == "cantonese" and sticky == "cantonese" and streak == 0


def test_sticky_no_anchor_follows_asr():
    from agent_runtime.agent import _sticky_reply_language
    # 无有效锚(空) → 退化为跟随 ASR。
    rl, sticky, streak = _sticky_reply_language("", "zh", "", 0)
    assert rl == "zh"


# ---- 发音教学形输出拦截(lecture_guard) ----
def test_lecture_guard_replaces_jyutping_lesson():
    from agent_runtime.providers.livekit_plugins import lecture_guard
    lesson = (
        "數字粵語用字粵拼（Jyutping）發音要點："
        "1一jat1入聲字，尾音收 - t，發音短促輕快；2二ji6降調；9九gau2高升調……"
    )
    out = lecture_guard(lesson)
    assert "發音要點" not in out
    assert "单号" in out or "單號" in out  # 换成罐头的「请再报单号」(简体字文本 → 普通话罐头)
    # 明确粤语 → 粤语罐头
    out2 = lecture_guard("我哋睇下發音要點：9九gau2高升調", "cantonese")
    assert out2 == "唔好意思，頭先聽得唔係好清楚，可唔可以再講多次個單號或者訂單號碼俾我？"


def test_lecture_guard_passes_normal_replies():
    from agent_runtime.providers.livekit_plugins import lecture_guard
    # 正常客服话(含报码/数字复述)原样放行
    normal = "收到，尾號係七八九零，啱唔啱？"
    assert lecture_guard(normal) == normal
    normal2 = "我幫你 check 返個 status，refund 一般 3–5 個工作天會到帳。"
    assert lecture_guard(normal2) == normal2


def test_lecture_guard_zh_and_en():
    from agent_runtime.providers.livekit_plugins import lecture_guard
    zh_lesson = "这是普通话发音教学：zhei 是声调，韵母是 ei……"
    assert "單號" not in lecture_guard(zh_lesson)  # 换 zh 罐头(不含粤语字)
    en_normal = "Please confirm your tracking number 7890."
    assert lecture_guard(en_normal, "en") == en_normal  # 正常英文原样


def test_lecture_guard_short_pinyin_not_false_positive():
    from agent_runtime.providers.livekit_plugins import lecture_guard
    # 1-2 个「字母+调号」token 唔当课程(正常夹英文/型号)
    assert lecture_guard("型號 version2 同 order3 嗰兩單") == "型號 version2 同 order3 嗰兩單"


# ---- <|im_end|> 等 EOS token 剥除(转录 + 流式 TTS,含跨 chunk 半截) ----
def test_strip_eos_tokens_sync():
    from agent_runtime.agent import _clean_transcript, _strip_eos_tokens
    # 转录落库前剥走 Qwen3 偶发输出嘅模板收尾 token。
    assert _strip_eos_tokens("收到，尾號七八九零<|im_end|>") == "收到，尾號七八九零"
    assert _strip_eos_tokens("<|im_start|>你好") == "你好"
    assert _clean_transcript("你好，林先生。<|im_end|>") == "你好，林先生。"
    assert _clean_transcript("完全冇 token 嘅句。") == "完全冇 token 嘅句。"


def test_strip_eos_tokens_streaming_across_chunks():
    from agent_runtime.agent import _trailing_eos_partial
    import asyncio
    from agent_runtime.agent import _strip_expr_markup

    async def _drain(chunks):
        out = ""
        async for piece in _strip_expr_markup(_agen(chunks)):
            out += piece
        return out

    async def _agen(items):
        for it in items:
            yield it

    async def _run():
        # token 被 stream 喺「|im_」中间切开:第一段应hold半截,下一段先剥净。
        c1 = _trailing_eos_partial("收到，尾號七八九零<|im_e")
        assert c1 == "<|im_e"
        r = await _drain(["收到，尾號七八九零<|im_", "end|>"])
        assert r == "收到，尾號七八九零"
        r2 = await _drain(["好嘅，冇問題。<|im_end|>"])
        assert r2 == "好嘅，冇問題。"
        # <expr> 半截处理唔应被 eos 逻辑影响
        assert _trailing_eos_partial("正常內容<expr type=") == ""

    asyncio.run(_run())


# ---- P4-C 回归:联网检索尾巴收紧(≤1 条 ×150 字)+ 前缀语言规则会话开始即稳定 ----
def test_web_snippets_capped_to_one_short_item():
    from agent_runtime.providers.livekit_plugins import ContextState
    ctx = ContextState(account_id="acc-001")
    # 旧实现保留 2 条且不截断(P4 实测尾部被 Wikipedia 撑到 500-900 token):
    # 现在最多 1 条、单条 150 字——开放域杂音既挤尾部预算又会带偏 4B 小模型。
    ctx.set_web(["很长的联网摘要" * 100, "第二条联网结果"])
    tail = ctx.render_context_tail()
    assert "第二条联网结果" not in tail
    assert len(ctx._web) == 1 and len(ctx._web[0]) <= 150
    assert len(tail) < 400
    # 空结果清空尾巴。
    ctx.set_web([])
    assert ctx.render_context_tail().find("【联网检索到的资料") == -1


def test_user_language_rule_stable_across_same_lang_turns():
    from agent_runtime.providers.livekit_plugins import ContextState
    ctx = ContextState(account_id="acc-001")
    # 会话开始前就按 greet_lang 锚定(agent.py 修复点):问候轮与首轮 user lang
    # 一致时前缀字节不变——旧代码首轮才首次写入语言规则,问候→首轮前缀断裂。
    ctx.set_user_language("cantonese")
    p1 = ctx.render_instruction_prefix()
    ctx.set_user_language("cantonese")  # ASR 同语言轮:锚定重算但值不变
    p2 = ctx.render_instruction_prefix()
    assert p1 == p2
