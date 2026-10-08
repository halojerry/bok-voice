"""双端同声传译 interpreter(B 线 v2):LiveKit 房间 × AgentSession 对话模型。

每个方向一个 worker 进程(agent_name=bok-interp-fwd/rev,由 INTERP_DIRECTION 决定),
复用 A 线同一套会话机制:AgentSession + silero VAD(基线参数同 A 线) +
Qwen3-ASR(源语言钉死) + 翻译 LLM(Hy-MT2 MT 小模型 :1236 逐句无状态优先,
缺省回退主 LLM :1235 / DeepSeek 云端,「只输出译文」) + TTS(目标语言音色,
本地 Qwen3-TTS 兜底 / settings 选 MiniMax 云端)。

- 听谁:RoomInputOptions(participant_identity) —— fwd 听 me-<room>,rev 听 other-<room>。
- 译文给谁:发布译文轨后 set_track_subscription_permissions 幂等白名单——
  fwd 只授权 other-<room>、rev 只授权 me-<room>(「我方输出=对方听到的内容」;
  开源 SFU 在订阅时强制执行;双方原声互听不受影响,权限只约束 agent 自己发的轨)。
- 字幕:AgentSession 自带 lk.transcription 转写流(原文+译文都广播),
  前端 useTranscriptions 渲染双栏——音频定向、字幕全量,各听各的、都看得到。
- 落库/沉淀:译文句到达时 add_turn(role=说话方, transcript="原文：…\\n译文：…");
  房间断开 settle → 总结/知识蒸馏/vault 原文落盘,全套复用 A 线结算链路。

方向与语言对来自 CP 签发 me 端 token 时挂的 RoomAgentDispatch metadata:
  {"listen": "me-", "deliver": "other-", "source_lang": "zh", "target_lang": "en"}
"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import json
import math
import os
import re
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

# 模型路由共享契约(2026-09-25 阶段 0):mt/a_reply 车道本地↔云端解析单点,只消费。
from bok_voice_core.model_routes import PROVIDER_OPENAI, resolve_route
# 人设音色 map 解析（单源 core，2026-10-08 B 线人设音色复用；A 线同源）。
from bok_voice_core.voice_map import parse_voice_map

# B 线 MT 出口确定性语言校验器(E5 增补):纯函数、零 LLM、零网络。判据本身只做
# 脚本族(CJK vs 拉丁)判定——见 bok_voice_core.mt_lang_check 模块 docstring 的
# 能/不能边界(治 en↔zh/cantonese 的脚本级错语言;测不出 zh↔cantonese)。
from bok_voice_core.mt_lang_check import language_match_score, looks_like_language


def _norm_lang(raw: str, default: str = "zh") -> str:
    key = (raw or "").strip().lower()
    if key in {"zh", "chinese", "mandarin", "普通话", "中文"}:
        return "zh"
    if key in {"cantonese", "粤", "粤语", "广东话"}:
        return "cantonese"
    if key in {"en", "english", "英语"}:
        return "en"
    # 2026-10-06 W2 四语扩容（demo-quality-wave §W2）：德/法/日/葡进归一表——
    # 不加则 target_lang="ja" 等落 default 折回 zh，boost_map/会话级音色全链哑火。
    if key in {"de", "german", "德语", "德文"}:
        return "de"
    if key in {"fr", "french", "法语", "法文"}:
        return "fr"
    if key in {"ja", "japanese", "日语", "日文"}:
        return "ja"
    if key in {"pt", "portuguese", "葡萄牙语", "葡语"}:
        return "pt"
    return default


def _minimax_asr_lane(source_lang: str, minimax_key: str) -> bool:
    """四语 ASR 车道判定（2026-10-07 W2c；纯函数，单测直喂）。

    B 线源语 ∈ {de,fr,ja,pt} 时豆包 SAUC 24/24 幻听（实测不可用），MiniMax
    asr-1.0 24/24 CER≤0.08——四语源语优先走 MiniMax 伪流式（官方 StreamAdapter
    包 offline provider）。三条件齐备才上车：源语在集内 + 总闸开
    （BOK_MINIMAX_ASR=0 一键回旧装配链逐字节）+ 凭据在场。zh/cantonese/en
    不在此集=既有装配链逐字节。
    """
    from .providers.minimax_asr import MINIMAX_ASR_4LANG, minimax_asr_enabled

    return (
        source_lang in MINIMAX_ASR_4LANG
        and minimax_asr_enabled()
        and bool((minimax_key or "").strip())
    )


def _translation_instructions(src: str, tgt: str, glossary: str = "") -> str:
    """同传 system 指令(对齐 v1 已退役 Node POC 的 local-openai prompt——目录
    2026-10-02 删除,prompt 血统见 git 史;补电话同传节奏与港式粤语输出规则)。
    glossary 非空时追加术语行——回退 LLM
    路径(DeepSeek/主 LLM)的术语一致性挂点;MT 快路(StatelessMTLLM)走
    _mt_prompt 的术语槽,不靠 instructions。"""
    names = {
        "zh": "Mandarin Chinese",
        "cantonese": "Hong Kong Cantonese (港式粤语口語,繁體)",
        "en": "natural spoken English",
    }
    s = names.get(src, src)
    t = names.get(tgt, tgt)
    lines = [
        "You are a professional simultaneous-interpretation engine on a live phone call.",
        f"Translate EVERY user utterance from {s} into {t}.",
        "Rules:",
        "- Output ONLY the translation; no explanations, no quotes, no notes, never the source language.",
        "- Keep names, numbers and technical terms where sensible; preserve the original tone (casual/courteous).",
        "- Speak like a live interpreter: short spoken sentences, one utterance at a time, no summaries.",
        "- If the utterance is already in the target language, output it unchanged.",
        # 语气上下文层(2026-10-08,call-996f3917):回退/云端 LLM 车道吃指令(本地
        # Hy-MT2 对模板外指示无视——实测留档,_mt_prompt docstring),把笑声/叹气/
        # 咳嗽按语境转括号标记而非照词翻译;确定性兜底=interpret._apply_voice_tags。
        "- If the speaker laughs, giggles, sighs or coughs, render the sound as a bracket tag in place of the sound word: "
        "(laughs) / (chuckle) / (sighs) / (coughs) — at most one tag per sentence, only for genuine vocal sounds.",
        # ASR 噪声纠错层(2026-10-08 Wave 1「MT 切 DeepSeek」):源文是实时 ASR 转写,
        # 同音误听/吞字/碎片难免——结合上下文先纠明显误识再译(c3ed3ef3 实证反例:
        # 云端 LLM 曾把碎噪声脑补成完整句)。铁律:绝不虚构、绝不应答、绝不加没说
        # 过的内容;纠不了的碎片宁可丢弃不硬译。
        "- The source is a live ASR transcript and may contain homophone mishearings or garbled fragments: "
        "use the conversation context to correct obvious misrecognitions before translating, and drop "
        "meaningless fragments rather than inventing content. Never answer, never explain, never add "
        "anything that was not said.",
    ]
    if glossary:
        lines.append(f"- Glossary (keep these renderings exactly): {glossary}")
    if tgt == "cantonese":
        lines.append(
            "- 港式粵語:輸出繁體中文口語(唔好用書面語/普通話詞),"
            "數字/單號逐個讀寫漢字(7890→七八九零),可自然夾英文詞。"
        )
    return "\n".join(lines)


# MiniMax 2.8 系语气词标记(官方文档 2026-09-16 核实:仅 speech-2.8-hd/2.8-turbo
# 支持;非 2.8 模型会把标记当文本念出来——所以有 _voice_tags_supported 门控)。
# 插入词汇表单源=A 线 voice_style.VOICE_TAG_WHITELIST(2026-10-08 复用立法,
# call-996f3917:句尾「…? Hahaha.」被 2.8 逐字念出+纯笑句译文列空白);剥除面更宽
# (白名单 ∪ 官方全集——MT 车道可能吐任意官方标记,字幕面要全收)。
from .voice_style import VOICE_TAG_WHITELIST, norm_voice_tag

_STRIP_TAG_INNER: frozenset[str] = VOICE_TAG_WHITELIST | frozenset(
    "groan groans pant pants gasp gasps sniff sniffs snort snorts burp burps "
    "lip-smacking humming hissing sneeze sneezes cough sigh breaths chuckles laugh".split()
)
# 括号 token:ASCII + 全角(镜像 A 线 _PAREN_RE 形状;云端 MT 车道按 instructions
# 产出标记会写全角（laughs）,只认 ASCII 会漏归一/漏剥/被非 2.8 档念出来)。
_TAG_PAREN_RE = re.compile(r"[（(]([^（）()]{1,24})[）)]")
# 引导词→标记:Hy-MT2 会把源文语气词照词翻译(Hahaha/Coughs/Ah),MiniMax 2.8
# 对这些词只会「念字」;say 前换成括号标记,合成层才出真声(笑/咳/叹)。
_VOICE_TAG_LEAD_RE = re.compile(
    r"^\s*((?:(?:ha){2,}|(?:he)+|lol|coughs?|ahem|sighs?|alas)\b)[,;:!.\s]*",
    re.IGNORECASE,
)
# 任意位置独立笑声簇(2026-10-08 v2):旧版只动句首——call-996f3917 句尾
# 「呃，你在说什么？哈哈哈。」译出「…? Hahaha.」漏网被逐字念出。token=笑声/
# 轻笑拟声词簇(拉丁侧 (?![A-Za-z0-9]) 词边界防误伤内容词;CJK 簇 ≥2),尾随
# 标点收编进替换(标记不吃标点),中文目标语「哈哈哈/嘻嘻/嘿嘿/呵呵」同收。
_INTERJECT_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:(?:ha){2,}|(?:he){2,}|lol|lmao|哈{2,}|嘻{2,}|嘿{2,}|呵{2,}))"
    r"(?![A-Za-z0-9])(?:\s*[.,;:!?，。！？；：、]+)?",
    re.IGNORECASE,
)


def _voice_tags_supported(model: str) -> bool:
    """语气词标记仅 2.8 系合成模型支持(纯函数,单测直喂)。"""
    return "2.8" in (model or "")


def _resolve_minimax_model() -> str:
    """B 线 MiniMax 合成档解析(纯读 env,单测直喂):显式 env > B 线默认 2.8-turbo。

    旧契约是 _build_tts_provider 里 setdefault 写 MINIMAX_MODEL 再全链读 env——
    常驻 worker 写入即驻留,且 voice_tags 门读的是同一键;现单点解析、构造经
    model_override 下发,进程 env 零写入(评审 follow-up,与采样档 P2-3 同治理)。
    """
    return (os.environ.get("MINIMAX_MODEL") or "").strip() or "speech-2.8-turbo"


def _canonical_tags(text: str) -> str:
    """白名单标记归一(纯函数):全角括号/大写→小写 ASCII 括号形(MiniMax 只认
    ASCII 形)。非白名单括号词是可能的内容((USA)/(广东话)),逐字不动——判据与
    A 线 sanitize 同源(白名单内才动),但**不做**句首未知剥除:MT 输出的句首括号
    更可能是真内容,不是 4B 自创标签。"""
    return _TAG_PAREN_RE.sub(
        lambda m: (
            f"({norm_voice_tag(m.group(1))})"
            if norm_voice_tag(m.group(1)) in VOICE_TAG_WHITELIST
            else m.group(0)
        ),
        text,
    )


def _interject_tag(word: str) -> str:
    """拟声词簇→白名单标记(ha 族/lol/lmao/哈→laughs;hehe/嘻/嘿/呵→chuckle)。"""
    w = word.lower()
    if w.startswith("ha") or w in ("lol", "lmao") or w.startswith("哈"):
        return "(laughs)"
    return "(chuckle)"


def _apply_voice_tags(text: str) -> str:
    """语气拟声词 → MiniMax 2.8 语气标记(纯函数,单测直喂)。

    v2(2026-10-08,call-996f3917 实证):旧版只动句首,句尾/句中笑声漏网被 2.8
    逐字念出(假人念稿)。现三层:①白名单标记归一(全角/大写→ASCII,A 线词汇表
    单源);②句首引导词(旧契约,coughs/ahem/sighs/alas 词形);③任意位置独立
    笑声簇(拉丁+CJK,尾随标点收编)。产出恒 ⊆ A 线 VOICE_TAG_WHITELIST(test
    钉死);中性句原样返回。"""
    if not text:
        return text
    out = _canonical_tags(text)
    m = _VOICE_TAG_LEAD_RE.match(out)
    if m:
        word = m.group(1).lower()
        if word.startswith("hah") or word == "lol":
            tag = "(laughs)"
        elif word.startswith("heh"):
            tag = "(chuckle)"
        elif word.startswith(("cough", "ahem")):
            tag = "(coughs)"
        elif word.startswith(("sigh", "alas")):
            tag = "(sighs)"
        else:
            tag = ""
        if tag:
            rest = out[m.end():].lstrip()
            out = f"{tag} {rest}" if rest else tag

    def _r(mm: re.Match) -> str:
        tag = _interject_tag(mm.group(1))
        if mm.start() == 0:
            return tag
        # 前邻是字母数字才补空格;前邻是标点(CJK 常形「…，哈哈哈。」)不补——
        # 字幕剥标记后不留「， 」悬挂空格。
        prev = out[mm.start() - 1]
        return f" {tag}" if prev.isalnum() else tag

    out = _INTERJECT_RE.sub(_r, out).strip()
    return re.sub(r"\s{2,}", " ", out)


def _has_strip_tag(text: str) -> bool:
    """文本是否含剥除面内的标记(tag-free 文本走零漂移快路径的判据)。"""
    return any(norm_voice_tag(x) in _STRIP_TAG_INNER for x in _TAG_PAREN_RE.findall(text or ""))


def _strip_voice_tags(text: str) -> str:
    """剥语气词标记(字幕/落库口径):标记只属合成层,不该出现在读者面前。
    剥除面=白名单 ∪ 官方全集,括号收全角形(云端 MT 车道会写全角（laughs）)。
    剥后把标点前的悬挂空格收掉(「morning (laughs),」→「morning,」),句尾被标
    记带走的分隔符残渣(「太好笑了，」)一并清。"""
    cleaned = _TAG_PAREN_RE.sub(
        lambda m: "" if norm_voice_tag(m.group(1)) in _STRIP_TAG_INNER else m.group(0), text
    )
    cleaned = re.sub(r"\s+([,!?;:.，。？！；：、])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return re.sub(r"[,，、;；]\s*$", "", cleaned).strip()


def _speech_text(text: str, tags_on: bool) -> str:
    """译文出声文本单点(2026-10-08):门开=拟声词→2.8 标记;门关=剥标记。

    门关也要剥:云端 MT 车道按 instructions 语气规则产出标记(_translation_
    instructions),非 2.8 合成/text-only 档把它念出来=假人念稿。tag-free 文本
    逐字节原样(零漂移铁律)。"""
    if tags_on:
        return _apply_voice_tags(text)
    return _strip_voice_tags(text) if _has_strip_tag(text) else text


# 纯语气句的读者面占位(call-996f3917:「哈哈。」整句转 (laughs) 后字幕译文列
# 空白)。zh 系(含粤语/日语)用中文记法,其余用拉丁记法;（笑）是转写通行记法。
_CAPTION_TAG_ZH = {
    "laughs": "（笑）", "chuckle": "（轻笑）", "sighs": "（叹气）", "coughs": "（咳嗽）",
    "breath": "（换气）", "emm": "（嗯）", "inhale": "（吸气）", "exhale": "（呼气）",
    "clear-throat": "（清嗓）", "groan": "（叹气）", "groans": "（叹气）",
}
_CAPTION_TAG_EN = {
    "laughs": "(laughs)", "chuckle": "(chuckles)", "sighs": "(sighs)", "coughs": "(coughs)",
    "groan": "(groans)", "groans": "(groans)",
}


def _polish_for_mt(text: str, lang: str) -> str:
    """B 线 MT 入口确定性音近吸附(纯函数,单测直喂;2026-10-08 P0)。

    今晚实测(69 条原文 ≈19% 噪声被忠实翻译,如「我成鸟解下」→「我成鳥解下你
    哋嘅產品」):ASR 错字原文直灌 MT=错得整整齐齐。复用 A 线 asr_polish 的
    确定性层(纯本地 ~1ms、数字/拉丁冻结、>max_edits 整层放弃、粤特征字守卫),
    kill-switch 同 ``BOK_ASR_POLISH``。**原文单轨铁律**——本函数只喂 MT 上下文
    与 ``_mt_pairs`` 滚动对;账本/字幕/spec 判定/QA 快路全吃 raw(调用点纪律,
    source-pin 测试钉死)。四语源(de/fr/ja/pt)表空且 detect_lane 会把日文嗅成
    zh,直通原样。"""
    if lang not in ("zh", "cantonese", "en") or not text:
        return text
    from .asr_polish_runtime import sync_polish

    return sync_polish(text, lang)


# 纯标点/空白残渣判据:剥标记后只剩「.」「，」等不算有内容(云端 MT 直接吐
# "(laughs)." 的形状),按纯语气句走占位。
_PUNCT_ONLY_RE = re.compile(r"^[\s.,;:!?，。！？；：、…]+$")


def _caption_text(text: str, lang: str) -> str:
    """译文行读者口径:剥合成层标记;纯语气句剥后为空(或只剩标点)→本地化占位。

    空译文行=读者面破相(通话记录/字幕只见空白不知发生了什么);占位让「这里
    是一声笑」可读,结算/蒸馏读到的也是有意义的转写记法而非空串。"""
    stripped = _strip_voice_tags(text)
    if stripped and not _PUNCT_ONLY_RE.match(stripped):
        return stripped
    m = next((x for x in _TAG_PAREN_RE.findall(text or "") if norm_voice_tag(x) in _STRIP_TAG_INNER), None)
    if not m:
        return stripped
    key = norm_voice_tag(m)
    zh_side = lang in ("zh", "cantonese", "ja")
    table = _CAPTION_TAG_ZH if zh_side else _CAPTION_TAG_EN
    return table.get(key, "（语气）" if zh_side else "(tone)")


# 术语表分隔符:中英逗号/顿号/分号/换行都收(与 A 线 hotwords 字段同口径)。
_GLOSSARY_SPLIT_RE = re.compile(r"[,，、;；\n]+")
# MT prompt 术语块总长护栏:术语表是会话级常量,进每轮请求前缀——超长吃
# 1.8B MT 模型的 prefill;超限从尾部丢弃(装配期一次性,运行时零开销)。
_GLOSSARY_MAX_CHARS = 400


def parse_glossary(raw: str) -> tuple[tuple[str, str], ...]:
    """解析术语表文本(纯函数,单测用):每条「源=译」或纯词条,分隔符同 hotwords。

    纯词条(无=)表示「源语原样保留」:ASR 热词照收,MT 侧提示保持原词。
    顺序保留(先到先得),空段丢弃;不做大小写归一(专名区分大小写)。
    """
    pairs: list[tuple[str, str]] = []
    for part in _GLOSSARY_SPLIT_RE.split(str(raw or "")):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            src, _, tgt = part.partition("=")
            src, tgt = src.strip(), tgt.strip()
            if src:
                pairs.append((src, tgt))
        else:
            pairs.append((part, ""))
    return tuple(pairs)


def glossary_block(pairs) -> str:
    """渲染 MT prompt 术语块(纯函数):「A=B；C=D」;空对/全部超限→空串(逐字节同旧)。"""
    if not pairs:
        return ""
    parts: list[str] = []
    total = 0
    for src, tgt in pairs:
        piece = f"{src}={tgt}" if tgt else src
        if total + len(piece) + 1 > _GLOSSARY_MAX_CHARS:
            break
        parts.append(piece)
        total += len(piece) + 1
    return "；".join(parts)


def glossary_source_terms(pairs) -> str:
    """ASR 热词侧:逗号拼接源语词条(数字主导项由 asr_hotword_context 统一丢弃)。"""
    return ",".join(src for src, _tgt in pairs)


def _turn_handling_opts() -> dict:
    """B 线 turn_handling 组装(纯函数,单测喂 env 断言;与 A 线同一对 env 单源)。

    2026-09-16 P2 定案:框架模式 **manual**——STT 句级 FINAL 不再走框架自动回复,
    由 worker 的 `user_input_transcribed(is_final=True)` 监听自驱 MT→`session.say()`
    队列。理由(实弹 probe_interp_backlog 实证):框架对「播报中到达的新用户轮」
    只有两条路——打断(current_speech.allow_interruptions=True,整轮取消生成中/
    播报中的译文)或**整轮丢弃**(=False,agent_activity「skipping reply to user
    input, current speech generation cannot be interrupted」,连原文落库都没有;
    低门槛压测 8 句只落 5 句)。两条都唔係译员行为;manual 零丢弃零打断,句子
    进 say 队列串行播,背压归 _PlaybackBacklog 管。kill-switch 配对不变:
    TURN_DETECTION≠stt → 不设 turn_detection(框架回 EOT)+ 插件句级 FINAL 熄火,
    旧行为原样回退。打断恒关(BOK_INTERP_INTERRUPT=1 也不该开——manual 下无
    自动回复可打断,仅余 VAD 音频打断译文的实验危害)。"""
    from .agent import _endpointing_delays_from_env, _turn_detection_mode_from_env

    mode = _turn_detection_mode_from_env()
    min_delay, max_delay = _endpointing_delays_from_env()
    opts: dict = {
        "endpointing": {"mode": "dynamic", "min_delay": min_delay, "max_delay": max_delay},
        "preemptive_generation": _preemptive_generation_opts(),
        "interruption": {
            # 同传语义:manual 模式下框架不再自动回复,此开关只余「音频活动打断
            # 译文」一条路,恒关。字段保留为显式声明+防未来框架行为变化。
            "enabled": False,
            "min_duration": float(os.environ.get("INTERRUPT_MIN_DURATION", "0.6")),
            "min_words": 0,
            "resume_false_interruption": os.environ.get("RESUME_FALSE_INTERRUPTION", "1") == "1",
            "false_interruption_timeout": float(os.environ.get("FALSE_INTERRUPTION_TIMEOUT", "1.0")),
        },
    }
    if mode == "stt":
        # 与 sentence_commit_enabled 同判:TURN_DETECTION=stt(默认)→ B 线走
        # manual 自驱管线;其余值(vad/""→EOT)整族回退框架自动回复旧档——
        # 插件句级 FINAL 同源熄火,不可能出现「manual 模式吃不到句子 FINAL」
        # 的错配(kill-switch 一对 env 同进同退)。
        opts["turn_detection"] = "manual"
    return opts


def _build_mt_context(instructions: str, pairs, text: str):
    """单句翻译用 ChatContext(纯函数,单测直喂):instructions(系统)+滚动「源→译」
    对(旧→新)+当前句(user)。StatelessMTLLM 只读最后一条 user(模板无状态),
    滚动对由 _rolling_pairs 现场重抽做参考块;回退通用 LLM 路径则把 instructions
    与对历史当真实上下文翻译。"""
    from livekit.agents import llm as lk_llm

    ctx = lk_llm.ChatContext()
    if instructions:
        ctx.add_message(role="system", content=[instructions])
    for src_t, tgt_t in pairs:
        ctx.add_message(role="user", content=[src_t])
        ctx.add_message(role="assistant", content=[tgt_t])
    ctx.add_message(role="user", content=[text])
    return ctx


def _mt_lang_guard_enabled() -> bool:
    """B 线 MT 出口语言校验器总闸(E5 增补,**默认开**)。

    默认开的理由:判据是纯 Python 正则扫描(微秒级),正常轮**零额外网络/零额外
    延迟**;只有输出真判为错语言时才多一次 MT 往返(且至多一次)。即「宁可在病态
    轮多花一次往返,也不放一整句错语言出去」——正常路径零回归,病态路径有界
    代价,故默认安全。``BOK_INTERP_MT_LANGGUARD=0`` 一键回退到「出口不校验、
    照原样出稿」的旧行为。"""
    return os.environ.get("BOK_INTERP_MT_LANGGUARD", "1") == "1"


def _mt_open_stream(llm_provider, ctx, *, retry: bool):
    """开一条单句翻译流:conn_options 必显式给 max_retry=1。

    插件内芯直接读 conn_options.max_retry,session 托管调用才有默认值,直调传
    None 会 AttributeError(max_retry of None)。直调档单次尝试不重试:重试是延迟
    放大器,积压由背压门槛管。``retry=True`` 走 StatelessMTLLM.chat_retry(强化
    prompt);通用回退 LLM 无该方法,调用方据此决定是否可重试。"""
    from livekit.agents import APIConnectOptions

    opts = APIConnectOptions(max_retry=1)
    if retry:
        return llm_provider.chat_retry(chat_ctx=ctx, conn_options=opts)
    return llm_provider.chat(chat_ctx=ctx, conn_options=opts)


async def _mt_collect(stream, timeout_s: float) -> str:
    """把一条翻译流排空成整句(超时保护),awaitable 流先 await(旧 _mt_once 同款)。

    超时也关流(2026-10-02 审计修):旧版 wait_for 超时只 cancel 读取端,流本体
    (含 _StripMTQuoteStream 泵+原生 openai 流)继续解码到自然完稿——每次
    15s 超时留一条全量 512-token 僵尸占 :1236 单生成线程(AGENTS 记 21 次/通,
    「句堆积」形状);MT 回落主 LLM 时僵尸坐在 reply lane。finally 级联关闭
    (mixin 已挂,内芯 _attach_mlx_abort 的 aclose 补丁由此外达,server 端止损)。"""
    if inspect.isawaitable(stream):
        stream = await stream
    parts: list[str] = []

    async def _drain() -> None:
        async for chunk in stream:
            delta = getattr(chunk, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)

    try:
        await asyncio.wait_for(_drain(), timeout=timeout_s)
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - 关流尽力而为,不吞业务结果
                pass
    return "".join(parts).strip()


def _src_track_state(participant, audio_kind) -> tuple[str, list[str]]:
    """listen 身份的音轨订阅态分类(纯函数,遥测看护消费)。

    返回 ("none", [])           —— 对端无任何音轨发布;
    ("unsubscribed", [sid...]) —— 有音轨但本 worker 未订上(publication.track
                                  is None;sdk 无手动订阅口,此态=订阅链断);
    ("ok", [])                  —— 至少一条音轨已订阅在席。
    participant 为 None(对端未进房)按 none 计。"""
    pubs = list(participant.track_publications.values()) if participant is not None else []
    audio = [p for p in pubs if getattr(p, "kind", None) == audio_kind]
    if not audio:
        return ("none", [])
    unsubs = [str(p.sid) for p in audio if getattr(p, "track", None) is None]
    if unsubs:
        return ("unsubscribed", unsubs)
    return ("ok", [])


async def _src_track_watch_loop(
    room, listen_identity: str, closed: asyncio.Event, interval: float, heal: bool
) -> None:
    """listen 身份音轨订阅看护循环(2026-10-02 刀1 自闭包提取,假 room 可驱动)。

    行为与旧 entrypoint 内联闭包逐字节同口径(2026-09-30 call-72112fd7 定案):
    每 interval 秒扫一次 listen 身份的音轨,分辨两态留观测行——
      SRC_NO_AUDIO_TRACK       —— 对端根本没发麦(未开传译/浏览器发布失败);
      SRC_TRACK_NOT_SUBSCRIBED —— 音轨已发布但本 worker 订不上(订阅链断)。
    heal=True 时在 unsubscribed 态对「已发布未订上」的音轨发 set_subscribed(True)
    (官方手动订阅口,官方 job.py 自己在用;前 3 轮每轮一次、其后每 10 轮一次),
    打 SRC_TRACK_RESUBSCRIBE 观测行。closed 置位(会话关闭)即返回。

    RC-1 根因(2026-10-02 定案):旧闭包 inline 消费 room.remote_participants.get()
    而 heal 分支引用未定义的 `part` → 首次进入 unsubscribed 态 NameError 杀死看护
    (裸 create_task 静默死,遥测+自愈同时熄火)。现每轮 **part 单点解析**,分类与
    自愈同源;迭代体全包 try/except 纵深防御(任何异常不杀循环,只打观测行)。
    """
    from livekit import rtc as _rtc  # 局部导入:模块头部无 rtc 面

    no_track_rounds = 0
    not_sub_rounds = 0
    while True:
        try:
            await asyncio.wait_for(closed.wait(), timeout=interval)
            return  # 会话收线,收队
        except asyncio.TimeoutError:
            pass
        try:
            part = room.remote_participants.get(listen_identity)
            state, sids = _src_track_state(part, _rtc.TrackKind.KIND_AUDIO)
            if state == "none":
                not_sub_rounds = 0
                no_track_rounds += 1
                if no_track_rounds == 3 or no_track_rounds % 10 == 0:
                    print(
                        f"[interp] SRC_NO_AUDIO_TRACK identity={listen_identity} "
                        f"rounds={no_track_rounds} (对端未发布麦克风/未开传译)",
                        flush=True,
                    )
            elif state == "unsubscribed":
                no_track_rounds = 0
                not_sub_rounds += 1
                if not_sub_rounds <= 3 or not_sub_rounds % 10 == 0:
                    print(
                        f"[interp] SRC_TRACK_NOT_SUBSCRIBED identity={listen_identity} "
                        f"sids={sids} rounds={not_sub_rounds} (音轨已发布但未订上=订阅链断)",
                        flush=True,
                    )
                # 【B 线订阅自愈(2026-09-30 官方对账定案)】RemoteTrackPublication
                # .set_subscribed(True) 即官方手动订阅口(官方 job.py 自己在用;
                # 此前误判"SDK 无手动订阅口")——检测到「已发布未订上」时重发订阅
                # 请求,前 3 轮每轮一次、其后每 10 轮一次,打 SRC_TRACK_RESUBSCRIBE
                # 观测行。call-72112fd7 形态(fwd 对 me- 轨零订阅静默 3 分钟)从
                # 只观测升级为自愈。heal=False 回纯观测档(BOK_INTERP_SRC_HEAL=0)。
                if (
                    heal
                    and (not_sub_rounds <= 3 or not_sub_rounds % 10 == 0)
                    and part is not None
                ):
                    _healed: list[str] = []
                    for _p in part.track_publications.values():
                        if (
                            getattr(_p, "kind", None) == _rtc.TrackKind.KIND_AUDIO
                            and getattr(_p, "track", None) is None
                        ):
                            try:
                                _p.set_subscribed(True)
                                _healed.append(str(_p.sid))
                            except Exception as exc:  # noqa: BLE001 - 自愈失败唔阻看护
                                print(
                                    f"[interp] SRC_TRACK_RESUBSCRIBE failed sid={getattr(_p, 'sid', '?')} exc={exc!r}",
                                    flush=True,
                                )
                    if _healed:
                        print(
                            f"[interp] SRC_TRACK_RESUBSCRIBE identity={listen_identity} "
                            f"sids={_healed} rounds={not_sub_rounds}",
                            flush=True,
                        )
            else:
                no_track_rounds = 0
                not_sub_rounds = 0
        except Exception as exc:  # noqa: BLE001 - 纵深防御:单轮异常绝不杀看护循环
            print(f"[interp] SRC_TRACK_WATCH_ERR {exc!r}", flush=True)


# ---------------------------------------------------------------------------
# Wave 2 流式交付(2026-10-08「B 线 A 线化」):MT 流逐子句喂 session.say(
# AsyncIterable)——框架 tee 后自切句、首子句即合成开播、余句边播边合成
# (agent_activity _produce_segments,与 A 线 generate_reply 同机制);一源句=
# 一 say=一 item,账本/_on_item/字幕口径全部不变。DeepSeek 首 token ~350ms,
# 首子句约 +400ms 即交 TTS——旧路径(整句排干再合成)的尾段等待被吃掉。
# kill-switch BOK_INTERP_MT_STREAM_SAY=0 回旧整句路径(字节回退钉)。
# ---------------------------------------------------------------------------

_CLAUSE_CUT_RE = re.compile(r"[，。！？；、,.!?;:…\n]")
# 强切上限:缓冲无任何句界标点(极端长从句/无标点语流)时防卡流的硬顶。
_CLAUSE_CUT_MAX_CHARS = 60


def _cut_clause_piece(buf: str, *, max_chars: int = _CLAUSE_CUT_MAX_CHARS) -> tuple[str, str]:
    """缓冲 → (可发射子句前缀, 余量)(纯函数,单测直喂)。

    切点=子句边界标点(中英逗号/句读/省略/换行);**小数/千分位保护**:前后皆
    数字字符的 `.`/`,` 不是句界(「3.14」「1,000」永不劈);超 max_chars 无边界
    则强切(拉丁空格优先,CJK 硬切)——TTS 侧框架还有一层句切,这里切得糙一点
    只影响语气标记换算的粒度,不影响合成正确性。"""
    for m in _CLAUSE_CUT_RE.finditer(buf):
        i = m.start()
        if buf[i] in ".,," and 0 < i < len(buf) - 1 and buf[i - 1].isdigit() and buf[i + 1].isdigit():
            continue
        return buf[: i + 1], buf[i + 1 :]
    if len(buf) >= max_chars:
        j = buf.rfind(" ", 8, max_chars)
        cut = j if j > 0 else max_chars
        return buf[:cut], buf[cut:]
    return "", buf


def _mt_stream_say_enabled() -> bool:
    """Wave 2 总闸(缺省开;0=回旧「整句排干再 say」路径,含字幕同步时序)。"""
    return os.environ.get("BOK_INTERP_MT_STREAM_SAY", "1") == "1"


# —— W0-2 MT 首 chunk 早交(2026-10-08 时效波) -----------------------------------
# ③号基线实锤:B 线 MT 腿结构性缺口 +224ms(890 vs A 666)——短句无子句切点,
# `_cut_clause_piece` 憋到流结束才放,first_ms≈mt_ms(等整句翻完才进 TTS)。
# 刀=官方 FlushSentinel 车(livekit 1.8.2 say()/TTS 转发路径原生支持:硬段边界
# 逐段独立合成、下段合成与上段播放重叠):首段按「子句边界 或 N 字」切出即
# yield 文本 + FlushSentinel() → MiniMax 立即合成(bidi 攒句被官方哨兵打断,
# 同 A 线 head-flush 语义)。N 缺省 6=A 线 BOK_TTS_FIRST_CHUNK_CHARS 耳测定档;
# 切点铁闸复用 A 线 _first_chunk_cut(数字/拉丁 run 绝不劈,句界就近 N+6 容差
# 内让位);首段语言门软失败=弃早交回落子句路径(不误触发整句回退)。
# kill-switch BOK_INTERP_MT_FIRST_CHUNK_CHARS=0 整档回旧行为。
_MT_FIRST_CHUNK_ENV = "BOK_INTERP_MT_FIRST_CHUNK_CHARS"
_MT_FIRST_CHUNK_DEFAULT = 6


def _interp_mt_first_chunk_chars() -> int:
    """首 chunk 早交字数(纯函数,单测直喂):0=关;坏值回缺省 6(A 线耳测定档)。"""
    raw = os.environ.get(_MT_FIRST_CHUNK_ENV, "")
    try:
        v = int(raw) if raw else _MT_FIRST_CHUNK_DEFAULT
    except ValueError:
        return _MT_FIRST_CHUNK_DEFAULT
    return v if v > 0 else 0


class _MTStreamGateFail(Exception):
    """首子句语言门违约(yield 前抛出=零播报,调用方可安全回退 _mt_once 旧路径)。"""


async def _mt_stream_say(
    session, llm_provider, ctx, *, target_lang: str, tags: bool, t0: float, timeout_s: float = 15.0
) -> dict:
    """流式翻译交付(纯装配层,零 session 依赖面=可假 session 单测)。

    开流→delta 缓冲→子句切割→首子句语言门(E5 启发式:looks_like_language,
    违约在 **yield 前**抛出=零播报)→session.say(async 生成器)逐子句喂出。
    **W0-2 首 chunk 早交**:首段额外按字数硬切(A 线 _first_chunk_cut 铁闸)并
    yield FlushSentinel()——官方硬段边界,MiniMax 立即起合成(下段边翻边合成
    与上段播放重叠);早切段语言门软失败=弃早交回落子句路径(不误回退整句)。
    返回 {full, first_ms, yielded, state}:
    - ``clean``     流自然结束(full=全文);
    - ``gate``      首子句语言门违约(零播报)→调用方回退 _mt_once(保留其
                    chat_retry 强化重试语义);
    - ``error_pre`` 首 yield 前流错误/超时(零播报)→回退;
    - ``error_mid`` 已 yield 后流错误——部分译文已出声,**不回退**(回退=重复
                    播报),full=已出部分,账本照常配对。
    语气标记按子句块换算(_apply_voice_tags 幂等,块内 token 不跨切点=换算完整);
    引号剥除在 provider 层(_StripMTQuoteStream 包流,流式天然继承)。"""
    from livekit.agents import FlushSentinel

    from .providers.livekit_plugins import _first_chunk_cut

    out: dict = {"full": "", "first_ms": 0, "yielded": False, "state": "clean"}
    stream = _mt_open_stream(llm_provider, ctx, retry=False)
    if inspect.isawaitable(stream):
        stream = await stream
    done = asyncio.Event()
    n0 = _interp_mt_first_chunk_chars()

    async def _gen():
        buf = ""
        parts: list[str] = []
        first = True
        deadline = time.monotonic() + timeout_s
        try:
            async for chunk in stream:
                if time.monotonic() > deadline:
                    out["state"] = "error_mid" if out["yielded"] else "error_pre"
                    print(f"[interp] MT_STREAM deadline_s={timeout_s} state={out['state']}", flush=True)
                    break
                delta = getattr(chunk, "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if not content:
                    continue
                buf += content
                # W0-2 首 chunk 早交:无标点也按字数硬切(A 线铁闸),切出即
                # yield + FlushSentinel → 官方硬段边界,合成立即起跑。
                if first and n0 > 0:
                    cut = _first_chunk_cut(buf, n0)
                    if cut:
                        piece, buf = buf[:cut], buf[cut:]
                        if (
                            target_lang
                            and _mt_lang_guard_enabled()
                            and not looks_like_language(piece, target_lang)
                        ):
                            # 软失败:N 字样本语言证据不足,弃早交回落子句路径
                            # (整句语言门照旧在子句/末段处把关,不误回退)。
                            buf = piece + buf
                        else:
                            out["first_ms"] = int((time.perf_counter() - t0) * 1000)
                            first = False
                            out["yielded"] = True
                            emitted = _apply_voice_tags(piece) if tags else piece
                            parts.append(emitted)
                            yield emitted
                            yield FlushSentinel()
                piece, buf = _cut_clause_piece(buf)
                while piece:
                    if first:
                        if (
                            target_lang
                            and _mt_lang_guard_enabled()
                            and not looks_like_language(piece, target_lang)
                        ):
                            out["state"] = "gate"
                            raise _MTStreamGateFail(piece)
                        out["first_ms"] = int((time.perf_counter() - t0) * 1000)
                        first = False
                    out["yielded"] = True
                    emitted = _apply_voice_tags(piece) if tags else piece
                    parts.append(emitted)
                    yield emitted
                    piece, buf = _cut_clause_piece(buf)
            if buf and out["state"] == "clean":
                if first:
                    if (
                        target_lang
                        and _mt_lang_guard_enabled()
                        and not looks_like_language(buf, target_lang)
                    ):
                        out["state"] = "gate"
                        raise _MTStreamGateFail(buf)
                    out["first_ms"] = int((time.perf_counter() - t0) * 1000)
                out["yielded"] = True
                emitted = _apply_voice_tags(buf) if tags else buf
                parts.append(emitted)
                yield emitted
        except _MTStreamGateFail:
            pass  # state 已置 gate;调用方回退(零播报)
        except asyncio.CancelledError:
            out["state"] = "error_mid" if out["yielded"] else "error_pre"
            raise
        except Exception as exc:  # noqa: BLE001 - 流错误:按 yield 前后分类
            out["state"] = "error_mid" if out["yielded"] else "error_pre"
            print(f"[interp] MT_STREAM err state={out['state']} {exc!r}", flush=True)
        finally:
            try:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
            except Exception:  # noqa: BLE001 - 关流尽力而为
                pass
            out["full"] = "".join(parts).strip()
            done.set()

    try:
        session.say(_gen())
    except Exception as exc:  # noqa: BLE001 - say 提交失败(error_pre,回退)
        out["state"] = "error_pre"
        print(f"[interp] MT_STREAM say submit failed {exc!r}", flush=True)
        return out
    try:
        # 等 gen 排干:say 消费端停止拉取(打断/收线)时 finally 也会 set。
        await asyncio.wait_for(done.wait(), timeout=timeout_s + 3.0)
    except asyncio.TimeoutError:
        print("[interp] MT_STREAM gen not drained in time", flush=True)
    return out


async def _mt_once(llm_provider, ctx, *, timeout_s: float = 15.0, target_lang: str = "") -> str:
    """单句直调翻译 LLM(StatelessMTLLM/通用 LLM 同一入口),超时保护防句堆积。

    E5 增补(2026-09-21):``target_lang`` 非空且总闸开时,出口做**确定性语言
    校验**(mt_lang_check.looks_like_language,纯函数)。不像目标语言 → 同句
    **至多一次**强化重试(``chat_retry``,IMPORTANT RETRY 前缀+模板句内约束);
    重试仍错 → **按现状出稿**(绝不回退源文——同传断流比错语言伤害更大,反向
    取舍写明于 plan §26.2-E5)。本函数无循环、重试后不再判,结构性不会累加。

    延迟纪律:重试只在「首次**成功返回**但语言不对」时发生(超时/异常走原车道),
    故额外代价至多一次 wait_for(同款 ``timeout_s``,默认 15s),与单句预算同量级;
    重试流同样 max_retry=1。重试语言仍错时返回值=重试输出(不是原文)。"""
    text = await _mt_collect(_mt_open_stream(llm_provider, ctx, retry=False), timeout_s)
    if not text or not target_lang or not _mt_lang_guard_enabled():
        return text
    if looks_like_language(text, target_lang):
        return text
    if not callable(getattr(llm_provider, "chat_retry", None)):
        # 通用回退 LLM(DeepSeek/主 LLM)无强化重试口:照现状出稿,只留观测行。
        print(
            f"[interp] MT_LANG_MISMATCH target={target_lang} "
            f"score={language_match_score(text, target_lang):.2f} retry=unsupported emit-as-is",
            flush=True,
        )
        return text
    print(
        f"[interp] MT_LANG_MISMATCH target={target_lang} "
        f"score={language_match_score(text, target_lang):.2f} retry=1",
        flush=True,
    )
    try:
        retried = await _mt_collect(_mt_open_stream(llm_provider, ctx, retry=True), timeout_s)
    except asyncio.TimeoutError:
        print(f"[interp] MT_LANG_RETRY_TIMEOUT target={target_lang} emit-as-is", flush=True)
        return text
    if not retried:
        return text
    if not looks_like_language(retried, target_lang):
        print(
            f"[interp] MT_LANG_MISMATCH_RETRY_FAIL target={target_lang} "
            f"score={language_match_score(retried, target_lang):.2f} emit-as-is",
            flush=True,
        )
    return retried


def _mt_fail_line(target_lang: str) -> str:
    """MT 超时兜底台词(纯函数,单测直喂)——按**目标语**出中性提示。

    2026-09-27:MT wait_for(15s) 超时此前只在 _mt_say_worker 里 print,句子既不
    出声也无译文行(21 次实证:15 fwd+6 rev,整通静默 call-4fda36e0)。绝不回放
    源文(同传语义:客户唔应该听到自己讲嘅话),改说目标语短请示句——对方无译文
    可听时至少知道「没听清」。原文行已即时落库,译文行**故意不补**(诚实缺行)。
    未知/空语言键回落 zh。
    """
    lines = {
        "zh": "抱歉，这句没听清，请再说一遍。",
        "cantonese": "唔好意思，呢句聽唔清楚，可唔可以再講一次？",
        "en": "Sorry, I didn't catch that — could you repeat?",
    }
    return lines.get(target_lang, lines["zh"])


def _sidecar_url(cfg_value: str, env_key: str, default: str) -> str:
    """sidecar 地址解析:settings 值 > env > 缺省(去尾部斜杠)。"""
    return (cfg_value or os.environ.get(env_key) or default).rstrip("/")


def _mt_model_valid(p: str) -> bool:
    """MT 模型路径门禁(纯函数,单测直喂):必须是真实存在的本地绝对路径。

    mlx_lm server 收到 repo-id/占位符等非本地路径会当 HF hub id 解析,断网时
    持锁挂死整个 server(B 线同传 2/8 FAIL 实弹,MT :1236 全灭)——非绝对路径
    或不在盘一律 False,装配侧跳过 MT 分支走既有回退链。"""
    path = Path((p or "").strip())
    return path.is_absolute() and path.exists()


def _mt_endpoint_alive(base_url: str, timeout_s: float = 1.5) -> bool:
    """MT 端点装配期探活(RC-2,2026-10-02 刀1):纯同步、绝不 raise。

    判据=「有 HTTP 响应即活」:GET `{base}/models` 拿到任何 HTTP 响应(2xx/3xx,
    或 401/404/5xx 等 HTTPError)=端点在场;连接错误/超时/协议错误=死。比
    control_plane/model_routing_detect.probe_endpoint 更宽(那处 401 算活、404
    不算)——本探针要拦的是「server 根本没起」(连接拒绝/超时)这类整通死亡,
    有服务在听即不该跳 MT;/models 未实现返回 404 也证明有进程在。装配点是同步
    函数,这里用 urllib(零新依赖);探活是数据不是异常,失败一律 False。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return False
    import http.client
    import urllib.parse

    try:
        # 无鉴权头:401/404 与成功响应同判「端点在场」。
        # 出站闸（http.client 直连——urlopen sink 形状与探针族共存不了，
        # tools/bok.py _http_call 同款实证）：scheme/host/userinfo 就地校验。
        parts = urllib.parse.urlsplit(f"{base}/models")
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
            return False
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = cls(host, parts.port or (443 if parts.scheme == "https" else 80), timeout=timeout_s)
        try:
            req_path = parts.path or "/"
            if parts.query:
                req_path = f"{req_path}?{parts.query}"
            conn.request("GET", req_path)
            conn.getresponse().read()
            return True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - URLError/超时/坏协议=死，绝不外抛
        return False


