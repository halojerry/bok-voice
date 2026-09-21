"""Qwen3-ASR 热词泄漏清洗器（确定性判据，纯函数，零 LLM）。

出处（逐字对照语义来源）：type4me（macOS 听写 App，Swift）
``Qwen3HotwordLeakSanitizer.swift`` 及其 Python sidecar 侧把热词喂进
``Qwen3-ASR`` 的 ``context=`` 的用法（§26.2-E2 移植规格）。

背景：本地 Qwen3-ASR 带 ``context=`` 热词表时，**极低内容音频**（静音/噪声/
纯语气词）会把词表本身当转写抄出来——"词表回声"。清洗器在 ASR **终稿**上做
确定性清洗：剥前导 ``Vocabulary:``/``词汇：`` 类标签 + 匹配串首的**最长连续
热词前缀**，判为泄漏则丢弃热词 dump、返回真实内容（fallback）。

两处**我们有意的偏差**（相对 type4me 原版）：

1. **纯热词 dump 整段丢弃**。原版对「剥完前缀 remainder 为空」的纯 dump
   原样保留（它测试锁定的行为）；我们改为**返回空串**——上游按空转写走
   已有静音分支（QWEN3 静音守卫/犹豫残片门同族），比把整段热词当客户话
   喂给下游安全。见 ``sanitize`` 内 ``偏差①`` 注释。
2. **fallback 必传语义**。原版有一条调用路径没传 ``fallback_text``，导致
   **单词泄漏漏网**（单词命中判据依赖 fallback）。我们不做无 fallback 的
   宽容分支：``fallback_text`` 为空时单词命中一律不算泄漏（与原版一致），
   但**本模块 docstring 要求调用方尽量传** —— 在我们侧它是必传语义
   （上一条 partial / 前置句），别走原版那条漏网路径。

消费点（本模块只提供纯函数，接线不在本文件）：将来挂在 agent 收到 ASR
**终稿**处（意图判定/QA 匹配之前，与 E1 snippet 后置轨同一落点）；热词列表
必须是**当通 ``asr_hotword_context`` 的同一份** effective 词表（内置行业词＋
运营模板词＋对象专名三层合并后、随 ``/api/start`` 下发 sidecar 的那一份），
否则"泄漏"判定与真实喂给 ASR 的偏置面对不上。``fallback_text`` 传上一条
partial 或前置句（无则传空串）。

契约与不变量：
- 输入先 ``strip``；所有返回路径都基于 strip 后的文本（ASR 转写本就无外层
  空白，故与输入逐字节一致）。
- 无热词表（清理后为空）→ 不清洗，返回 strip 后原文。
- ``hotwords`` 逐项 strip、丢空项；大小写不敏感匹配；词间可跳任意分隔符。
- ``_normalized`` = lowercase + 删除全部分隔符。
"""

from __future__ import annotations

import re

# 分隔符集（逐字移植 type4me）：Unicode 空白 ∪ 标点/符号集。
# 注意 `-` 与 `\` 是**字面量**字符（源实现写在正则字符类里，`\-` 转义后即 `-`；
# `/\-` 即 `/`、`\`、`-`）。逗号/分号/冒号/引号/括号均含全角与半角两套。
_SEPARATOR_CHARS = frozenset(
    ",，、;；:：|/\\-—_·.。.!！?？\"'“”‘’()（）[]【】<>《》"
)

# 标签表（**顺序敏感**：带冒号的长变体在前，裸词变体在后——锚定匹配逐项试，
# 长变体先命中才不会把裸词变体当标签、把冒号留进正文）。中英各一套，半角/
# 全角冒号均列。
_HOTWORD_LABELS: tuple[str, ...] = (
    "Vocabulary:",
    "Vocabulary：",
    "Vocabulary",
    "Hotwords:",
    "Hotwords：",
    "Hotwords",
    "词汇：",
    "词汇:",
    "词汇表：",
    "词汇表:",
    "热词：",
    "热词:",
    "关键词：",
    "关键词:",
)

# CJK 统一表意文字，仅 U+4E00–U+9FFF（单词泄漏判据的护栏：不碰假名/谚文/扩展区）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def contains_cjk(text: str) -> bool:
    """text 是否含 CJK 统一表意文字（U+4E00–U+9FFF）任一字符。"""
    return bool(_CJK_RE.search(text or ""))


def _is_separator(ch: str) -> bool:
    """分隔符判定：Unicode 空白 或 标点/符号集成员。"""
    return ch.isspace() or ch in _SEPARATOR_CHARS


def _skip_separators(text: str, pos: int) -> int:
    """跳过 ``text`` 中 ``pos`` 之后连续的任意分隔符，返回新下标。"""
    n = len(text)
    while pos < n and _is_separator(text[pos]):
        pos += 1
    return pos


def _normalized(text: str) -> str:
    """归一化 = lowercase + 删除全部分隔符（空白与标点一并去掉）。"""
    return "".join(ch for ch in (text or "").lower() if not _is_separator(ch))


def _clean_hotwords(hotwords: list[str] | tuple[str, ...] | None) -> list[str]:
    """逐项 strip、丢弃空项；``None``/空 → 空列表（调用方据此走"不清洗"路径）。"""
    out: list[str] = []
    for word in hotwords or []:
        item = str(word).strip()
        if item:
            out.append(item)
    return out


