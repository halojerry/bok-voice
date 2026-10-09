"""interp_lite 流式切块器（纯函数）：话段视图增量 → 翻译块切点。

call-e376e7a9 翻案配套（v2 话段连续流）+ 同窗 Ethan 定调修订：
「给 LLM 的切分按 ，。！？，别乱切」——MT 单位=标点界（译出来的句子才自然）；
「长讲话要一条连续输出」——由管线保证（一块话段一条 TTS 流，切块只决定喂 MT
的源文本分片，绝不开/关播放流）。

切点策略（门槛用内容字数：标点/空白不计）：
1. ``flush``（话段收尾）→ 全出；
2. 窗口 ``[0, max_cut]`` 内**标点优先**：句末族（。！？…；;.）从后往前找（切其后、
   含标点），无句末再找逗号族（，,、：:）；门槛 = 首块 ``min_first`` / 常规 ``min_reg``；
3. 首块例外（抢首声 ≤1.5s 预算）：内容 ≥ ``min_first`` 即切，不等标点——唯一允许
   的非标点切点，代价限定在开头几字（首声是硬指标，头部译文质量让步）；
4. 保险丝 ``max_cut``：无标点长跑（正常语速下碰不到）防死锁；ASCII 字母数字 run 不劈。
"""

from __future__ import annotations

_SENT_PUNCT = "。！？!?…；;."
_COMMA_PUNCT = "，,、：:"
_PUNCT = _SENT_PUNCT + _COMMA_PUNCT


def content_len(s: str) -> int:
    """内容字数（标点/空白不计）——门槛口径单点。"""
    return sum(1 for c in s if not c.isspace() and c not in _PUNCT)


def common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _ascii_run_boundary(s: str, cut: int) -> int:
    """切点落在 ASCII 字母数字 run 内 → 回退到 run 起点（整 run 留给下一块）。"""
    if cut <= 0 or cut >= len(s):
        return max(0, min(cut, len(s)))
    prev, cur = s[cut - 1], s[cut]
    if not (prev.isascii() and prev.isalnum() and cur.isascii() and cur.isalnum()):
        return cut
    i = cut
    while i > 0 and s[i - 1].isascii() and s[i - 1].isalnum():
        i -= 1
    return i if i > 0 else cut  # run 从 0 起（整段一个 run）：保原切点


def pick_cut(
    avail: str,
    *,
    first: bool = False,
    flush: bool = False,
    min_first: int = 4,
    min_reg: int = 4,
    max_cut: int = 24,
) -> int:
    """返回从 ``avail`` 头部切走的字符数（0=还不到量）。"""
    if not avail:
        return 0
    if flush:
        return len(avail)
    lo = min_first if first else min_reg
    window = avail[: max_cut + 1]  # +1：窗口边缘刚到的标点也算（切含标点一行出）
    for fam in (_SENT_PUNCT, _COMMA_PUNCT):
        for i in range(len(window) - 1, -1, -1):
            if window[i] in fam and content_len(window[: i + 1]) >= lo:
                return i + 1
    if first and content_len(avail) >= min_first:
        return len(avail)
    if len(avail) >= max_cut:
        return _ascii_run_boundary(avail, max_cut)
    return 0