def _mt_sampling(env_key: str, mt_default: float) -> float:
    """MT 采样档解析(单测直喂):用户显式 env 优先,缺省/非法回落 MT 推荐值。

    旧版用 os.environ.setdefault 下发采样档再由 MlxLlmLLM 构造时读回——同
    worker 先服务过 MT 有效会话后 env 永久驻留,后续会话 MT 失效落回主 LLM
    会带着 MT 采样档跑(主 LLM 期望 0.35,评审 P2-3 跨会话 env 泄漏)。现只在
    构造参数处解析,唔写回进程 env。"""
    raw = (os.environ.get(env_key) or "").strip()
    try:
        v = float(raw) if raw else mt_default
    except ValueError:
        return mt_default
    # inf/nan/1e400 过得了 float() 但会炸 int(top_k) 或序列化成 Infinity/NaN
    # ——非有限值一律当非法档回落推荐值（评审：原实现可穿透,装配期崩整条 job）。
    return v if math.isfinite(v) else mt_default


def _build_llm_provider(
    llm_cfg: dict, target_lang: str, glossary: str = "", routing_raw: str = "", mt_alive=None
):
    """组装 B 线翻译 LLM:MT 小模型(:1236)优先,回退 DeepSeek 云端 / 主 LLM(:1235)。

    MT 分支按官方 Hy-MT2 推荐采样收窄,MlxLlmLLM 构造时显式传参(用户显式 env
    优先、唔写回进程 env,防跨会话泄漏——见 _mt_sampling);StatelessMTLLM 负责
    逐句无状态模板化,glossary 非空时进 _mt_prompt 术语槽(会话级常量,前缀稳定)。
    回退开关 = unset MT_LLM_BASE_URL,老 DeepSeek/主 LLM 路径原样保留(术语一致
    性由 instructions 的 glossary 行兜)。MT_LLM_MODEL 须经 _mt_model_valid(本地
    绝对路径且在盘)才进 MT 分支——非法值原样透传会让 mlx_lm server 挂死,跳过
    MT 走回退链 + 日志留值。

    装配期 MT 探活(RC-2,2026-10-02 刀1):本地 MT 分支只验「模型在盘」不验
    「server 活着」——:1236 死(模型在盘但 server 挂/prod 单元不含它)则整通每句
    走异常兜底。现分支选定后 GET `{base}/models` 1.5s 探活(_mt_endpoint_alive),
    死→日志一行 + 落既有回退链(绝不把整通押在死端点);`BOK_INTERP_MT_PROBE=0`
    回旧行为(信任配置),`mt_alive` 注入口供单测免网络。云端 openai 档不经此闸
    (路由表显式配置的云端点,探活只剩装配延迟)。

    `routing_raw`＝当通 CP 设置顶层 `model_routing_json` 原始串(2026-09-25 阶段 0):
    mt 车道 openai 档整体改走云端(本地路径门禁不适用——云端模型 id 非 mlx 路径);
    local 档显式改端点时换 base_url/model 后仍走既有门禁与回退链(挂死防线不绕);
    缺省 ""(未配置/kill-switch)＝env 档,既有读法逐字节(零漂移保证)。参数随当通
    会话传入,worker 并发多通不串线(禁模块级可变全局)。

    max_tokens:整句翻译放宽到 512(默认 160 是客服短句口径,长句会截断)——经
    构造参数显式下发,不再靠 entrypoint `os.environ.setdefault` 写进程 env
    (常驻 worker 跨会话驻留,刀1 卫生)。
    """
    from .providers.livekit_plugins import (
        DeepSeekLLM,
        MlxLlmLLM,
        StatelessMTLLM,
        route_llm_kwargs,
    )

    mt_route = resolve_route("mt", os.environ, routing_raw)
    if mt_route.provider == PROVIDER_OPENAI:
        # 云端 MT 指令化(2026-10-08 Wave 1,用户拍板「MT 全量切 DeepSeek 试」):
        # 云端大模型**吃指令**——不再包 StatelessMTLLM 模板(它只取最后一条 user 套
        # _mt_prompt,system 指令/滚动对全丢=纠错能力被锁死,subagent 调研实证)。
        # 直接 MlxLlmLLM 吃完整 ctx(_build_mt_context 组装:system=
        # _translation_instructions 含「ASR 同音误听结合上下文纠错」规则 + 术语行
        # + 滚动对 + 当前句)=回退档既有形状。kill-switch
        # BOK_INTERP_MT_CLOUD_INSTRUCT=0 回旧模板包裹(试验逃生口);本地 Hy-MT2
        # 档逐字节零漂移(对模板外指示无视=实测定案,指令化只对云端有意义)。
        # 采样档照旧(Hy-MT2 推荐档对翻译任务同样适用:贴原文、窄采样防自由发挥)。
        cloud_instruct = os.environ.get("BOK_INTERP_MT_CLOUD_INSTRUCT", "1") == "1"
        if cloud_instruct:
            # 滚动对经 _build_mt_context 全量进 ctx(_mt_pairs=deque maxlen 8,回退档
            # 同款);BOK_INTERP_MT_CONTEXT 只管旧模板包裹的参考段抽取。
            print(f"[interp] llm=mt-cloud-instructed base={mt_route.base_url}", flush=True)
            return MlxLlmLLM(
                base_url=mt_route.base_url,
                model=mt_route.model,
                api_key=mt_route.api_key or "mlx",
                enable_thinking=mt_route.enable_thinking,
                temperature=_mt_sampling("LLM_TEMPERATURE", 0.7),
                top_p=_mt_sampling("LLM_TOP_P", 0.6),
                top_k=int(_mt_sampling("LLM_TOP_K", 20)),
                repetition_penalty=_mt_sampling("LLM_REPETITION_PENALTY", 1.05),
                max_tokens=512,
            )
        print(f"[interp] llm=mt-cloud base={mt_route.base_url}", flush=True)
        context_turns = int(os.environ.get("BOK_INTERP_MT_CONTEXT", "0") or 0)
        return StatelessMTLLM(
            MlxLlmLLM(
                base_url=mt_route.base_url,
                model=mt_route.model,
                api_key=mt_route.api_key or "mlx",
                enable_thinking=mt_route.enable_thinking,
                temperature=_mt_sampling("LLM_TEMPERATURE", 0.7),
                top_p=_mt_sampling("LLM_TOP_P", 0.6),
                top_k=int(_mt_sampling("LLM_TOP_K", 20)),
                repetition_penalty=_mt_sampling("LLM_REPETITION_PENALTY", 1.05),
                max_tokens=512,
            ),
            target_lang,
            glossary=glossary,
            context_turns=context_turns,
        )

    if mt_route.source == "routing":
        # local 档显式改端点(如 LM Studio :1234):路由表只换「去哪/用哪个模型」,
        # _mt_model_valid 挂死防线与回退链照走(model 非空才覆盖)。
        mt_base = mt_route.base_url
        mt_model = mt_route.model or os.environ.get("MT_LLM_MODEL", "").strip()
    else:
        mt_base = os.environ.get("MT_LLM_BASE_URL", "").strip()
        mt_model = os.environ.get("MT_LLM_MODEL", "").strip()
    _mt_probe_dead = False
    if mt_base and _mt_model_valid(mt_model):
        if os.environ.get("BOK_INTERP_MT_PROBE", "1") == "1":
            _probe = mt_alive or _mt_endpoint_alive
            if not _probe(mt_base):
                _mt_probe_dead = True
                print(f"[interp] mt endpoint dead ({mt_base}) — fallback chain", flush=True)
        if not _mt_probe_dead:
            # Hy-MT2 官方推荐采样:temperature 0.7 / top_p 0.6 / top_k 20 / 重复惩罚
            # 1.05——翻译要贴原文,采样收窄防小模型自由发挥/复读。经构造参数显式下发
            # (用户显式 env 优先),唔再用 env setdefault——那会在同 worker 跨会话驻留,
            # MT 失效落回主 LLM 时采样档跟着泄漏(评审 P2-3)。
            print(f"[interp] llm=hy-mt2 base={mt_base}", flush=True)
            # 滚动上下文(默认 0=关,治代词/指代断裂的 A/B 档):非零=带最近 N 对
            # 「源→译」进 MT prompt 上文参考块(LLMA 式)。代价=参考段逐轮位移,
            # prefix 从该段失效(术语槽/模板头仍命中)——延迟影响用
            # scripts/probes/probe_interpret_latency.py 实测后再定默认。
            context_turns = int(os.environ.get("BOK_INTERP_MT_CONTEXT", "0") or 0)
            return StatelessMTLLM(
                MlxLlmLLM(
                    base_url=mt_base,
                    model=mt_model,
                    temperature=_mt_sampling("LLM_TEMPERATURE", 0.7),
                    top_p=_mt_sampling("LLM_TOP_P", 0.6),
                    top_k=int(_mt_sampling("LLM_TOP_K", 20)),
                    repetition_penalty=_mt_sampling("LLM_REPETITION_PENALTY", 1.05),
                    max_tokens=512,
                ),
                target_lang,
                glossary=glossary,
                context_turns=context_turns,
            )

    if mt_base and not _mt_probe_dead:
        # 挂死防线:base 有值但 model 非法(repo-id/占位符/空)——跳过 MT 走既有
        # 回退链(DeepSeek/主 LLM 原逻辑不动),日志留值方便查 env(超 60 字截断)。
        # 探活死已单独打过日志,不重复报「model invalid」误导排障。
        shown = mt_model[:60] + ("…" if len(mt_model) > 60 else "")
        print(f"[interp] mt model invalid ('{shown}') — fallback (deepseek/main LLM 链)", flush=True)

    if (llm_cfg.get("provider") or "local_openai") == "deepseek" and (
        llm_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
    ):
        return DeepSeekLLM(
            api_key=llm_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", ""),
            model=llm_cfg.get("model") or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            base_url=llm_cfg.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            max_tokens=512,
        )
    # B 线回复/兜底车道路由(a_reply,2026-09-25):env 档=下方既有读法原样传参
    # (零漂移);openai 档=路由表下发端点/模型/密钥+思考旗(local routing 档只换
    # 端点/模型)。设置卡 deepseek 带密钥显式云档不属本车道覆盖面,原样保留。
    return MlxLlmLLM(
        **route_llm_kwargs(
            resolve_route("a_reply", os.environ, routing_raw),
            env_base_url=llm_cfg.get("base_url")
            or os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
            cfg_model=llm_cfg.get("model") or os.environ.get("MLX_LLM_MODEL", ""),
        ),
        max_tokens=512,
    )