def _strip_label(text: str) -> tuple[str, bool]:
    """剥**串首**标签（anchored、大小写不敏感），返回 (标签后剩余, 是否命中)。

    标签表顺序敏感；命中即返回（不跳词续试别的标签）。
    """
    low = text.lower()
    for label in _HOTWORD_LABELS:
        if low.startswith(label.lower()):
            return text[len(label):], True
    return text, False


def _match_run(body_low: str, words_low: list[str], start: int) -> tuple[int, int]:
    """从 ``body_low`` **串首**按词表顺序连续匹配 ``words_low[start]``、``[start+1]``…

    契约：锚定前缀比较、大小写不敏感（调用方传已 lowercase 的 body 与词表）；
    词间可跳任意分隔符；**某词不匹配即停，不跳词续试**。
    返回 ``(命中词数, 消费字符数)``——消费字符数=最后一个命中词结束处的下标
    （不含其后跳过的分隔符）。
    """
    n = len(words_low)
    pos = 0
    count = 0
    i = start
    while i < n:
        word = words_low[i]
        if not word or not body_low.startswith(word, pos):
            break
        pos += len(word)
        count += 1
        i += 1
        if i < n:  # 还有下一个候选词：跳过分隔符再试（词间容忍任意分隔符）
            pos = _skip_separators(body_low, pos)
    return count, pos


def sanitize(
    text: str,
    hotwords: list[str] | tuple[str, ...] | None,
    fallback_text: str = "",
) -> str:
    """清洗 ASR 终稿上的热词泄漏；返回清洗后的文本（可能为空串）。

    主流程（规格见模块 docstring，与 type4me 逐条对应）：

    1. ``text`` strip 后为空 → ``""``；``hotwords`` 清理后为空 → 原样返回。
    2. 剥串首标签（命中记 ``had_label``），命中后跳过分隔符。
    3. **最长连续热词前缀**：所有起始下标都试，得分先比命中词数、并列比消费
       字符数（取最优；同分保留更靠前的起始下标）。
    4. ``remainder`` = 前缀之后跳过分隔符的剩余。
    5. 泄漏判据：``had_label`` 或 命中词数 ≥2；**单词命中**需 ``fallback_text``
       非空 且 consumed 不整段是 fallback 的前缀 且 (remainder 与 fallback
       归一化后相等或互为后缀) 且 consumed 含 CJK。
    6. 命中泄漏：``remainder`` 非空 → 归一化后与 fallback 相等/互为后缀则返回
       ``remainder``，否则返回 ``fallback_text``；``remainder`` 为空 →
       **返回 ""**（偏差①，见下）。

    偏差①（有意）：type4me 原版对纯热词 dump（remainder 空）**原样保留**，
    我们改为**整段丢弃**（返回 ``""``），由上游按空转写走静音分支。

    偏差②（有意）：本模块把 ``fallback_text`` 当**必传语义**（上一条 partial /
    前置句）；调用方应尽量传。为空时单词命中一律不算泄漏（与原版一致），
    但别依赖这条宽容分支——原版正因一条调用路径没传 fallback 而漏网。
    """
    trimmed = str(text or "").strip()
    if not trimmed:
        return ""

    words = _clean_hotwords(hotwords)
    if not words:
        return trimmed  # 无词表：不清洗

    fallback = str(fallback_text or "")

    # 1) 剥前导标签（anchored、大小写不敏感），命中后跳过分隔符。
    body, had_label = _strip_label(trimmed)
    if had_label:
        body = body[_skip_separators(body, 0):]

    body_low = body.lower()
    words_low = [w.lower() for w in words]

    # 2) 最长连续热词前缀：所有起始下标都试，先比词数、并列比消费长度。
    best_count = 0
    best_consumed = 0
    for start in range(len(words_low)):
        count, consumed = _match_run(body_low, words_low, start)
        if count > best_count or (count == best_count and consumed > best_consumed):
            best_count = count
            best_consumed = consumed

    consumed_text = body[:best_consumed]
    remainder = body[_skip_separators(body, best_consumed):]

    # 3) 泄漏判据。
    if had_label or best_count >= 2:
        leaked = True
    elif best_count == 1:
        norm_fallback = _normalized(fallback)
        norm_consumed = _normalized(consumed_text)
        norm_remainder = _normalized(remainder)
        leaked = (
            bool(fallback)
            # consumed 若整段是 fallback 的前缀，则热词是客户真实说出的
            # （fallback 本身就以其开头）→ 不算泄漏。
            and not norm_fallback.startswith(norm_consumed)
            and (
                norm_remainder == norm_fallback
                or norm_remainder.endswith(norm_fallback)
                or norm_fallback.endswith(norm_remainder)
            )
            and contains_cjk(consumed_text)
        )
    else:
        leaked = False

    if not leaked:
        return trimmed

    # 4) 命中泄漏：remainder 空 = 纯热词 dump → 偏差①：整段丢弃（空转写）。
    if not remainder:
        return ""

    norm_remainder = _normalized(remainder)
    norm_fallback = _normalized(fallback)
    if (
        norm_remainder == norm_fallback
        or norm_remainder.endswith(norm_fallback)
        or norm_fallback.endswith(norm_remainder)
    ):
        return remainder
    return fallback
