"""改口检测纯函数（E4，2026-09-20）：显式改口区间 + 语义否定计数单点。

出处：type4me（macOS 听写 App，Swift）``CorrectionIntentAnalysis.swift`` 的
``analyze`` 源码级移植——移植规格见
``docs/superpowers/plans/2026-09-21-a-line-speed-asr-decision-verification.md``
§26.2-E4（抄什么/改什么/别抄什么）与 §26.3 速查参数表。

两条显式改口正则（与源实现逐字对齐）：

- ``_EXPLICIT_WITH_SUFFIX``：标记词 ``不对|哦不|改口|算了|重说|i mean|sorry``
  ＋**可选后缀** ``(\\s*[，,。.]?\\s*(?:改成|换成|应该是|是))?``（把
  「哦不，是 X」「改口，换成 Y」整段吃进同一区间）；
- ``_EXPLICIT_BARE``：**独立后续词** ``改成|换成|应该是``（``(?!不要)`` 拦
  「改成不要…」——后缀位的否定）。

两者均带 ``(?<!不)(?<!别)`` 负向后行（拦「不改成」「别改成」）。命中后再做
**后置过滤**：取区间起点前最多 4 个字符，若以 ``不要`` / ``不能`` / ``别``
结尾则丢弃该区间（拦前缀位的否定「不要改成 X」）。

**关键设计事实（口径，务必读懂）**：本模块词表里**根本没有「不是」**——所以
「不是A，是B」这类裸对比**天然不触发任何显式改口区间**。这个"豁免"是**词表
缺席的自然结果，不是一条显式分支**（移植时不要好心补上「不是」）。

口径取 type4me 的 **IntelliSense（智能感知）模板**：它**不**把「不是A是B」当
改口。type4me 另有一个 **Voice Polish（离线语音润色）模板**，语义**相反**（把
「不是A，是B」折叠成 B、取后值）。**本模块只做 IntelliSense 口径**；离线润色面
若需要相反语义，必须**另加参数化或另写模块**，不在本文件内实现、不在此处埋
隐式开关。

``semantic_negation_counts``：先**删除**（替换为空串）三类**元话语**（改口/致歉
词、正反问句标记、让步连词——它们内部的「不」不是语义否定，留着会被误计），
再按**固定顺序六分类消费**：prohibition → inability → absence → contradiction →
never → general。每类**在「剩余串」上计数**后**把命中替换为空格**——这样兜底类
``general``（裸「不」）绝不会把已被更高优先类消费掉的否定重复计数一遍。

**纯检测、零改写**：本模块绝不修改输入文本的语义，也绝不碰数字（A 线「数字零
降级」铁律同族）——``span_after_last_correction`` 只从原文切出「改口之后」那一段
（边缘剥空白/标点，数字与内容逐字原样返回），供调用方自己决定怎么用。

消费点（本模块只出纯函数，**接线不在本文件**，留给后续实施）：A 线
``FlowController`` 的 CONFIRM/UNCLEAR 判定（``contains_explicit_correction``）与
WhatsApp 累积轮（``span_after_last_correction`` 取改口后的号码段）。落在 agent 收到
客户轮文本、流程判定之前。

术语：语言相关字面量只用 ``zh`` / ``cantonese`` / ``en``（AGENTS.md 术语铁律）。
"""

from __future__ import annotations

import re

__all__ = [
    "contains_explicit_correction",
    "explicit_correction_ranges",
    "semantic_negation_counts",
    "span_after_last_correction",
]


# ---- 显式改口正则（§26.2-E4，逐字移植） ----

# 标记词 + 可选后缀。后缀把「哦不，是 X」「改口，换成 Y」整段吃进同一区间。
_EXPLICIT_WITH_SUFFIX = re.compile(
    r"(?<!不)(?<!别)"
    r"(?:不对|哦不|改口|算了|重说|i\s+mean|sorry)"
    r"(?:\s*[，,。.]?\s*(?:改成|换成|应该是|是))?",
    re.IGNORECASE,
)

# 独立后续词（没有前置标记也足以成区间）——(?!不要) 拦「改成不要…」。
_EXPLICIT_BARE = re.compile(
    r"(?<!不)(?<!别)(?:改成|换成|应该是)(?!不要)",
    re.IGNORECASE,
)

# 后置过滤：区间起点前最多 4 个字符若以否定词结尾，则该区间是「不要改成 X」这种
# 被否定的动作、不是改口。与 (?!不要) 形成双保险：前者拦前缀位否定、后者拦后缀位。
_PREFIX_WINDOW = 4
_NEGATED_PREFIX_TAIL: tuple[str, ...] = ("不要", "不能", "别")

