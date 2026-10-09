"""interp_lite 装配纯函数：翻译指令（官方 19 标签版）+ 旧线纯函数复出口。

旧线单源复用（不双轨）：``interpret._norm_lang`` / ``parse_glossary`` /
``glossary_block`` / ``glossary_source_terms``——术语表解析/渲染/热词侧语义与
旧线逐字节同源（本模块只做 re-export，改动回旧线改）。
"""

from __future__ import annotations

from ..interpret import (  # noqa: F401 - 单源复出口
    _norm_lang as norm_lang,
)
from ..interpret import (
    glossary_block,  # noqa: F401
    glossary_source_terms,  # noqa: F401
    parse_glossary,  # noqa: F401
)
from .voice_tags import OFFICIAL_VOICE_TAGS

_LANG_NAMES = {
    "zh": "Mandarin Chinese",
    "cantonese": "Hong Kong Cantonese (港式粤语口語,繁體)",
    "en": "natural spoken English",
    "de": "German",
    "fr": "French",
    "ja": "Japanese",
    "pt": "Portuguese",
}


def _tag_list_line() -> str:
    tags = " ".join(f"({t})" for t in sorted(OFFICIAL_VOICE_TAGS))
    return f"Allowed tags (the only ones, exactly this spelling): {tags}"


def build_instructions(src: str, tgt: str, glossary: str = "") -> str:
    """同传 system 指令（interp_lite 版）：官方 19 标签进提示词=生成侧。

    与旧线 ``_translation_instructions`` 的分野只在语气词条款：旧线「4 标签示例 +
    事后确定性转换」；lite「枚举官方全集 + TagGate 校验」（计划档 §5）。其余条款
    （只输出译文/口语短句/已是目标语则原样/ASR 噪声纠错/粤语繁体规则）语义同源。
    """
    s = _LANG_NAMES.get(src, src)
    t = _LANG_NAMES.get(tgt, tgt)
    lines = [
        "You are a professional simultaneous-interpretation engine on a live phone call.",
        f"Translate EVERY user utterance from {s} into {t}.",
        "Rules:",
        "- Output ONLY the translation; no explanations, no quotes, no notes, never the source language.",
        "- Keep names, numbers and technical terms where sensible; preserve the original tone (casual/courteous).",
        "- Speak like a live interpreter: short spoken sentences, one utterance at a time, no summaries.",
        "- If the utterance is already in the target language, output it unchanged.",
        # 语气词（官方 19 标签，docs-first）：每句至多一枚、只标真实发声、语境自然嵌入。
        "- If the speaker laughs, giggles, sighs, coughs, gasps and similar genuine vocal sounds, render the "
        "sound as a bracket tag in place of the sound word — at most one tag per sentence, only for genuine "
        "vocal sounds, never invent them.",
        _tag_list_line(),
        # ASR 噪声纠错（旧线 Wave 1 同源条款：c3ed3ef3 实证云端 LLM 会把碎噪声脑补成句）。
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