# 与 A 线 agent.py _MINIMAX_LOCAL_VOICES 同源（复制不 import：agent 模块装配重，
# interpret 单测要保持轻导入）。设置页/同传页误配本地 Qwen3 音色（预设 9 个 +
# 克隆 agent-*/acceptance-*）发给云端 MiniMax 会 2054 voice not exist，逐句 beep。
_MINIMAX_LOCAL_VOICES = frozenset({
    "serena", "vivian", "uncle_fu", "ryan", "aiden", "ono_anna", "sohee", "eric", "dylan",
})
_MINIMAX_LOCAL_PREFIXES = ("agent-", "acceptance-")


def _cloud_voice(vid: str) -> str:
    """云端 MiniMax 可用音色过滤（纯函数，单测直喂）：本地 Qwen3 音色返回空串，
    有效云端音色原样返回（trim 后）。"""
    raw = (vid or "").strip()
    base = raw.lower()
    if not base:
        return ""
    if base in _MINIMAX_LOCAL_VOICES or base.startswith(_MINIMAX_LOCAL_PREFIXES):
        return ""
    return raw


def _parse_session_voices(raw) -> dict:
    """会话级音色 map 解析（纯函数，单测直喂）：同传页建单 voices_json 经 CP
    dispatch metadata 下发，`{"zh": "voice_id", ...}` → 归一 {lang: voice_id}。

    坏 JSON / 形状不对 / 空值 / 未知语言键 → 丢弃 + 日志，返回空 dict——配置
    错误绝不能打死通话，全链回落设置三键 > 硬编码默认。键经 _norm_lang 归一
    成 zh/cantonese/en 三态。"""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[interp] session voices parse failed: {exc!r} — ignore", flush=True)
        return {}
    if not isinstance(data, dict):
        print(f"[interp] session voices not an object: {type(data).__name__} — ignore", flush=True)
        return {}
    voices: dict = {}
    for key, val in data.items():
        lang = _norm_lang(str(key), default="")
        vid = str(val or "").strip()
        if lang and vid:
            voices[lang] = vid
    return voices