# span_after 取后值时剥掉的**边缘**空白与标点（只剥标点/空白，**不碰数字**）。
_EDGE_PUNCT = " \t\r\n，,。.、；;：:！!？?…~～\u3000"


# ---- 语义否定计数（§26.2-E4） ----

# 元话语（先删，避免疑问/让步词里的「不」被当语义否定误计）：
# 1) 改口/致歉类（与显式改口标记同族，删掉免得算进否定）；
# 2) 正反问句标记（能不能/是不是/要不要…）；
# 3) 让步连词（不过/不仅/不但…）。
_METADISCOURSE: tuple[re.Pattern[str], ...] = (
    re.compile(r"不对|哦不|i\s+mean|sorry", re.IGNORECASE),
    re.compile(r"能不能|可不可以|是不是|要不要|有没有|是否", re.IGNORECASE),
    re.compile(r"不过|不仅|不但|不论|不管", re.IGNORECASE),
)

# 六分类消费：顺序即优先级；每类计完即把命中替换为空格（防兜底类重复计数）。
# 有意偏差：inability 在源规格词表（不能|无法|不可|can't|cannot）之外补入「不行」——
# locked 判例「不过不行」要求 inability=1（原词表三词全不含「不行」），且「不行」本
# 就是中文里最典型的「不能」义表达，补入无语义风险。见同轮报告「偏差」节。
_NEGATION_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "prohibition",
        re.compile(r"不要|别|请勿|不得|do\s+not|don't|must\s+not", re.IGNORECASE),
    ),
    ("inability", re.compile(r"不能|不行|无法|不可|can't|cannot", re.IGNORECASE)),
    (
        "absence",
        re.compile(r"尚未|没有|未|没|not\s+yet|didn't|hasn't|haven't", re.IGNORECASE),
    ),
    ("contradiction", re.compile(r"并非|不是|\bnot\b|\bno\b", re.IGNORECASE)),
    ("never", re.compile(r"从不|绝不|never", re.IGNORECASE)),
    ("general", re.compile(r"不")),
)


def explicit_correction_ranges(text: str) -> list[tuple[int, int]]:
    """返回显式改口区间 ``[start, end)`` 列表（已过否定后置过滤，按起点升序）。

    两条正则的命中合并后逐条做前缀否定过滤（起点前 ≤4 字符以 不要/不能/别 结尾即
    丢弃）。**注意「不是A是B」不出区间**——词表里没有「不是」，见模块 docstring。

    纯函数：不改输入、不回写、零副作用。
    """
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    for pattern in (_EXPLICIT_WITH_SUFFIX, _EXPLICIT_BARE):
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            prefix = text[max(0, start - _PREFIX_WINDOW) : start]
            if prefix.endswith(_NEGATED_PREFIX_TAIL):
                continue
            spans.append((start, end))
    spans.sort(key=lambda span: (span[0], span[1]))
    return spans


def contains_explicit_correction(text: str) -> bool:
    """文本是否含显式改口（= ``explicit_correction_ranges`` 非空）。

    供 CONFIRM/UNCLEAR 判定消费：有显式改口标记的客户轮不再按普通应承处理。
    """
    return bool(explicit_correction_ranges(text))


def span_after_last_correction(text: str) -> str | None:
    """最后一个显式改口区间**之后**的原文段（剥边缘空白/标点后为空则 ``None``）。

    「改口取后值」的确定性子面：WhatsApp 报号改口（取改口后重报的号码段）、
    CONFIRM 改口（取改口后的确认值）。**只切原文段、零改写**——返回值必为输入文本
    的一个连续子串（数字逐字原样，绝不归一/替换）。
    """
    spans = explicit_correction_ranges(text)
    if not spans:
        return None
    _start, end = max(spans, key=lambda span: (span[0], span[1]))
    tail = text[end:].strip(_EDGE_PUNCT)
    return tail or None


def semantic_negation_counts(text: str) -> dict[str, int]:
    """六类语义否定计数（顺序消费、每类命中后置空格防重复计数）。

    返回固定六键 ``prohibition`` / ``inability`` / ``absence`` / ``contradiction``
    / ``never`` / ``general``（缺类=0）。先删三类元话语再分类，故「能不能不要这样」
    的 prohibition 只计 1（疑问标记「能不能」已先被删，不落进 general）。
    """
    remaining = text or ""
    for pattern in _METADISCOURSE:
        remaining = pattern.sub("", remaining)
    counts: dict[str, int] = {}
    for name, pattern in _NEGATION_CLASSES:
        counts[name] = len(pattern.findall(remaining))
        remaining = pattern.sub(" ", remaining)
    return counts