def _build_local_qwen3_tts(tts_cfg: dict, target_lang: str, tts_ls):
    """本地 Qwen3-TTS 构造(纯装配,单测直喂)。

    从 _build_tts_provider 本地分支提取(2026-09-27 B 线 TTS 硬失败兜底):现在两处
    复用同一构造——①provider=qwen3_tts 的纯本地方向;②MiniMax 主档失败时的
    FallbackAdapter 备档(官方 A 线同款姿势)。音色优先级=设置页全局单音色 speaker
    > 分语言 speaker_zh/cantonese/en,未配时 Qwen3TTSTTS 内部按语言档回落。
    """
    from .providers.livekit_plugins import Qwen3TTSTTS

    voice = str(tts_cfg.get("speaker") or "").strip()
    if not voice:
        keymap = {"zh": "speaker_zh", "cantonese": "speaker_cantonese", "en": "speaker_en"}
        voice = str(tts_cfg.get(keymap.get(target_lang, "speaker_zh")) or "")
    return Qwen3TTSTTS(
        base_url=_sidecar_url(tts_cfg.get("base_url") or "", "QWEN3_TTS_BASE_URL", "http://127.0.0.1:8788"),
        voice=voice,
        language_state=tts_ls,
        sample_rate=int(tts_cfg.get("sample_rate") or 24000),
    )


def _build_tts_provider(tts_cfg: dict, target_lang: str, session_voices=None, persona_voices=None):
    """组装 B 线 TTS:settings 指定 minimax → 云端 MiniMax;否则本地 Qwen3-TTS 兜底。

    音色锁口音——粤语音色读普/英自然,普通话音色读粤文变广普,故按 target_lang
    三键换音色;音色优先级=会话级(同传页建单选定,session_voices)>**人设层**
    (建单绑定的 persona.reference_audio,B 线人设音色复用 2026-10-08,A 线
    parse_voice_map 同源解析)>设置页三键>硬编码默认。B 线默认 turbo 档
    (agent 场景 <250ms、$60/M),A 线仍 2.8-hd。

    MiniMax 档回退(2026-09-27):云端 SSL 校验失败/「AudioEmitter isn't started」
    bidi 错误实测令整通零译文出声(call-b347f691:8 源句 0 译文,纯静音)。现按 A
    线同款官方 FallbackAdapter 姿势包裹——主档云端 MiniMax、备档本地 Qwen3-TTS
    (A 线是 hd→turbo 同云换档保音色;B 线跨供应商换声可接受,出声 > 静音)。
    `BOK_LOCAL_TTS=0`(bok.py 明确跳过 :8788 的语义)时不装备档,返回裸主档;
    本地 sidecar 未跑时 adapter 逐请求穿透失败,行为等价单实例。
    """
    from .providers.livekit_plugins import LanguageState, MiniMaxTTS

    tts_ls = LanguageState()
    tts_ls.lang = target_lang
    provider = (tts_cfg.get("provider") or "qwen3_tts").lower()
    if provider in ("minimax", "minimax_streaming"):
        # 防御与 A 线 agent.py 同源:设置页误选本地 Qwen3 音色发给云端会 2054。
        keymap = {"zh": "speaker_zh", "cantonese": "speaker_cantonese", "en": "speaker_en"}
        voice_map = {}
        for lang, key in keymap.items():
            vid = _cloud_voice(str(tts_cfg.get(key) or ""))
            if vid:
                voice_map[lang] = vid

        # 人设层（2026-10-08 B 线人设音色复用）：覆盖设置三键（会话级仍在最上）。
        # 本地 Qwen3 音色 ID 同样过滤（误配防 2054）；人设 map 只有三语键，
        # 四语目标天然缺席=回落下层。
        if isinstance(persona_voices, str):
            persona_voices = parse_voice_map(persona_voices)
        for lang_raw, vid_raw in (persona_voices or {}).items():
            lang = _norm_lang(str(lang_raw), default="")
            if lang not in keymap:
                continue
            vid = _cloud_voice(str(vid_raw or ""))
            if not vid:
                continue
            if voice_map.get(lang) != vid:
                print(f"[interp] persona voice {lang}: {voice_map.get(lang) or '(default)'} -> {vid}", flush=True)
            voice_map[lang] = vid
        # 会话级音色最优先,覆盖人设/设置;误配本地音色同样过滤 → 回落人设/设置/默认。
        if isinstance(session_voices, str):
            session_voices = _parse_session_voices(session_voices)
        for lang_raw, vid_raw in (session_voices or {}).items():
            lang = _norm_lang(str(lang_raw), default="")
            if not lang:
                continue
            vid = str(vid_raw or "").strip()
            cloud = _cloud_voice(vid)
            if not cloud:
                if vid:
                    print(f"[interp] minimax skip local qwen3 session voice {vid!r} for {lang}", flush=True)
                continue
            if voice_map.get(lang) != cloud:
                print(f"[interp] session voice {lang}: {voice_map.get(lang) or '(default)'} -> {cloud}", flush=True)
            voice_map[lang] = cloud
        # 设置页/会话级都没配的分语言音色用验证过的默认(各语种母语音色,口音不串)。
        voice_map.setdefault("zh", "Chinese (Mandarin)_News_Anchor")
        voice_map.setdefault("cantonese", "Cantonese_crisp_news_anchor_vv2")
        # EN 默认音色 2026-09-07 换:旧 male_english_speaker 已被 MiniMax 下线
        # （每轮 2054 voice id not exist → 目标侧整轮静音,B 线 E2E 实证）,
        # 换成 minimax-voices.ts 里 preview 验证过的 English_magnetic_voiced_man。
        voice_map.setdefault("en", "English_magnetic_voiced_man")
        # B 线默认 2026-09-16 起 2.8-turbo(原 2.6-turbo):语气词标记 (laughs)/
        # (coughs)/(sighs) 仅 2.8 系支持——真人感需求拍板上 2.8,实测代价 ~0.2s
        # 感知 lag(2532→2721ms,预算 3500 内);要快可 MINIMAX_MODEL=speech-2.6-
        # turbo 回退(标记自动熄火)或 BOK_INTERP_VOICE_TAGS=0 只关标记。合成档
        # 经 model_override 构造下发(_resolve_minimax_model 读 env 不写)——旧
        # setdefault 写进程 env,常驻 worker 跨会话驻留(评审 follow-up,与采样
        # 档 P2-3 同治理)。
        # language_boost 锁目标语,防源语音夹词时合成语种漂移;值是 MiniMax API
        # 的外部枚举字面量(术语门禁白名单单点),唔系语言字段命名。同经构造参数
        # 下发:同 worker 先 zh 后 en 的会话,旧 setdefault 会让 boost 停在首通
        # 的值(合成语种漂移),现逐会话解析零驻留。
        boost_map = {
            "zh": "Chinese",
            "cantonese": "Chinese,Yue",
            "en": "English",
            # 2026-10-06 W2 四语（MiniMax boost 官方枚举内：German/French/Japanese/
            # Portuguese）；前提=_norm_lang 已收 de/fr/ja/pt（否则到不了这里）。
            "de": "German",
            "fr": "French",
            "ja": "Japanese",
            "pt": "Portuguese",
        }
        boost = boost_map.get(target_lang, "")
        tts = MiniMaxTTS(
            voice=voice_map,
            language_state=tts_ls,
            sample_rate=int(tts_cfg.get("sample_rate") or 24000),
            api_key=str(tts_cfg.get("api_key") or ""),
            model_override=_resolve_minimax_model(),
            language_boost=boost or None,
        )
        # keep-warm 预连(同 A 线):无事件循环时静默跳过,失败零影响。
        # 2026-09-28 prewarm async 化(W-TTS):协程挂当前 loop 后台跑;
        # 无 loop(历史同步装配形态)时走 tts_cache 的同步兼容垫语义=关闭跳过。
        try:
            _pw = tts.prewarm()
            if asyncio.iscoroutine(_pw):
                try:
                    # FIRE_FORGET_EXEMPT: 预热纯增益——被 GC 掐掉=首段译句就地握手回退。
                    asyncio.get_running_loop().create_task(_pw)
                except RuntimeError:
                    _pw.close()
        except Exception:  # noqa: BLE001
            pass
        # TTS 硬失败兜底(2026-09-27):主档云端 MiniMax 单点失败曾令整通零译文出声。
        # A 线同款官方 FallbackAdapter,备档=本地 Qwen3-TTS;`BOK_LOCAL_TTS=0`
        # 显式跳过本地 TTS 时保持单实例(bok.py 同键语义)。装配失败零影响(裸主档)。
        if os.environ.get("BOK_LOCAL_TTS", "") != "0":
            try:
                from livekit.agents import tts as agents_tts  # noqa: F401 - 形状自检

                from .providers.livekit_plugins import PrewarmFallbackTTS

                backup = _build_local_qwen3_tts(tts_cfg, target_lang, tts_ls)
                # PrewarmFallbackTTS:官方 prewarm 同步约定 × W-TTS async prewarm 兼容垫。
                wrapped = PrewarmFallbackTTS([tts, backup])
                print("[interp] tts fallback armed primary=minimax backup=qwen3_local", flush=True)
                return wrapped
            except Exception as exc:  # noqa: BLE001 - 回退装配失败就用单实例
                print(f"[interp] tts fallback init failed, single instance: {exc!r}", flush=True)
        return tts
    # 本地 Qwen3-TTS 兜底(离线可用):设置页全局单音色 speaker 优先,否则分语言。
    return _build_local_qwen3_tts(tts_cfg, target_lang, tts_ls)


def _preemptive_generation_opts() -> dict:
    """抢跑（preemptive generation）预算（单测直接喂 env 断言，唔使起 worker）。

    max_retries 默认 3（P1 churn 收敛，旧 8 会把误判轮的 prefill 白烧放大）：
    每个 PREFLIGHT 事件 count+1，烧穿后 FINAL 到达只 cancel 不重建 → 译文从零
    生成。PREEMPTIVE_MAX_RETRIES 可回退。
    """
    return {
        "enabled": True,
        "preemptive_tts": False,
        "max_speech_duration": 10.0,
        "max_retries": int(os.environ.get("PREEMPTIVE_MAX_RETRIES", "3")),
    }


def _direction_audio_enabled(speaker_role: str) -> bool:
    """该方向译文是否合成+发布音频(纯函数,单测用)。

    2026-10-08 用户翻案(拍板原话:「对方说英文 我要听到英文转普通话的翻译! 我讲
    普通话对方听到英文的翻译!」)——同传台**双向出声**:rev(对方→我)译文合成 TTS
    进我方耳机(译员耳语),fwd(我→对方)不变。旧 2026-09-12「单向化」拍板(rev
    纯字幕零 TTS,省一半 MiniMax 合成)作废。``BOK_INTERP_REV_AUDIO=0`` 回退
    单向化档(字幕仍全量;逃生口留给纯字幕同传/合成预算敏感场景)。"""
    if speaker_role != "other":
        return True
    return os.environ.get("BOK_INTERP_REV_AUDIO", "1") != "0"


def _session_report_payload(report_dict: dict) -> dict:
    """SessionReport dict + 来源 worker 标识(纯函数,单测直喂)。

    P1-A(2026-09-17 全量 debug):fwd/rev 双 worker 共享同一 call_id、各自产一份
    真实 usage 的 SessionReport,后收尾者曾撞 CP ghost guard 409(报告静默丢失,
    interp-fwd.log 2026-09-16 call-c7a63c17 实证)。CP 端按 worker 维度收多份,
    body 带 worker 字段区分来源;不带 worker 的旧格式(A 线 _close)走原单报告
    ghost guard 语义,行为不变。深拷贝入 payload,绝不 mutate caller 的 dict。
    """
    payload = dict(report_dict)
    payload["worker"] = f"bok-interp-{os.environ.get('INTERP_DIRECTION', 'fwd')}"
    return payload


def _spawn_pooled_task(coro, pool: set, err_tag: str) -> None:
    """fire-and-forget 但入池(2026-09-17 全量 debug P2-A 的通用机制,单测直喂)。

    事件循环对 task 只持弱引用,GC 中途回收=任务静默丢失(B 线纪要账本行丢失
    不可补)。入强引用池 + done-callback 自清(防池无界增长)+ 失败打点 err_tag;
    纯引用+打点,不补重试、不改异常语义。镜像 A 线 agent.py `_spawn_report`。
    """
    task = asyncio.create_task(coro)
    pool.add(task)

    def _done(t: asyncio.Task) -> None:
        pool.discard(t)
        if not t.cancelled() and t.exception() is not None:
            print(f"{err_tag} {t.exception()!r}", flush=True)

    task.add_done_callback(_done)


def _estimate_speech_seconds(text: str, target_lang: str) -> float:
    """粗估译文播报时长(秒,纯函数,单测直喂)。

    zh/cantonese 按字(去标点;MiniMax speed 1.2 ≈ 5 字/秒),en 按词(≈2.6 词/秒)。
    估算只喂背压门槛不求精确;非空地板 0.8s(TTS 起播+段间开销)。"""
    if not text:
        return 0.0
    if target_lang == "en":
        return max(0.8, len(text.split()) / 2.6)
    stripped = "".join(ch for ch in text if ch.isalnum())
    return max(0.8, len(stripped) / 5.0)


class _PlaybackBacklog:
    """译文播放背压——v1 PlaybackScheduler 的 maxBacklogMs 门移植(2026-09-16 P2)。

    句级提交+打断默认关之后,源语语速 > 译文播报速度时,译文在框架 speech 队列
    无限堆积,体感 lag 变雪球(字幕早出声、译文越拖越远)。策略=「追最新」
    (AlignAtt 的 always-attend-latest 哲学,同传译员摘译的工程化形态):估时总量
    超限时从最旧开始 force 中断**未开播**的译文句;队头(当前播报)与最新一条
    永不弃——弃音保字,已生成文本的 chat item 照常落库/进字幕。生成中被取消的
    句=整句摘掉(译员压力下的跳句形态)。

    门槛语义 v2(2026-09-23 修复波#2,task-9 §14 实证):①**队头不计入门槛**——
    队头=正在播报的沉没成本,旧算法把它全额计入,单句译文即可「超门」但 depth<3
    结构性弃不了,门槛日志恒饱和假警(est_ms=1600 drop=0);改计「等待积压」后
    depth<3 不弃仍正确(唯一候选=最新一条,追最新保护),假警消失。②**源句队列
    计入门槛、成为摘译候选**——MT 未消化的待译源句也是听感积压;等待积压超门
    且 say 队无弃句候选时,按次摘译(arm)最旧待译源句一条(原文行已落库=摘译
    保文,与 _src_q 溢出摘译同语义),由 MT worker 取句时消费跳过。每次评估至多
    arm 一条(下轮再评估),压力阀步进不连跳。真实战场=源语连珠炮时先弃旧译文
    再摘旧源句,最新内容必达。

    `BOK_INTERP_BACKLOG=0` 总闸关;`BOK_INTERP_MAX_BACKLOG_S` 调门槛(默认 6s
    ≈通路 lag 2.5s+一句余量)。只在 speech_created 事件判定——队列只在新建句时
    增长;interrupt 必须 force=True(会话打断默认关,非 force 会 RuntimeError)。
    """

    def __init__(self, target_lang: str, source_backlog_s=None, room: str = ""):
        self._target_lang = target_lang
        self._room = room  # 仅作 BACKLOG_DROP 观测行(无 DB 句柄,不写库)
        try:
            self._max_s = float(os.environ.get("BOK_INTERP_MAX_BACKLOG_S", "6") or 6)
        except ValueError:
            self._max_s = 6.0
        self._pending: list[tuple[object, float]] = []  # [(handle, 估时秒)]
        self._source_backlog_s = source_backlog_s  # () -> float:待译源句估时总量
        self.source_drops_pending = 0  # 摘译指令(MT worker 取句时消费)
        self.dropped = 0
        self.dropped_est_s = 0.0

    @property
    def enabled(self) -> bool:
        return os.environ.get("BOK_INTERP_BACKLOG", "1") == "1" and self._max_s > 0

    def set_source_backlog(self, est_s: float) -> None:
        """测试注入口:直接设定源队列估时(生产走 source_backlog_s 回调)。"""
        self._source_backlog_s = lambda: est_s  # type: ignore[assignment]

    def _src_backlog_s(self) -> float:
        try:
            return max(0.0, float(self._source_backlog_s() or 0)) if self._source_backlog_s else 0.0
        except Exception:
            return 0.0

    def take_source_drops(self) -> int:
        """MT worker 取句时消费摘译指令(一次性取走当前计数)。"""
        n = self.source_drops_pending
        self.source_drops_pending = 0
        return n

    def _text_of(self, handle) -> str:
        texts = []
        for item in getattr(handle, "chat_items", None) or []:
            t = getattr(item, "text_content", None) or getattr(item, "raw_text_content", "")
            if t:
                texts.append(str(t))
        return " ".join(texts)

    def _est_of(self, handle) -> float:
        return _estimate_speech_seconds(self._text_of(handle), self._target_lang)

    def on_speech_created(self, handle) -> tuple[int, float, int]:
        """登记新译文句并按门槛弃旧。返回 (队列深度, 等待积压估时秒, 本轮弃句数)。

        est=等待积压(不含在播队头)+待译源队列——门槛量的係「还要多久才轮到最新
        内容」,不是已播了多少。"""
        if not self.enabled:
            return (0, 0.0, 0)
        # 1) 清账:已播完/已取消的句出队(text-only 方向句句秒完,队列天然不积)。
        self._pending = [(h, e) for (h, e) in self._pending if not h.done()]
        # 2) 估时:chat item 尚未生成的按旧值/地板记,下轮决策自动补准。
        refreshed = [(h, self._est_of(h) or e) for (h, e) in self._pending]
        est_new = self._est_of(handle) or 0.8  # 新句此刻多半还没有 chat item
        refreshed.append((handle, est_new))
        self._pending = refreshed
        # 3) 门槛(等待积压口径,index 0=在播队头不计):队头永不弃、最新一条永不弃
        #    → 只从 index 1 起弃;仍超门且 say 队无候选 → arm 一条源句摘译。
        total = sum(e for _, e in self._pending[1:])
        dropped_now = 0
        while total > self._max_s and len(self._pending) > 2:
            h, e = self._pending.pop(1)
            try:
                h.interrupt(force=True)
            except RuntimeError:  # 已完成/已取消——账面照扣
                pass
            total -= e
            dropped_now += 1
            self.dropped += 1
            self.dropped_est_s += e
            # 弃句观测(2026-09-27):弃音保字之下,被弃句在 DB 与已播句不可分——本行
            # 是唯一区分点。成本零(仅真弃时打印;无 DB 句柄,绝不写库)。
            print(
                f"[interp] BACKLOG_DROP room={self._room} lang={self._target_lang} "
                f"chars={len(self._text_of(h))}",
                flush=True,
            )
        if total + self._src_backlog_s() > self._max_s and self._src_backlog_s() > 0:
            self.source_drops_pending += 1  # 摘译压力阀:每次评估至多一条
        return len(self._pending), total, dropped_now


def _mt_consume_skip(backlog: "_PlaybackBacklog", text: str) -> bool:
    """MT worker 取句后先消费摘译指令（True=跳过本句 MT+播报）。

    模块级便于单测钉消费契约（fix round 1 评审 Minor-6）：积压门 arm 的摘译在
    取句时刻生效——被摘句的原文行已即时落库（`_on_user_input`），跳过的只是
    译文生成与出声（摘译保文，与 _src_q 溢出摘译同语义）。消费一次性。"""
    skipped = backlog.take_source_drops()
    if skipped:
        print(
            f"[interp] INTERP_BACKLOG source-skip x{skipped} "
            f"({len(text)} chars, backlog gate 摘译保文)",
            flush=True,
        )
        return True
    return False


class _LagLedger:
    """B 线感知延迟账本(RC-8,2026-10-02 刀1):源句 final → 译文交付的 FIFO 配对。

    口径镜像 A 线(agent.py `_on_conversation_item`/`_report_assistant_turn`):
    started_ms/ended_ms=通话相对毫秒(单调钟减当通基线 `_t0`),perceived_ms=源句
    讲完→译文 item 落地(入账时刻)的墙钟毫秒。**代理口径**:B 线无 eou/tts 分段,
    且框架在整句播报完成才发 conversation_item_added——perceived 是整链上界
    (MT+合成+播报),不是首声北极星;生产 grep INTERP_LAG 行可得真实分布。

    两条 FIFO 天然同序:源句按序 `note_src`(`_on_user_input` 句级 final,确认入队
    成功才记),MT 完成按序 `done_mt`(弹 src 头进 pending),译文 item 按序
    `pop_pending`。未产出译文的句(积压摘译/MT 空/超时/异常)必须 `drop_src` 消费
    src 头,否则后续句全部错配。`clock` 注入口供单测免真钟。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._src: deque = deque()      # [(t_src, src_chars)]
        self._pending: deque = deque()  # [(t_src, src_chars, mt_ms)]

    def note_src(self, text: str) -> None:
        """源句 final 记账(句子确认进入 MT 队列后调用,防溢出摘句错配)。"""
        self._src.append((float(self._clock()), len(str(text or ""))))

    def drop_src(self) -> None:
        """消费 src 头但不产出译文(摘译/空译文/MT 失败):保后续句配对对齐。"""
        if self._src:
            self._src.popleft()

    def done_mt(self, mt_ms: int) -> None:
        """该句 MT 完成且译文非空(say 前):src 头配对进 pending。"""
        if not self._src:
            return
        t_src, src_chars = self._src.popleft()
        self._pending.append((t_src, src_chars, int(mt_ms or 0)))

    def pop_pending(self) -> tuple[float, int, int] | None:
        """译文 item 落地时取配对头;无配对(兜底句/异常轮)=None,不落时间列。"""
        if not self._pending:
            return None
        return self._pending.popleft()


def _lag_turn_timing(rec: tuple[float, int, int], now: float, t0: float) -> tuple[int, int, int]:
    """账本配对 → add_turn 三列(纯函数,单测直喂):镜像 A 线口径。

    started_ms/ended_ms=通话相对毫秒(单调钟减当通基线),perceived_ms=源句 final→
    译文 item 落地的墙钟毫秒(代理口径,见 _LagLedger docstring)。"""
    t_src, _src_chars, _mt_ms = rec
    return (
        int((t_src - t0) * 1000),
        int((now - t0) * 1000),
        int((now - t_src) * 1000),
    )


# —— B 线碎片闸(Wave 3a,2026-10-08「B 线 A 线化」) -----------------------------
# 豆包云档 FINAL 只按 VAD 静音切分(min_silence 0.28s):「啊。」「No.」「你。」级
# 纯应承碎片占 finals ~30%(2026-10-08 段级审计),每片独烧一条 ASR-final→MT 往返
# →TTS 合成→播报 全链,FIFO 串行下尾部 perceived 尖刺(「译文：No.」5.3s 实证)。
# 闸语义=**hold-and-merge,绝不丢**(同传保真):worker 取到纯应承碎片时 hold
# ≤BOK_INTERP_FRAG_HOLD_S(缺省 0.6s)等下一段——到=并成一句一次翻译(账本
# drop_src 补偿保配对对齐);超时=照旧单独出译(纯语气句代价=该片 +hold 延迟,
# INTERP_FRAG flush 行可观测)。碎片判据=剥标点后逐字纯应承(复用 A 线
# _PURE_ACK_TAIL_CHARS+人称代词/英文应承词)——数字 run/任何实词永不闸(内容必译)。
# kill-switch BOK_INTERP_FRAG_MERGE=0 旧路径逐字节(不 hold 不并,零额外 await);
# 窗口 BOK_INTERP_FRAG_HOLD_S 可调(坏值回缺省 0.6,负数钳 0,上限 2.0)。
_FRAG_MERGE_ENV = "BOK_INTERP_FRAG_MERGE"
_FRAG_HOLD_ENV = "BOK_INTERP_FRAG_HOLD_S"
_FRAG_HOLD_DEFAULT_S = 0.6
_FRAG_HOLD_MAX_S = 2.0
_FRAG_MAX_ABSORB = 3  # 单次 hold 最多并入段数(链式碎片帽)
_FRAG_MAX_CHARS = 80  # 并后总字数帽(超长不并)
_INTERP_FRAG_ACK_EXTRA = frozenset("你我他她它您诶")  # 人称单字(「你。」)
_INTERP_FRAG_ACK_WORDS = frozenset(
    {
        "no", "yes", "yeah", "yep", "nope", "ok", "okay", "right", "sure",
        "hmm", "hm", "um", "uh", "oh", "ah", "hey", "wow", "well",
    }
)
_FRAG_BOUNDARY_TAIL = "，。！？；、,.!?;:…"
_FRAG_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _interp_frag_merge_enabled() -> bool:
    """碎片闸总门(默认开;0=旧路径逐字节——worker 侧短路,零额外 await)。"""
    return os.environ.get(_FRAG_MERGE_ENV, "1") == "1"


# —— B 线豆包尾部续说观察窗(Wave 3b,2026-10-08「切碎」主刀) -------------------
# 诊断定案(2026-10-08 实弹):豆包 lane 只按 VAD 静音切句(B 线 min_silence=0.45s),
# 思考停顿/换气即一个 FINAL,一段话被切成 N 段独立烧全链(ASR+DeepSeek MT+TTS),
# 段间隔 1-2s → 对方听感「断断续续」。刀=**尾部续说观察窗**:END_OF_SPEECH 不
# 立刻定稿,先等 BOK_INTERP_UTT_WAIT_S——窗口内续讲=同一 WS 会话续喂并段(服务端
# result.text 单调累积=天然并稿,零拼接账本);静默到底才负 seq 定稿。
# **W0 复核(2026-10-08 晚,A/B 基线 ③号)窗缺省 0.45→0.2**:窗税实弹 +450ms
# 把 B 线首声盖过 A 线(2332 vs 1763),而窗关档 B 反超(1666);并段收益集中在
# <0.65s 微停顿带,0.2 窗已覆盖大头——有效切句边界=说话末音后 0.45+0.2=0.65s
# 总静默(微停顿并段保留,真停顿税砍 250ms)。**B 线专用**(装配点传
# utt_merge=True;A 线 agent.py 不传=逐字节旧路)。kill-switch
# BOK_INTERP_UTT_MERGE=0 整档关闭;窗口 BOK_INTERP_UTT_WAIT_S 可调(坏值回缺省,
# 钳 [0,3])。与 3a 碎片闸互补:本刀治「内容微停顿切碎」,3a 治「纯应承碎片」。
_UTT_MERGE_ENV = "BOK_INTERP_UTT_MERGE"
_UTT_WAIT_ENV = "BOK_INTERP_UTT_WAIT_S"
_UTT_WAIT_DEFAULT_S = 0.2
_UTT_WAIT_MAX_S = 3.0


def _interp_utt_merge_enabled() -> bool:
    """豆包尾部续说观察窗总闸(默认开;0=旧路径逐字节)。"""
    return os.environ.get(_UTT_MERGE_ENV, "1") == "1"


def _interp_utt_wait_s() -> float:
    """观察窗秒(纯函数,单测直喂):坏值回缺省 0.45;负数钳 0;上限 3.0。"""
    raw = os.environ.get(_UTT_WAIT_ENV, "")
    try:
        v = float(raw) if raw else _UTT_WAIT_DEFAULT_S
    except ValueError:
        return _UTT_WAIT_DEFAULT_S
    return min(max(v, 0.0), _UTT_WAIT_MAX_S)


def _interp_frag_hold_s() -> float:
    """hold 窗口秒(纯函数,单测直喂):坏值回缺省 0.6;负数钳 0;上限 2.0。"""
    raw = os.environ.get(_FRAG_HOLD_ENV, "")
    try:
        v = float(raw) if raw else _FRAG_HOLD_DEFAULT_S
    except ValueError:
        return _FRAG_HOLD_DEFAULT_S
    if v < 0:
        return 0.0
    return min(v, _FRAG_HOLD_MAX_S)


def _interp_frag_is_ack(text: str) -> bool:
    """纯应承碎片判据(纯函数):剥标点后为空(纯语气「嗯。」)或逐字纯应承
    (A 线 _PURE_ACK_TAIL_CHARS 复用+人称代词)或整词英文应承(「No.」)。
    数字/任何实词(「我唔知」)→ False 永不闸(内容必译)。"""
    from .providers.livekit_plugins import _PURE_ACK_TAIL_CHARS, _strip_punct_space

    core = _strip_punct_space(str(text or ""))
    if not core:
        return True
    if core.lower() in _INTERP_FRAG_ACK_WORDS:
        return True
    return all(ch in _PURE_ACK_TAIL_CHARS or ch in _INTERP_FRAG_ACK_EXTRA for ch in core)


def _frag_join(frag: str, text: str) -> str:
    """碎片并入下段(纯函数):碎片已带句界标点=CJK 直连/拉丁补空格;裸碎片补
    一枚逗号(CJK 全角)。保序:碎片恒在前(时间序=语序)。"""
    f = str(frag or "").strip()
    t = str(text or "").lstrip()
    if not f:
        return t
    if f[-1] in _FRAG_BOUNDARY_TAIL:
        if f[-1] in ",.?;:!" and t and not _FRAG_CJK_RE.search(t[0]):
            return f + " " + t
        return f + t
    sep = "，" if (_FRAG_CJK_RE.search(f) or _FRAG_CJK_RE.search(t)) else ", "
    return f + sep + t


async def _frag_absorb(
    text: str,
    *,
    q: asyncio.Queue,
    backlog: "_PlaybackBacklog",
    lag: "_LagLedger",
    hold_s: float | None = None,
    max_absorb: int = _FRAG_MAX_ABSORB,
    max_chars: int = _FRAG_MAX_CHARS,
) -> tuple[str, int, float]:
    """hold-and-merge 执行体(worker 专用;队列/账本注入=单测免真会话)。

    调用前提:text 已过 _interp_frag_is_ack 判真(worker 侧闸)。返回
    (合并文, 并入段数, hold 毫秒)。账本补偿:note_src 每入队段一条,N 段并
    1 译=drop_src × (N-1),配对锚=最后一段(merged 译文 t_src=末段 final,hold
    窗不计入 perceived——hold 税单列 INTERP_FRAG 行)。摘译语义保真:hold 中
    弹出的下段若命中背压摘译(_mt_consume_skip 真),该段照旧 drop(不并入不
    译),碎片独立放行。task_done 随 get 逐段平衡(finally 的那枚归外层弹的)。
    """
    hold = _interp_frag_hold_s() if hold_s is None else max(0.0, hold_s)
    parts: list[str] = [str(text)]
    hold_ms = 0.0
    while len(parts) - 1 < max_absorb and sum(len(p) for p in parts) < max_chars:
        w0 = time.perf_counter()
        try:
            nxt = await asyncio.wait_for(q.get(), timeout=hold)
        except asyncio.TimeoutError:
            hold_ms += (time.perf_counter() - w0) * 1000
            break
        hold_ms += (time.perf_counter() - w0) * 1000
        q.task_done()
        if _mt_consume_skip(backlog, nxt):
            lag.drop_src()
            break
        parts.append(nxt)
        if not _interp_frag_is_ack(nxt):
            break  # 内容段已并入,停
    n = len(parts) - 1
    if n <= 0:
        print(
            f"[interp] INTERP_FRAG flush chars={len(str(text))} hold_ms={int(hold_ms)}",
            flush=True,
        )
        return str(text), 0, hold_ms
    for _ in range(n):
        lag.drop_src()
    merged = parts[0]
    for p in parts[1:]:
        merged = _frag_join(merged, p)
    print(
        f"[interp] INTERP_FRAG merge n={n + 1} chars={len(merged)} hold_ms={int(hold_ms)}",
        flush=True,
    )
    return merged, n, hold_ms


# —— B 线 interim 投机翻译(prewarm-and-confirm,2026-10-06) -------------------
# Ethan 指令:A/B 线都要吃 interim 流喂 LLM,B 线反应必须快过 A 线。目标:译文
# 首声从「segment-end + MT 往返 + TTS 首包」压到「final 即声」——MT+合成在
# 说话期间预付。姿势=**只预热不出声**(错译文出声比慢更糟,无早期播放):
#   ① detector 吃 interim 流(doubao=段内累积全文;qwen 本地=句级提交后的未提交
#      尾巴——坐标系差异经「候选身份变化=稳定性重计」天然消化),识别末个稳定
#      子句 span(边界标点收尾+内容 ≥6 字+跨 ≥2 次 interim 不变或 ≥500ms);
#   ② 开火→后台同款 _mt_once 翻译该 span,译文经 tts_provider.synthesize 全量
#      排干成 PCM 持在 held slot(不进 say 队列,零播放);
#   ③ final 到达:归一前缀比对(≥0.85)——HIT=held PCM 走 session.say(text,
#      audio=frames) 零合成直播(qa_gate 罐头低 TTFT 车同款,agent.py say_cached
#      同构),余段进 _src_q 正常管线;MISS=弃 slot(cancel 在途 MT:本地车道
#      aclose 经 _attach_mlx_abort 即 POST /v1/abort,云端 openai 车道无补丁
#      =纯丢弃),正常路径原样走。
# kill-switch ``BOK_INTERP_SPEC_MT``(默认 1;0=旧路径逐字节,interim 照旧丢弃、
# final 照旧整句入队)。护栏:真车道忙(FIFO 非空/真 MT 在途/背压摘译 pending)
# 一律不开火;每段 ≤2 次;再开火须比上次 span 长 ≥6 字;数字串 span(≥4 位 run,
# 镜像 flow._digit_runs_in 族口径)永不投机(号码高危)。
_SPEC_KILL_SWITCH_ENV = "BOK_INTERP_SPEC_MT"
_SPEC_CLAUSE_BOUNDARY = "，、；,;。？！.!?"
_SPEC_DIGIT_RUN_RE = re.compile(r"[0-9一二三四五六七八九零]{4,}")
# 归一化口径:剥标点/空白/全半角差异,留 \w+CJK(镜像 agent._ticket_norm)——
# 前缀比对与切点锚都跑在这层,ASR 对标点的增删不改判定。
_SPEC_NORM_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
_SPEC_CONFIRM_SIM = 0.85  # final 与投机 span 的归一前缀相似度门


def _spec_mt_enabled() -> bool:
    """投机翻译总闸(默认开;0=旧路径逐字节)。"""
    return os.environ.get(_SPEC_KILL_SWITCH_ENV, "1") == "1"


def _spec_norm(text: str) -> str:
    """投机比对归一化(纯函数):剥标点/空白,小写,留 word 字符+CJK。"""
    return _SPEC_NORM_RE.sub("", str(text or "")).lower()


def _spec_clause_prefix(text: str) -> str | None:
    """text 中「最后一个子句边界标点」收尾的前缀(纯函数);无边界=None。"""
    cut = max((text.rfind(ch) for ch in _SPEC_CLAUSE_BOUNDARY), default=-1)
    if cut < 0:
        return None
    return text[: cut + 1]


def _spec_content_chars(text: str) -> int:
    """span 内容字数(纯函数):剥边界标点/空白后的长度(字数闸口径)。"""
    return len(_SPEC_NORM_RE.sub("", str(text or "")))


def _spec_prefix_split(final_text: str, span: str) -> tuple[float, str]:
    """final 与投机 span 的前缀比对(纯函数):返回 (相似度, 余段)。

    相似度=difflib ratio(span 归一, final 归一同长头部)——ASR 对 span 内个别字
    的修正在门(≥0.85)内容忍;切点锚=归一字符等长消费(逐 raw 字符数归一存活位),
    修正不改切点位置;切点后挂着的边界标点/空白一并吞进 span 侧(余段从下一
    个归一存活字符起)。final 归一不足 span 长度 → 相似度照算但余段恒空
    (len 门兜底),调用方按 MISS 处理。"""
    nf, ns = _spec_norm(final_text), _spec_norm(span)
    if not ns:
        return 0.0, ""
    head = nf[: len(ns)]
    sim = difflib.SequenceMatcher(a=ns, b=head).ratio()
    if sim < _SPEC_CONFIRM_SIM or len(nf) < len(ns):
        # 不过门:余段不可信(切点没锚住),恒空——调用方按 MISS 处理。
        return sim, ""
    consumed = 0
    for i, ch in enumerate(final_text):
        if _spec_norm(ch):
            consumed += 1
            if consumed >= len(ns):
                j = i + 1
                while j < len(final_text) and not _spec_norm(final_text[j]):
                    j += 1
                return sim, final_text[j:].strip()
    return sim, ""  # 不可达(len 门已保证),防御


class _SpecMtDetector:
    """interim 稳定子句检测器(纯同步状态机,单测直喂;时钟可注入)。

    候选=interim 文本里「最后边界标点收尾的前缀」;同一候选跨 ≥2 次 interim
    不变(或首见后 ≥0.5s)判稳定。开火后记账 last_spec,再开火须比它长 ≥6 字
    (doubao 累积流=同段渐进;本地尾巴流=候选身份变化即稳定性重计,天然防
    坐标系切换误开火)。每段预算 reset_segment 清——final 到达即切段。"""

    MIN_CLAUSE_CHARS = 6
    REFIRE_GROWTH_CHARS = 6
    MAX_PER_SEGMENT = 2
    STABLE_SIGHTINGS = 2
    STABLE_S = 0.5

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cand = ""
        self._seen = 0
        self._since = 0.0
        self._last_spec = ""
        self._fires = 0

    def feed(self, text: str, *, now: float | None = None) -> str | None:
        """吃一条 interim(已去重),返回应投机的 span 或 None。``now`` 供单测
        免真钟(缺省读构造时钟)。"""
        text = str(text or "").strip()
        if not text:
            return None
        cand = _spec_clause_prefix(text)
        if cand is None or _spec_content_chars(cand) < self.MIN_CLAUSE_CHARS:
            return None
        if _SPEC_DIGIT_RUN_RE.search(cand):
            return None  # 号码高危:数字串 span 永不投机(镜像 flow 数字 run 族)
        ts = float(self._clock()) if now is None else float(now)
        if cand != self._cand:
            self._cand, self._seen, self._since = cand, 1, ts
        else:
            self._seen += 1
        if self._seen < self.STABLE_SIGHTINGS and (ts - self._since) < self.STABLE_S:
            return None
        if self._fires >= self.MAX_PER_SEGMENT:
            return None
        if len(cand) - len(self._last_spec) < self.REFIRE_GROWTH_CHARS:
            return None
        self._last_spec = cand
        self._fires += 1
        return cand

    def reset_segment(self) -> None:
        """final 到达切段:候选/预算/last_spec 全清。"""
        self._cand = ""
        self._seen = 0
        self._since = 0.0
        self._last_spec = ""
        self._fires = 0


class _SpecMtHold:
    """投机持有槽:在途任务 + 就绪的 (src, text, pcm)——只预热不出声的载体。"""

    __slots__ = ("task", "src", "text", "pcm")

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.src = ""
        self.text = ""
        self.pcm = b""


class _SpecMtController:
    """B 线 interim 投机翻译控制器(prewarm-and-confirm;每方向一个)。

    骨架=detector(稳定子句) + held slot(在途 MT/TTS) + final 确认。真件
    (MT/TTS/say/队列/账本)全部经构造注入——entrypoint 组装真件,单测喂 fake;
    ``enabled=False``(kill-switch=0 或 text-only 方向)全入口零动作=旧路径。"""

    def __init__(
        self,
        *,
        enabled: bool,
        detector: _SpecMtDetector,
        hold: _SpecMtHold,
        run_spec: Callable,
        busy_gate: Callable[[], bool],
        say_cached: Callable,
        enqueue: Callable[[str], None],
        log: Callable[[str], None] = print,
    ) -> None:
        self._enabled = enabled
        self._detector = detector
        self.hold = hold
        self._run_spec = run_spec  # async (span) -> (text, pcm);空=投机未成
        self._busy_gate = busy_gate  # () -> bool:True=真车道忙,不开火
        self._say_cached = say_cached  # (final_src, text, pcm) -> None(异常上抛)
        self._enqueue = enqueue  # (rest) -> None:余段入 FIFO+记账
        self._log = log

    # ---- interim 入口 -----------------------------------------------------
    def on_interim(self, text: str) -> None:
        if not self._enabled or self._busy_gate():
            return
        span = self._detector.feed(text)
        if not span:
            return
        hold = self.hold
        hold.src = span
        hold.text = ""
        hold.pcm = b""
        old = hold.task
        if old is not None and not old.done():
            old.cancel()  # 单飞:新 span 顶掉旧投机(cancel→本地车道 abort/云端纯弃)
        hold.task = asyncio.create_task(self._fire(span))
        self._log(f"[interp] INTERP_SPEC fire chars={len(span)}")

    async def _fire(self, span: str) -> None:
        try:
            text, pcm = await self._run_spec(span)
        except asyncio.CancelledError:
            self._log(f"[interp] INTERP_SPEC abort chars={len(span)}")
            raise
        except Exception as exc:  # noqa: BLE001 - 投机失败零影响(保持 slot 空=MISS)
            self._log(f"[interp] INTERP_SPEC failed: {exc!r}")
            return
        if not text or not pcm:
            return
        self.hold.text = text
        self.hold.pcm = pcm

    # ---- final 入口 -------------------------------------------------------
    def on_final(self, final_text: str) -> bool:
        """final 到达:确认投机。返回 True=已投机交付(调用方跳过正常路径);
        False=MISS/关闸(调用方走原路径)。切段(预算重置)两态都做。"""
        if not self._enabled:
            self._detector.reset_segment()
            return False
        played = self._confirm(str(final_text or "").strip())
        self._detector.reset_segment()
        return played

    def _confirm(self, final_text: str) -> bool:
        hold = self.hold
        span = hold.src
        task = hold.task
        hold.src = ""
        hold.task = None
        if not span:
            return False
        if task is not None and not task.done():
            # 未就绪:预热没赢过说话,投机价值已失——cancel 在途 MT 让路真车道
            # (本地=abort 即时弃流;云端=客户端断开纯丢弃)。
            task.cancel()
        text, pcm = hold.text, hold.pcm
        hold.text = ""
        hold.pcm = b""
        if not text or not pcm:
            self._log(f"[interp] INTERP_SPEC miss chars={len(span)} reason=not_ready")
            return False
        sim, rest = _spec_prefix_split(final_text, span)
        if sim < _SPEC_CONFIRM_SIM:
            self._log(f"[interp] INTERP_SPEC miss chars={len(span)} sim={sim:.2f}")
            return False
        self._log(
            f"[interp] INTERP_SPEC hit chars={len(span)} rest={len(rest)} sim={sim:.2f}"
        )
        try:
            self._say_cached(final_text, text, pcm)
        except Exception as exc:  # noqa: BLE001 - 播放失败退正常路径(队列兜底)
            self._log(f"[interp] INTERP_SPEC say failed: {exc!r}")
            return False
        if rest:
            self._enqueue(rest)
        return True

    # ---- 生命周期 ---------------------------------------------------------
    def cancel(self, reason: str = "shutdown") -> None:
        """teardown/重连卫生:弃 slot + cancel 在途任务(绝不外抛)。"""
        task = self.hold.task
        self.hold.src = ""
        self.hold.task = None
        if task is not None and not task.done():
            task.cancel()


async def _exit_stage(name: str, coro, timeout_s: float = 5.0):
    """退出路径单段守护（P1.c，2026-09-29 v2 spec §4）。

    b8793951 实证 `process did not exit in time, killing process`：_shutdown
    的 report/settle 走 CP client（timeout=15s），两段即可挂 30s+ 超过框架
    10s 强杀窗。本助手：wait_for 掐死 + 慢段打点 `interp.exit_slow stage=
    <n> ms=<t>`（观测定位）+ 超时/取消/异常吞掉——收尾尽力而为（CP settle
    幂等，丢了 job 死后回收器兜底），绝不挂死退出主链。返回 coro 结果或
    None（被掐/异常）。"""
    import time as _time

    t0 = _time.monotonic()
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.CancelledError:
        # 段被取消：吞（镜像旧 _mt_worker 段的 except (CancelledError, Exception)
        # 语义——退出回调里段取消不该挂死主链）。
        print(f"interp.exit_slow stage={name} ms={(_time.monotonic() - t0) * 1000:.0f} kind=cancelled", flush=True)
        return None
    except Exception as exc:  # noqa: BLE001 - 超时/异常全吞：收尾尽力而为
        kind = "timeout" if isinstance(exc, asyncio.TimeoutError) else "err"
        print(
            f"interp.exit_slow stage={name} ms={(_time.monotonic() - t0) * 1000:.0f} "
            f"{kind}={exc!r}",
            flush=True,
        )
        return None


async def entrypoint(ctx) -> None:
    from livekit import rtc
    from livekit.agents import (
        Agent,
        AgentSession,
        RoomInputOptions,
        RoomOutputOptions,
        inference,
    )
    from livekit.agents import stt as lk_stt

    from .control_plane import ControlPlaneClient
    from .providers.doubao_asr import DoubaoSTT, doubao_asr_enabled
    from .providers.livekit_plugins import (
        LanguageState,
        Qwen3ASRLiveSTT,
        Qwen3ASRSTT,
        _asr_engine_from_cfg,
        _parse_vocab_terms,
    )

    meta: dict = {}
    try:
        meta = json.loads(getattr(ctx.job, "metadata", "") or "{}")
    except Exception:
        meta = {}
    # CP /api/token 写入精确 identity(它已知房间名);缺关键 metadata 说明分发
    # 配置不对,无法安全选边,直接放弃本 job。
    listen_identity = str(meta.get("listen_identity") or "").strip()
    deliver_identity = str(meta.get("deliver_identity") or "").strip()
    source_lang = _norm_lang(str(meta.get("source_lang") or "zh"))
    target_lang = _norm_lang(str(meta.get("target_lang") or "en"))
    # 术语表(可选,建单时填,CP 随 dispatch metadata 下发):ASR 热词 + MT prompt
    # 术语槽双路注入,治领域词误听与译名漂移(「无术语表」是 B 线对业界同传的
    # 结构性差距,2026-09-16 调研定案 P0-2)。
    glossary_pairs = parse_glossary(str(meta.get("glossary") or ""))
    # 会话级音色(可选,同传页建单时我方/对方各选一把):voices_json 随 dispatch
    # metadata 下发,_build_tts_provider 组 voice_map 时最优先(> 设置三键 > 默认)。
    session_voices = _parse_session_voices(meta.get("voices"))
    if not listen_identity or not deliver_identity:
        print(
            f"[interp] job metadata missing listen_identity/deliver_identity: {meta!r} — abort",
            flush=True,
        )
        return

    room = ctx.room
    room_name = room.name
    call_id = room_name  # 房间名 = call_id(CP 建会话时生成,与 A 线同约定)
    speaker_role = "me" if listen_identity.startswith("me-") else "other"

    print(
        f"[interp] room={room_name} listen={listen_identity} deliver={deliver_identity} "
        f"{source_lang}->{target_lang} "
        f"audio={'on' if _direction_audio_enabled(speaker_role) else 'text-only'}",
        flush=True,
    )

    cp = ControlPlaneClient(os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"), call_id=call_id)
    settings: dict = {}
    try:
        settings = await cp.get_settings()
    except Exception as exc:  # pragma: no cover - 设置失败回退默认
        print(f"[interp] settings resolve failed: {exc!r}", flush=True)
    # —— B 线人设音色复用（2026-10-08）：同传台建单可绑人设（persona_id 随
    # dispatch metadata 下发），音色链插「人设层」（复用 A 线 parse_voice_map
    # 单源=A 线同一份 reference_audio 解析）。优先级=会话级 voices > 人设 >
    # 设置三键 > 硬编码；拉取失败=该层缺席，链路回落零损伤。四语目标
    # （de/fr/ja/pt）不在人设 map 内=天然回落设置/默认。
    _persona_voices: dict = {}
    _persona_id = str(meta.get("persona_id") or "").strip()
    if _persona_id:
        try:
            _persona = await cp.get_persona(_persona_id)
            _persona_voices = parse_voice_map(_persona.get("reference_audio"))
            _keys = sorted(k for k, v in _persona_voices.items() if str(v or "").strip())
            print(f"[interp] persona voice id={_persona_id[:16]} keys={_keys}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 人设拉取失败不阻通话
            print(f"[interp] persona fetch failed id={_persona_id[:16]}: {exc!r}", flush=True)
    llm_cfg = settings.get("llm", {}) or {}
    asr_cfg = settings.get("asr", {}) or {}
    tts_cfg = settings.get("tts", {}) or {}
    vad_cfg = settings.get("vad", {}) or {}

    def _cfg_float(key: str, env_key: str, default: str) -> float:
        env_raw = os.environ.get(env_key)
        try:
            return float(env_raw if env_raw is not None else (default if vad_cfg.get(key) is None else vad_cfg.get(key)))
        except Exception:
            return float(default)

    # 翻译输出按句合成,token 上限放宽(默认 160 是客服短句口径,长句会截断)——
    # 2026-10-02 刀1:不再 `os.environ.setdefault("LLM_MAX_TOKENS", "512")`(常驻
    # worker 写入即驻留、跨会话泄漏),改由 _build_llm_provider 各构造点显式传参
    # max_tokens 512 下发(构造参数,进程 env 零写入)。

    # VAD 基线与 A 线一致(0.45 静音/0.15 起声/0.75 抗噪):压缩端点会让轮次
    # 在整包 ASR 返回前提交,转写被丢——同传同样受此约束。
    vad_provider = inference.VAD(
        max_buffered_speech=_cfg_float("max_buffered_speech", "VAD_MAX_BUFFERED_SPEECH", "15"),
        min_speech_duration=_cfg_float("min_speech_duration", "VAD_MIN_SPEECH_DURATION", "0.15"),
        min_silence_duration=_cfg_float("min_silence_duration", "VAD_MIN_SILENCE_DURATION", "0.45"),
        activation_threshold=_cfg_float("sensitivity", "VAD_ACTIVATION_THRESHOLD", "0.75"),
    )

    # 源语言钉死(用户建房时选定):zh/en/cantonese 都下发 hint——粤语防 auto 误判成
    # 普通话,英语/普通话钉定保证整场识别稳定,不吃 auto 的偶发漂移。
    asr_ls = LanguageState()
    asr_ls.lang = source_lang
    # 热词 context:术语表源语词条(用户建单指定,最贴当前场景)。include_industry
    # =False——A 线行业静态词(单号/运单/赔偿…)是快递客服域,B 线通用同传不吃;
    # 数字主导项丢弃/去重/200 字上限/BOK_ASR_HOTWORDS kill-switch 全部复用单源。
    from .agent import asr_hotword_context

    _asr_hotword_ctx = asr_hotword_context(
        source_lang,
        None,
        extra_hotwords=glossary_source_terms(glossary_pairs),
        include_industry=False,
    )
    _asr_provider_name = str(asr_cfg.get("provider") or "qwen3_asr").strip().lower()
    _doubao_key = str(asr_cfg.get("api_key") or "").strip()
    _doubao_old_auth = bool(str(asr_cfg.get("app_id") or "").strip()) and bool(
        str(asr_cfg.get("access_token") or "").strip()
    )
    # MiniMax ASR 凭据:env 优先(probe 同源 MINIMAX_API_KEY,_FORWARD_ENV 已登记),
    # 缺省回设置面 tts.api_key——MiniMax 控制台同一把 key(TTS/ASR 共用,probe
    # minimax_key() 回读同一 DB 面)。零打印(掩码都不打,装配行只报语言)。
    _minimax_key = (
        os.environ.get("MINIMAX_API_KEY", "").strip()
        or str(tts_cfg.get("api_key") or "").strip()
    )
    if _minimax_asr_lane(source_lang, _minimax_key):
        # 四语车道（2026-10-07 W2c）：de/fr/ja/pt 源语豆包 24/24 幻听，MiniMax
        # asr-1.0 24/24 CER≤0.08——官方 stt.StreamAdapter(vad=) 包 offline
        # provider 伪流式（MiniMax 输入侧不支持推流，SSE 只是结果流）。MiniMax
        # ASR 无热词表，术语表只帮 MT 侧（_glossary 照旧进翻译 prompt）。
        from .providers.minimax_asr import LANG_TAGS, MiniMaxSTT

        stt_provider = lk_stt.StreamAdapter(
            stt=MiniMaxSTT(
                api_key=_minimax_key,
                language_state=asr_ls,
                lang_tag=LANG_TAGS[source_lang],
            ),
            vad=vad_provider,
        )
        print(
            f"[interp] asr=minimax (4lang lane) lang={source_lang}",
            flush=True,
        )
    elif (
        _asr_provider_name in ("doubao", "doubao_asr")
        and doubao_asr_enabled()
        and (_doubao_key or _doubao_old_auth)
    ):
        # 云端豆包 SAUC 流式（B 线同传，2026-10-03 云 ASR 装线波；与 A 线共用
        # provider 类）。凭据/端点=设置面 asr 段；热词=术语表源语词条（同上文
        # _asr_hotword_ctx 单源）。
        stt_provider = DoubaoSTT(
            api_key=_doubao_key,
            resource_id=str(asr_cfg.get("resource_id") or "").strip(),
            ws_url=str(asr_cfg.get("endpoint") or "").strip(),
            app_id=str(asr_cfg.get("app_id") or "").strip(),
            access_token=str(asr_cfg.get("access_token") or "").strip(),
            language_state=asr_ls,
            hotword_terms=list(_parse_vocab_terms(_asr_hotword_ctx)),
            vad_=vad_provider,
            # B 线 3b 尾部续说观察窗(A 线不传=逐字节旧路);kill-switch/调窗见
            # _interp_utt_* 纯函数块注释。
            utt_merge=_interp_utt_merge_enabled(),
            utt_wait_s=_interp_utt_wait_s(),
        )
        print(
            f"[interp] asr=doubao (cloud SAUC, resource={stt_provider._resource_id}"
            + (
                f", utt-merge wait={_interp_utt_wait_s():g}s)" if _interp_utt_merge_enabled() else ")"
            ),
            flush=True,
        )
    else:
        if _asr_provider_name in ("doubao", "doubao_asr"):
            print(
                "[interp] asr=doubao unavailable — falling back to local Qwen3-ASR",
                flush=True,
            )
        _asr_inner = Qwen3ASRSTT(
            base_url=_sidecar_url(asr_cfg.get("base_url") or "", "QWEN3_ASR_BASE_URL", "http://127.0.0.1:8787"),
            language_state=asr_ls,
            pin_language=True,
            hotword_context=_asr_hotword_ctx,
            # 【P1 SV-CPU 引擎车道(2026-10-01)】与 A 线同解析器:env > asr_json >
            # 缺省旧路;验证门后同步翻 sensevoice。
            engine=_asr_engine_from_cfg(asr_cfg),
        )
        if os.environ.get("QWEN3_ASR_STREAM", "1") == "1":
            # 同传更要 partial:源语音边说边出稳定前缀 → 抢跑 prefill,译文首句更早。
            stt_provider = Qwen3ASRLiveSTT(stt_=_asr_inner, vad_=vad_provider)
        else:
            stt_provider = lk_stt.StreamAdapter(stt=_asr_inner, vad=vad_provider)

    # 翻译 LLM 与 TTS 组装走模块级纯函数(单测直接喂 cfg,唔使起 worker)。
    _glossary = glossary_block(glossary_pairs)
    if _glossary:
        print(f"[interp] glossary {len(glossary_pairs)} terms -> asr+mt", flush=True)
    # B 线官方对账(2026-09-30):text-only 方向(BOK_INTERP_REV_AUDIO=0 回退档)此前无条件构造并
    # **真连**云端 MiniMax bidi(prewarm connect_ms=446 实测)——但
    # RoomOutputOptions.audio_enabled=False 下 TTS 永不被调用=零收益连接,
    # 还与在途方向抢握手。音频向关闭的方向直接不装配 TTS。
    _dir_audio = _direction_audio_enabled(speaker_role)
    tts_provider = (
        _build_tts_provider(tts_cfg, target_lang, session_voices, persona_voices=_persona_voices)
        if _dir_audio
        else None
    )
    # 语气词标记(2026-09-16 用户拍板;2026-10-08 v2):Hy-MT2/云端 MT 都会把语气照
    # 词翻译(Hahaha/哈哈),say 前由 _speech_text→_apply_voice_tags 换成 MiniMax
    # 2.8 括号标记——合成层出真声(笑/咳/叹),不再是假人念稿;v2 收任意位置笑声
    # 簇+CJK 目标语(旧版句首一刀切漏句尾,call-996f3917)。双门控:模型档(仅 2.8
    # 系支持,非 2.8 会把标记念出来;门关也剥——云端 MT 按 instructions 产出的
    # 标记在非 2.8 档念出来=假人念稿) + env 总闸(BOK_INTERP_VOICE_TAGS=0 关)。
    # 标记进 say() 文本,字幕/落库由 _caption_text(译文行,纯语气句回退（笑）占位)
    # 与前端 stripVoiceTags(字幕)剥掉,只活合成层。
    # 模型档只认 MiniMax 分支(旧版靠 _build_tts_provider 的 setdefault 副作用
    # 传递;本地 Qwen3 兜底档标记会被当文本念出来,必须保持熄火)。
    # 语气词标记同理只在音频向有意义(合成层标记,text-only 无合成=纯噪音)。
    _tts_is_minimax = (tts_cfg.get("provider") or "qwen3_tts").lower() in (
        "minimax",
        "minimax_streaming",
    )
    tts_model = (_resolve_minimax_model() if _tts_is_minimax else "") if _dir_audio else ""
    voice_tags = (
        _dir_audio
        and os.environ.get("BOK_INTERP_VOICE_TAGS", "1") == "1"
        and _voice_tags_supported(tts_model)
    )
    if tts_model:
        print(f"[interp] voice_tags {'on' if voice_tags else 'off'} (tts={tts_model})", flush=True)
    # 模型路由原始串（2026-09-25 阶段 0）：CP 设置顶层键与引擎卡同一 fetch（改道
    # 下一通生效，零重启）；缺键/空＝未配置，resolve_route 落回 env 缺省链。随当通
    # 会话传参，worker 并发多通不串线（禁模块级可变全局）。
    # 构造整体下线程(2026-10-02 review 修):内部含 _mt_endpoint_alive 的同步
    # urllib 1.5s 探活——死端点窗内若直跑在事件循环上会拖住同 worker 的并发
    # job;装配非热路径,to_thread 包裹后循环零阻塞。
    llm_provider = await asyncio.to_thread(
        _build_llm_provider,
        llm_cfg,
        target_lang,
        glossary=_glossary,
        routing_raw=str(settings.get("model_routing_json") or ""),
    )

    # 轮次判定走 _turn_handling_opts(纯函数):manual 模式 + STT 句级 FINAL 自驱
    # MT→say 队列(P2 定案,函数注释有框架打断/丢弃两条路的实证);kill-switch
    # 配对与 A 线同一对 env(TURN_DETECTION≠stt → 回 EOT 整段档)。
    turn_handling = _turn_handling_opts()
    _th = turn_handling
    print(
        "[interp] turn_handling: "
        f"endpointing=dynamic({_th['endpointing']['min_delay']}/{_th['endpointing']['max_delay']}) "
        f"preemptive={'on' if _th['preemptive_generation']['enabled'] else 'off'} "
        f"max_retries={_th['preemptive_generation']['max_retries']} "
        f"interruption={'on' if _th['interruption']['enabled'] else 'off(同传语义)'} "
        f"turn_detection={_th.get('turn_detection') or 'default(EOT kill-switch)'}"
        f"{'(STT句final自驱MT→say队列)' if _th.get('turn_detection') == 'manual' else ''}",
        flush=True,
    )
    session = AgentSession(
        vad=vad_provider,
        stt=stt_provider,
        llm=llm_provider,
        tts=tts_provider,
        turn_handling=turn_handling,
    )

    # 落库:原文/译文拆成两条 turn(2026-09-07 审计闭环——旧行为合成一条,
    # 无 language 标签、译文延迟无从查)。译文行带 latency;原文行即时落,
    # 译文后到再落——总结/蒸馏按序读仍是对照文本(language 字段区分)。
    # B 线感知延迟账本(RC-8,2026-10-02 刀1):当通单调基线 + FIFO 配对(见
    # _LagLedger docstring)。口径镜像 A 线——started/ended=通话相对毫秒,
    # perceived=源句 final→译文 item 落地(代理口径:整链上界,含播报)。
    _t0 = time.monotonic()
    _lag = _LagLedger()
    last_user = {"text": ""}

    async def _add_turn(
        text: str,
        language: str,
        latency: int = 0,
        *,
        started_ms: int = 0,
        ended_ms: int = 0,
        perceived_ms: int = 0,
    ) -> None:
        try:
            await cp.add_turn(
                call_id, speaker_role, text, provider="interpret", latency_ms=latency, language=language,
                line="b", speaker=speaker_role,  # B 线账本:此前缺省误标 line=a(P0 遗留)
                # RC-8:源句 final→译文入账时间轴(call 相对毫秒,缺省 0=无配对不落)。
                started_ms=started_ms, ended_ms=ended_ms, perceived_ms=perceived_ms,
            )
        except Exception as exc:  # pragma: no cover - 落库失败不阻翻译
            print(f"[interp] add_turn failed: {exc!r}", flush=True)

    # 在途账本任务强引用池(2026-09-17 全量 debug P2-A):GC 中途回收=原文/译文
    # 行静默缺行——B 线纪要账本行丢失不可补,池化首选。机制在模块级
    # _spawn_pooled_task(单测直喂),此处只是入口点作用域的薄包装。
    _ledger_tasks: set = set()

    def _spawn_ledger(coro) -> None:
        _spawn_pooled_task(coro, _ledger_tasks, "LEDGER_TASK_ERR")

    # —— 同传主链(P2 manual 架构):STT 句 final 自驱 MT → session.say() 队列 ——
    # 框架 turn_detection=manual:不再自动回复。框架对「播报中到达的新用户轮」
    # 只有两条路——打断在途译文(allow_interruptions=True)或整轮丢弃(=False,
    # 连原文落库都没有;probe_interp_backlog 实弹 8 句只落 5 句),两条都唔係
    # 译员行为。manual 下句子经 user_input_transcribed(is_final) 进本 worker 的
    # 单消费队列,MT 完一条 say 一条:say 队列串行播,MT 与播报流水线重叠,
    # 积压由 _PlaybackBacklog 门槛追最新弃旧。
    _llm_instructions = _translation_instructions(source_lang, target_lang, _glossary)
    _mt_pairs: deque = deque(maxlen=8)  # (源,译) 滚动对——_rolling_pairs 的参考料
    _mt_latency = {"ms": 0}
    # Wave 2 首子句时延(流式 say 才有语义:final→首子句交 TTS;整句路径=0)
    _mt_first = {"ms": 0}
    _src_q: asyncio.Queue = asyncio.Queue(maxsize=48)

    def _on_item(ev) -> None:
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        text = str(getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or "").strip()
        if not text:
            return
        if role == "user":
            # manual 模式用户轮不进 chat ctx(原文行由 _on_user_input 即时落);
            # 此分支只兜收线 drain 的残余 user item,不再落库防重复行。
            last_user["text"] = text
        elif role == "assistant":
            latency = int(_mt_latency.get("ms") or 0)
            # RC-8(2026-10-02 刀1):译文 item 落地时取账本配对,落时间轴三列+
            # 每句 INTERP_LAG 观测行(计时起点=源句 final,代理口径见 _LagLedger)。
            # 无配对(MT 兜底句/异常轮):三列缺省 0,add_turn 与旧行为逐字节同。
            _rec = _lag.pop_pending()
            if _rec is not None:
                _started_ms, _ended_ms, _perceived_ms = _lag_turn_timing(
                    _rec, time.monotonic(), _t0
                )
                print(
                    f"[interp] INTERP_LAG src_chars={_rec[1]} mt_ms={_rec[2]} "
                    f"first_ms={_mt_first.get('ms') or 0} perceived_ms={_perceived_ms}",
                    flush=True,
                )
                _mt_first["ms"] = 0
                _spawn_ledger(
                    _add_turn(
                        f"译文：{_caption_text(text, target_lang)}", target_lang, latency,
                        started_ms=_started_ms, ended_ms=_ended_ms, perceived_ms=_perceived_ms,
                    )
                )
            else:
                _spawn_ledger(_add_turn(f"译文：{_caption_text(text, target_lang)}", target_lang, latency))

    session.on("conversation_item_added", _on_item)

    async def _mt_say_worker() -> None:
        # 单消费 FIFO:句序=翻译序=播报序。MT 下一句时上一句照播(流水线重叠)。
        _round = 0  # 本通 MT 取句序号(可观测:超时兜底行带 round=)
        while True:
            text = await _src_q.get()
            _round += 1
            # 真 MT 在途旗(2026-10-06 投机翻译 busy 闸消费;置位/清位零行为变化,
            # BOK_INTERP_SPEC_MT=0 时无读者)。
            _mt_busy["flag"] = True
            try:
                # 背压摘译(2026-09-23 修复波#2):积压门 arm 的摘译指令在取句时
                # 消费——跳过最旧待译源句的 MT+播报(原文行已落库=摘译保文)。
                if _mt_consume_skip(backlog, text):
                    _lag.drop_src()  # 摘译句不产出译文:消费 src 头保后续配对对齐
                    continue
                # Wave 3a 碎片闸(hold-and-merge,2026-10-08):纯应承碎片 hold
                # ≤BOK_INTERP_FRAG_HOLD_S 等下段并入一次翻译(账本 drop_src 补偿
                # 在 _frag_absorb 内);超时=照旧单独出译。kill-switch=0 短路
                # =旧路径逐字节(零额外 await)。hold 期 _mt_busy 已置位=投机
                # 不开火(下段改走队列被并入,防双烧)。
                if _interp_frag_merge_enabled() and _interp_frag_is_ack(text):
                    text, _frag_n, _frag_hold_ms = await _frag_absorb(
                        text, q=_src_q, backlog=backlog, lag=_lag
                    )
                t0 = time.perf_counter()
                # MT 入口确定性音近吸附(2026-10-08 P0,subagent 调研:今晚 69 条原文
                # ≈19% ASR 噪声被忠实翻译):复用 A 线 asr_polish 确定性层(纯本地
                # ~1ms,kill-switch 同 BOK_ASR_POLISH)。**原文单轨铁律**:账本/字幕/
                # spec 判定/QA 全吃 raw,只有 MT 输入与滚动对 `_mt_pairs` 吃吸附副本
                # ——与 A 线 ContextAwareLLM 冻结点同构。四语源(de/fr/ja/pt)表空
                # 且 detect 有误判风险,直通。
                text_mt = _polish_for_mt(text, source_lang)
                ctx = _build_mt_context(_llm_instructions, list(_mt_pairs), text_mt)
                # Wave 2 流式交付(2026-10-08):流式 say 逐子句喂出,首子句即合成
                # 开播——旧「整句排干再合成」的尾段等待被吃掉;一源句=一 say=一
                # item(账本/_on_item 口径不变)。gate/error_pre(零播报)→回退
                # _mt_once 旧路径(保留 chat_retry 语义);error_mid(部分已出声)
                # **不回退**防重复播报。kill-switch BOK_INTERP_MT_STREAM_SAY=0 整段
                # 跳过=旧路径逐字节。
                _streamed = False
                if _mt_stream_say_enabled():
                    _sr = await _mt_stream_say(
                        session, llm_provider, ctx, target_lang=target_lang, tags=voice_tags, t0=t0
                    )
                    if _sr["state"] in ("clean", "error_mid"):
                        translated = _sr["full"]
                        _mt_first["ms"] = _sr["first_ms"]
                        _streamed = True
                    else:
                        print(
                            f"[interp] MT_STREAM fallback state={_sr['state']} round={_round}",
                            flush=True,
                        )
                if not _streamed:
                    translated = await _mt_once(llm_provider, ctx, target_lang=target_lang)
                    _mt_first["ms"] = 0
                _mt_latency["ms"] = int((time.perf_counter() - t0) * 1000)
                if translated:
                    _mt_pairs.append((text_mt, translated))
                    # 先 say 后记账:say 失败(会话关闭)不留 pending 孤儿——待配对
                    # 队列只装「交付已发起」的句,与 item 到达序仍一一对应(RC-8)。
                    if not _streamed:
                        session.say(_speech_text(translated, voice_tags))
                    _lag.done_mt(_mt_latency["ms"])
                else:
                    _lag.drop_src()
                    print(f"[interp] mt empty for {len(text)} chars, skipped", flush=True)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # MT 超时此前静默吞掉(整通零译文出声,21 次实证)。现按目标语说一句
                # 中性请示语——同传语义绝不回放源文;译文行故意不写(诚实缺行,原文行
                # 已在 _on_user_input 即时落库)。say 走同一条译文输出链。
                print(
                    f"[interp] MT_TIMEOUT_FALLBACK room={room_name} round={_round} "
                    f"lang={target_lang}",
                    flush=True,
                )
                try:
                    session.say(_mt_fail_line(target_lang))
                    # 兜底句也是一次交付(2026-10-02 LagLedger 错位根修):say 出声
                    # 就配对记账(done_mt(0),mt_ms=0=非真译),否则 _on_item 会偷弹
                    # **下一条真译文**的 pending——三列时间轴整体错一位、末条永不弹。
                    # 先 say 后记账与成功路径同款:say 失败(会话关闭)不留孤儿。
                    _lag.done_mt(0)
                except Exception as say_exc:  # noqa: BLE001 - 兜底不出声也不阻后续
                    _lag.drop_src()
                    print(f"[interp] mt timeout fallback say failed: {say_exc!r}", flush=True)
            except Exception as exc:  # 单句失败不阻后续
                # RC-2(2026-10-02 刀1):连接错误/装配异常此前只 print 不出声——
                # :1236 死亡(模型在盘但 server 挂)时每句都走这条=整通只有日志没有
                # 声音,与超时静默(已修的兄弟 bug)同构。与超时分支同款:目标语中性
                # 请示语兜底,绝不回放源文;译文行故意不补(诚实缺行)。say 再包 try。
                print(f"[interp] mt/say failed: {exc!r}", flush=True)
                try:
                    session.say(_mt_fail_line(target_lang))
                    _lag.done_mt(0)  # 同上:兜底句配对记账,防 _on_item 错位
                except Exception as say_exc:  # noqa: BLE001 - 兜底不出声也不阻后续
                    _lag.drop_src()
                    print(f"[interp] mt fail fallback say failed: {say_exc!r}", flush=True)
            finally:
                _mt_busy["flag"] = False
                _src_q.task_done()

    _mt_worker = asyncio.create_task(_mt_say_worker())

    # —— 投机翻译装配(prewarm-and-confirm,机制见模块级块注释) ——
    # text-only 方向(BOK_INTERP_REV_AUDIO=0 回退档)无 TTS 无可预热音频=整闸不开;kill-switch 同判。
    # (tts_cache 面函数级导入:interpret 模块头保持零 livekit 轻导入惯例。)
    from .tts_cache import frames_aiter, pcm_to_frames

    _mt_busy = {"flag": False}  # 真 MT 在途旗(投机 busy 闸消费,见 _mt_say_worker)
    _spec_on = _spec_mt_enabled() and tts_provider is not None
    if _spec_on:
        print("[interp] spec_mt armed (interim prewarm-and-confirm)", flush=True)

    async def _spec_synth_pcm(text: str) -> bytes | None:
        """投机译文 TTS 全量排干成 PCM(不进 say 队列,零播放)。

        合成走会话同一个 tts_provider(音色/模型档/语速按会话装配天然同源
        ——confirm 播放的 audio 无需再对缓存 key, PCM 即真值)。miniMax bidi
        并发合成已由 A 线垫话 backfill 实证安全;失败=slot 永不 ready,静默 MISS。"""
        if tts_provider is None:
            return None
        buf = bytearray()
        try:
            stream = tts_provider.synthesize(_speech_text(text, voice_tags))
            async with stream:
                async for ev in stream:
                    frame = getattr(ev, "frame", None)
                    data = getattr(frame, "data", None)
                    if data is not None:
                        buf.extend(data.tobytes() if isinstance(data, memoryview) else bytes(data))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[interp] INTERP_SPEC synth failed: {exc!r}", flush=True)
            return None
        return bytes(buf) or None

    async def _spec_run(span: str) -> tuple[str, bytes]:
        """投机执行体:同款 _mt_once 翻译 + TTS 排干。cancel 语义:本地 MT 车道
        aclose 经 _attach_mlx_abort 即 POST /v1/abort(server 立即弃流);
        云端 openai 车道无 abort 补丁=纯丢弃(客户端断开即终止)。"""
        ctx = _build_mt_context(_llm_instructions, list(_mt_pairs), span)
        translated = await _mt_once(llm_provider, ctx, target_lang=target_lang)
        if not translated:
            return "", b""
        pcm = await _spec_synth_pcm(translated)
        if not pcm:
            return "", b""
        return translated, pcm

    def _spec_busy_gate() -> bool:
        """真车道忙闸(闭包后绑 backlog——事件只在 session.start 后流动,装配序安全):
        FIFO 非空/真 MT 在途/背压摘译 pending 任一真=投机让路。"""
        return bool(not _src_q.empty() or _mt_busy["flag"] or backlog.source_drops_pending)

    def _spec_say_cached(final_src: str, text: str, pcm: bytes) -> None:
        """HIT 直播:held PCM 走 say(audio=frames) 零合成(qa_gate 罐头车同构)。
        先 say 后记账:say 失败不留 pending 孤儿(RC-8 同款纪律);投机轮 mt_ms=0
        (非真译时长,同兜底句口径)防 _on_item 错弹下一对。"""
        session.say(
            _speech_text(text, voice_tags),
            audio=frames_aiter(pcm_to_frames(pcm, tts_provider.sample_rate)),
        )
        _mt_latency["ms"] = 0
        _lag.note_src(final_src)
        _lag.done_mt(0)

    def _spec_enqueue(rest: str) -> None:
        """余段进正常管线(溢出摘译语义与 _on_user_input 同款)。"""
        try:
            _src_q.put_nowait(rest)
            _lag.note_src(rest)
        except asyncio.QueueFull:
            print("[interp] source queue overflow, remainder dropped(摘译)", flush=True)

    spec_ctl = _SpecMtController(
        enabled=_spec_on,
        detector=_SpecMtDetector(),
        hold=_SpecMtHold(),
        run_spec=_spec_run,
        busy_gate=_spec_busy_gate,
        say_cached=_spec_say_cached,
        enqueue=_spec_enqueue,
    )

    def _on_user_input(ev) -> None:
        # STT 句级 FINAL 是 manual 模式下唯一句子入口(空串过滤);interim 事件
        # 此前直接丢弃——投机翻译(2026-10-06)把它当 detector 输入(kill-switch
        # BOK_INTERP_SPEC_MT=0 时 on_interim/on_final 零动作=旧路径逐字节)。
        # 原文行即时落库,翻译进单消费队列;final 先过投机确认(HIT=held PCM
        # 直播+余段入队,跳过整句正常路径)。
        text = str(getattr(ev, "transcript", "") or "").strip()
        if not text:
            return
        if not getattr(ev, "is_final", False):
            spec_ctl.on_interim(text)
            return
        last_user["text"] = text
        _spawn_ledger(_add_turn(f"原文：{text}", source_lang))
        if spec_ctl.on_final(text):
            return
        try:
            _src_q.put_nowait(text)
        except asyncio.QueueFull:  # 48 句积压=极端场景,摘最新句防雪崩
            print("[interp] source queue overflow, sentence dropped(摘译)", flush=True)
            return
        # RC-8:确认入队成功才记账本(溢出摘掉的句不进 MT/不出 item=不进配对);
        # put_nowait 到 note_src 之间无 await,MT worker 不可能先消费该句。
        _lag.note_src(text)

    session.on("user_input_transcribed", _on_user_input)

    # 译文播放背压(P2):manual 之下译文堆在 say 队列——超门槛从最旧弃起
    # (已生成文本照常进字幕/落库,只弃音)。text-only 方向句句秒完播,队列
    # 天然不积,同一钩子零害。v2(2026-09-23):门槛=等待积压口径(队头在播不计),
    # 源句待译队列计入并成为摘译候选(task-9 §14:depth<3 结构性不触发→假警)。
    def _source_backlog_s() -> float:
        return sum(
            _estimate_speech_seconds(t, source_lang) for t in list(_src_q.queue)
        )

    backlog = _PlaybackBacklog(target_lang, source_backlog_s=_source_backlog_s, room=room_name)
    if backlog.enabled:
        print(f"[interp] backlog gate={backlog._max_s:g}s (追最新弃音保字,等待积压口径+源队列摘译)", flush=True)

    def _on_speech_created(ev) -> None:
        handle = getattr(ev, "speech_handle", None)
        if handle is None or not backlog.enabled:
            return
        depth, est_s, dropped = backlog.on_speech_created(handle)
        if dropped or depth > 1 or backlog.source_drops_pending:
            print(
                f"[interp] INTERP_BACKLOG depth={depth} est_ms={int(est_s * 1000)} "
                f"drop={dropped} total_dropped={backlog.dropped} "
                f"src_pending_s={int(backlog._src_backlog_s() * 1000)} "
                f"src_skips={backlog.source_drops_pending}",
                flush=True,
            )

    session.on("speech_created", _on_speech_created)

    # 房间断开 → SessionReport(真实 usage) + settle(总结/知识蒸馏/vault,服务端幂等;失败不阻塞退出)。
    async def _shutdown() -> None:
        _mt_worker.cancel()  # 排空 MT 消费协程(挂队列 get 上,不 cancel 会泄漏到下个 job)
        # 投机在途任务收线卫生(2026-10-06):cancel 在途 MT/TTS 排干,绝不外抛。
        spec_ctl.cancel("shutdown")
        # TTS 收尾(2026-09-30 对账):bidi 持久 WS 无人 aclose=worker 复用进程下
        # 连接跨通残留;text-only 方向 rev 现不装配 TTS(None 跳过)。
        if tts_provider is not None:
            try:
                await asyncio.wait_for(tts_provider.aclose(), timeout=3.0)
            except Exception as exc:  # noqa: BLE001 - 收尾失败唔阻结算
                print(f"[interp] tts aclose failed: {exc!r}", flush=True)
        try:
            await _exit_stage("mt_drain", _mt_worker, timeout_s=3.0)
        except Exception:  # noqa: BLE001 - cancel 语义由 _exit_stage 内吞
            pass
        # 在途账本行先落地再报告/结算(镜像 A 线 _close 的 _report_tasks gather):
        # job teardown 会把裸任务杀掉——原文/译文行丢失不可补。短超时防收尾卡死。
        if _ledger_tasks:
            await _exit_stage(
                "ledger_flush",
                asyncio.gather(*list(_ledger_tasks), return_exceptions=True),
                timeout_s=5.0,
            )
        # P1.c（2026-09-29 v2 spec §4）：report/settle 走 CP client（timeout=15s）
        # ——两段可挂 30s+ 超框架 10s 强杀窗（b8793951 `killing process` 实证）。
        # _exit_stage 5s 掐死：CP settle 幂等，丢了回收器兜，绝不挂死退出。
        report = None
        try:
            report = ctx.make_session_report(session)
        except Exception as exc:  # noqa: BLE001
            print(f"[interp] session report build failed: {exc!r}", flush=True)
        if report is not None:
            # P1-A(2026-09-17):body 带 worker 来源标识,CP 按方向维度收多份报告
            # (见 _session_report_payload 注释)。
            await _exit_stage(
                "report", cp.post_session_report(call_id, _session_report_payload(report.to_dict()))
            )
        # 【半场结算闸(2026-09-30 官方对账)】close_on_disconnect 默认开=先走的
        # 一端(如 me- 挂线)立刻关本方向会话并触发 shutdown——但双 worker 同房,
        # 另一端(other-)可能还在通话,此刻 settle=拿半场账本早结算。有人仍在房
        # (非 agent 身份)→ 本方向跳过 settle 留给最后离场方向;两端同时走=双方
        # 都见空房各结算一次(CP 幂等);job 被杀没人结算=既有回收器兜底。
        _humans_alive: list[str] = []
        try:
            from livekit.rtc import ParticipantState as _PState

            for _p in room.remote_participants.values():
                if str(_p.identity).startswith("agent-"):
                    continue
                if getattr(_p, "state", None) == _PState.ACTIVE:
                    _humans_alive.append(str(_p.identity))
        except Exception:  # noqa: BLE001 - 判定失败回旧行为(照结算)
            _humans_alive = []
        if _humans_alive:
            print(
                f"[interp] settle deferred (participants still active: {_humans_alive[:3]}) "
                f"— 双 worker 同房,留最后离场方向结算",
                flush=True,
            )
        else:
            _settle_res = await _exit_stage("settle", cp.settle(call_id))
            if _settle_res is not None:
                print(f"[interp] settled {call_id}", flush=True)
        await _exit_stage("cp_close", cp.aclose())

    ctx.add_shutdown_callback(_shutdown)

    # 官方姿势显式 connect:1.8 的 session.start 只把 ctx.connect 挂成后台任务
    # (agent_session.py "automatically connect"),而 RoomIO 建 legacy 转写输出时
    # 会同步访问 room.local_participant——本 worker 传了 RoomInputOptions
    # .participant_identity 必走该路径,不先 connect 必抛 "cannot access local
    # participant before connecting"、job 秒崩(同传零反应根因,2026-09-06)。
    # A 线没传 participant_identity 提前 return 才侥幸不炸。
    await ctx.connect()

    await session.start(
        room=room,
        agent=Agent(instructions=_translation_instructions(source_lang, target_lang, _glossary)),
        room_input_options=RoomInputOptions(
            participant_identity=listen_identity,
            audio_enabled=True,
            text_enabled=False,
        ),
        # 具名译文轨 trans-<目标语言>(前端可按名渲染);订阅权限白名单见下。
        # audio_enabled=False = 文字-only agent:框架跳过 TTS 推理(agent_activity
        # 的 perform_tts_inference 只在 audio_output 非空时跑),译文照常走
        # transcription 通道——对方→我方向默认只看字幕不出声(_direction_audio_enabled)。
        room_output_options=RoomOutputOptions(
            audio_enabled=_direction_audio_enabled(speaker_role),
            audio_track_name=f"trans-{target_lang}",
            # 字幕先出(Wave 2 2026-10-08):缺省 TranscriptSynchronizer 会把 agent
            # 转写拉平到语音节拍(按播放进度发词)——关掉=delta 随生成流出,前端
            # 字幕先于语音;item(账本)仍是播完落,口径不变。kill-switch 同
            # BOK_INTERP_MT_STREAM_SAY(0=回同步字幕+整句 say 旧档)。
            sync_transcription=not _mt_stream_say_enabled(),
        ),
    )

    # 「我方输出=对方听到的内容」:本 agent 的译文轨只授权 deliver 端订阅。
    # 发布者单方声明即生效;新发布轨/新加入参与者默认无权限 → 幂等重设三处触发。
    # W4b(2026-10-07 demo-quality-wave):fwd 方向(speaker_role=me)额外放开 listen
    # 侧(me- 译员座)对本 agent 译文轨(trans-<对方语言>)的**订阅权**——控制台
    # 「听对方听到的翻译」开关要拉这条轨。订阅≠自动播:放开只是权限,是否出声由
    # 前端路由决定(默认关=连订阅都不拉);deliver 端收听不受影响。rev 方向**不**
    # 对称放开——other- 订 rev 轨=对方听到自己话的译文(回声),无产品诉求。
    def _apply_track_permissions() -> None:
        try:
            lp = room.local_participant
            sids = [
                pub.sid
                for pub in lp.track_publications.values()
                if getattr(pub, "kind", None) == rtc.TrackKind.KIND_AUDIO
            ]
            if not sids:
                return
            perms = [
                rtc.ParticipantTrackPermission(
                    participant_identity=deliver_identity,
                    allow_all=False,
                    allowed_track_sids=sids,
                )
            ]
            note = ""
            if speaker_role == "me":
                perms.append(
                    rtc.ParticipantTrackPermission(
                        participant_identity=listen_identity,
                        allow_all=False,
                        allowed_track_sids=sids,
                    )
                )
                note = f" (+{listen_identity} hear-their-trans)"
            lp.set_track_subscription_permissions(
                allow_all_participants=False,
                participant_permissions=perms,
            )
            print(f"[interp] audio tracks {sids} -> {deliver_identity}{note}", flush=True)
        except Exception as exc:  # pragma: no cover - 权限失败退化为全场可听(不阻翻译)
            print(f"[interp] track permissions failed: {exc!r}", flush=True)

    _apply_track_permissions()

    @room.on("local_track_published")
    def _on_local_published(_publication, _participant) -> None:
        _apply_track_permissions()

    @room.on("participant_connected")
    def _on_participant(_participant) -> None:
        _apply_track_permissions()

    # 保持 entrypoint 存活直到会话关闭(框架在房间断开时终止 job 并跑 shutdown 回调)。
    closed = asyncio.Event()
    session.on("close", lambda _ev: closed.set())

    # 【B 线缺源遥测(2026-09-30 call-72112fd7 定案)】同房三轨齐发(服务器实锤:
    # fwd TTS + me-/other- 麦于 +16s 双双发布)而 fwd 全程零订阅零 ASR、rev 同
    # 形正常——订阅空挂在我们的代码面零痕迹,python SDK 亦无手动 subscribe 口
    # (auto_subscribe 房间级默认开,订阅面在 FFI)。本看护每 BOK_INTERP_SRC_
    # TELEMETRY_S(默认 10s)扫一次 listen 身份的音轨,分辨两态留观测行:
    #   SRC_NO_AUDIO_TRACK        —— 对端根本没发麦(未开传译/浏览器发布失败);
    #   SRC_TRACK_NOT_SUBSCRIBED  —— 音轨已发布但本 worker 订不上(订阅链断,
    #                                下一例现场直接指认服务器/FFI 哪层断)。
    # 纯遥测+订阅自愈(=0 关);fwd(音频方向)为主要受益面。
    # RC-1(2026-10-02 刀1):循环体提取为模块级 `_src_track_watch_loop`(假 room
    # 可直驱单测),此处只解析 env 旋钮 + 池化 spawn——旧闭包 heal 分支引用未定义
    # `part`(首次进 unsubscribed 态 NameError),且裸 create_task 无 done-callback
    # 静默死;入 `_watch_tasks` 池后异常必有 SRC_WATCH_ERR 日志。
    _watch_tasks: set = set()

    def _spawn_src_watch() -> None:
        if os.environ.get("BOK_INTERP_SRC_TELEMETRY", "1") != "1":
            return
        try:
            interval = float(os.environ.get("BOK_INTERP_SRC_TELEMETRY_S", "10") or 10)
        except ValueError:
            interval = 10.0
        if interval <= 0:
            interval = 10.0
        heal = os.environ.get("BOK_INTERP_SRC_HEAL", "1") == "1"
        _spawn_pooled_task(
            _src_track_watch_loop(room, listen_identity, closed, interval, heal),
            _watch_tasks,
            "SRC_WATCH_ERR",
        )

    _spawn_src_watch()
    try:
        await closed.wait()
    finally:
        pass


def run_interpreter() -> None:
    """启动一个方向的 worker:INTERP_DIRECTION=fwd(听我方/译给对方)|rev(对称)。"""
    import sys

    direction = os.environ.get("INTERP_DIRECTION", "fwd")
    if direction not in ("fwd", "rev"):
        raise SystemExit(f"INTERP_DIRECTION must be fwd or rev, got {direction!r}")
    from livekit.agents import WorkerOptions, cli

    # livekit-agents 1.7.x 的 cli.run_app 需要显式子命令(start),与 A 线 run_agent 同。
    if len(sys.argv) == 1:
        sys.argv.append("start")
    # 与 A 线 main(8081)并存须分端口:三 worker 同抢默认 8081,后绑者 Errno 48
    # 即崩(A 线输掉竞态时每通通话 "Agent did not join the room")。
    ports = {"fwd": 8082, "rev": 8083}
    # C6-2 端口单例守卫(2026-09-13):重复 spawn 良性退出(治 Errno 48 崩溃)。
    from .worker_guard import worker_port_singleton_guard

    worker_port_singleton_guard(ports[direction], f"interp-{direction}")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=f"bok-interp-{direction}",
            port=ports[direction],
            # 空闲子进程池框架缺省 prod=min(cpu,4)：每个 worker 4 个空转子进程
            # （本机 3 worker=12 个 idle 吃 ~3.1GB）。B 线通话量低，1 个足够兜
            # 冷启动；框架无 env 旋钮，只能 WorkerOptions 传参（2026-10-02 内存瘦身）。
            num_idle_processes=1,
            # load=整机 psutil.cpu_percent（见 agent.py 同款注释；B 线同暴露:
            # 共享机桌面噪音→0.7 线拒派=空房全哑）。0.99 仅整机近全饱和才拒。
            load_threshold=float(os.environ.get("BOK_WORKER_LOAD_THRESHOLD", "0.99") or 0.99),
        )
    )


if __name__ == "__main__":
    run_interpreter()
