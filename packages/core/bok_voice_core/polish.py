"""离线润色面（E7，2026-09-21）：ASR 转写的**确定性精炼**（纯函数，零 LLM）。

出处：type4me（macOS 听写 App）``AppState.swift`` 的 ``formalWritingPromptTemplate``
（Voice Polish 模板）—— 移植规格见
``docs/superpowers/plans/2026-09-21-a-line-speed-asr-decision-verification.md``
§26.2-E7 与 §26.3 速查参数表。

**落点 = 挂断后的异步面，绝不进实时轮**（§26.2-E7 明文）。消费点：纪要生成输入、
QA 挖掘（``mine_qa_pairs``）输入、L-① 漏网轮挖掘输入——脏转写是 qa 簇不纯的主因之一。
延迟不敏感，故这里只做**机制性**清洗，不做 LLM 改写（LLM 面另见
``scripts/probe_polish_model.py`` 的 A/B）。

模板要求的五类变换（逐条落地，全确定性）：

1. **删口水词**（``remove_fillers``）：中文停顿/语气残片（呃/嗯/唔/啊…）当成**独立段**
   才删（左右两侧皆为边界）；嵌在词内的「啊」（好啊）不动。英文整词口水词同理。
2. **删重复**（``collapse_repetitions``）：**紧邻**或**逗号分隔**的同一短段折叠一次；
   **感叹号分隔的重复是刻意强调，永不折叠**（规格「保有意强调如签字！签字！签字！」）；
   单字叠词（看看/想想）是汉语构词、不是口水，永不折叠。
3. **丢弃半句废弃**（``fold_self_correction``）：显式改口标记（不对/哦不/改口/重说/算了）
   之后的**后值**为准，之前那半句（被废弃的）整段丢弃。
4. **改口取后值**（同 ``fold_self_correction``）：折叠 ``不是A，是B`` → ``B``。
   **这是 Polish 口径**——与 ``correction_intent``（IntelliSense 口径）**语义相反**：
   后者词表里根本没有「不是」，故「不是A是B」在改口检测里天然不触发（豁免）。两模板
   语义相反是有意的，见 §26.2-E4 末段；本模块只在离线面走 Polish 口径。**落地时以
   ``不是X，是Y`` 的对比结构为条件**（不是见到「不是」就切）——否则「我今天不是来投诉的」
   这类合法否定的前半句会被整段丢。
5. **口语数字规范化**（``normalize_numbers``）：``两千三百→2300``、
   ``百分之十五→15%``、``三点半→3:30``。

数字铁律（AGENTS.md「数字零降级」，与 ``snippets`` 的数字守卫同族）——**本模块的最高
优先级纪律**。两条结构性防线：

- **结构防线**：规范化只碰**汉语数词**（含十/百/千/万/亿的数值式、``百分之X``、``X点Y``），
  **绝不碰 ASCII/全角数字串本身**。单字连读报号（``三七七八九零``，无级量词）不构成
  数值式，天然不被匹配——报号串结构性地安全。
- **语境防线**：``_number_report_spans`` 标出「报号语境」区间（≥4 位数字串、≥4 连单字
  中文数字、以及紧跟在号码关键词后 10 字内的数词），落在区间内的候选**一律不改**。
  防线是**区间级**（护住那一段报号），不是整句级——这样「我买了两千三百块，单号12345」
  里的 12345 与两千三百各得其所（前者不动、后者规范）。

**规格冲突的裁决（铁律优先）**：规格要求「口语数字规范化」，铁律要求「数字零降级」。
二者在**报号串**上真冲突。裁决 = **铁律赢**：报号串（结构 + 语境两道防线）一律不归一，
只有非报号的数值式才规范化。本裁决是**被测试钉住的属性**（``tests/test_polish.py``）。

**与 E3 Guard 的口径冲突（参数化解决）**：本模板会（a）折叠「不是A是B」、（b）**新增**
阿拉伯数字（2300/15%/3:30）——正是 Guard 的两类风险点。§26.2-E7 的定案是
**参数化**：润色面走 ``GuardPolicy(exempt_digit_addition=True)``——豁免「数字新增」
（关掉编造数字事实判定、扩张预算剥数字计长），**保留硬保护 token 检查**（URL/邮箱/
路径/数字串/星期丢失照拒）与语言漂移/硬否定/敏感新增/空输出全部照拒。于是：

- 折叠「不是A是B」：``contradiction`` 类（「不是」）在 Guard 里**只告警不拒**，放行；
- 但若折叠把**真否定**（``prohibition`` 的「不要」/``never``）整段丢掉 → Guard 硬拒 → 回退原文。
  ``polish_text`` 直接把这个契约跑在出口上（拒绝即回退原文），并留档在 ``PolishResult.guard``。

术语：语言相关字面量只用 ``zh`` / ``cantonese`` / ``en``（AGENTS.md 术语铁律）；本模块
的数字面只认字符与词素，不写语言字面量。

纯函数：零 I/O、零全局状态、零时间依赖；同输入恒同输出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

from .output_guard import GuardPolicy, GuardResult, guard_output

__all__ = [
    "CN_NUMERAL_CHARS",
    "PolishPolicy",
    "PolishResult",
    "collapse_repetitions",
    "fold_self_correction",
    "is_number_reporting",
    "normalize_numbers",
    "polish_text",
    "remove_fillers",
]


def _as_text(value: object) -> str:
    return "" if value is None else str(value)


# ---- 数字面字符集（AGENTS.md「数字零降级」的判定面） ----

# 中文数词字符：合计十一百千万亿 + 占位零/〇（不含「幺」——只用于报号连读，见防线）。
CN_NUMERAL_CHARS = "零〇一二两三四五六七八九十百千万亿"
# 单字连读报号用字（含「幺」）——≥4 连即判报号串。
_SPOKEN_DIGIT_CHARS = "零〇一二三四五六七八九幺"

_ASCII_OR_FULLWIDTH_DIGIT = re.compile(r"[0-9０-９]")
# 报号数字串：数字 + 常见分隔（空格/连字符/点/冒号/斜杠/顿号）。
_DIGIT_RUN_RE = re.compile(r"[0-9０-９]+(?:[ ,、.\-:：/][0-9０-９]+)*")
# 连读报号：≥4 个单字数字（三七七八九零 / 一二二三三四四五）。
_SPOKEN_DIGIT_RUN_RE = re.compile(f"[{_SPOKEN_DIGIT_CHARS}]{{4,}}")

# 号码关键词（报号语境的旁证；只在数词**之前**的窗口里找，与「单号是XXXX」同向）。
_NUMBER_REPORT_KEYWORDS: tuple[str, ...] = (
    "单号",
    "运单",
    "订单",
    "快递单",
    "包裹号",
    "手机",
    "电话",
    "号码",
    "手机号",
    "账号",
    "账户",
    "card",
    "whatsapp",
    "微信",
    "身份证",
    "卡号",
    "尾号",
    "编号",
)
_KEYWORD_WINDOW = 10
# 子句边界（关键词判定不跨句读——否则「单号12345，买了两千三百块」里隔句的 单号
# 会把后面那个正常的数值式也误保护掉）。
_CLAUSE_SPLIT_RE = re.compile(r"[，,。.、；;：:！!？?\s]+")


def _digit_run_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _DIGIT_RUN_RE.finditer(text)]


def _spoken_digit_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _SPOKEN_DIGIT_RUN_RE.finditer(text)]


def _number_report_spans(text: str) -> list[tuple[int, int]]:
    """报号语境区间（数字串 + 连读单字串）——规范化候选落在其中一律不改。"""
    return _digit_run_spans(text) + _spoken_digit_spans(text)


def _in_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _keyword_protected(text: str, start: int) -> bool:
    """数词匹配点**之前**同一子句（不跨句读）且 ``_KEYWORD_WINDOW`` 字内是否有关键词。"""
    head = text[max(0, start - _KEYWORD_WINDOW) : start]
    head = _CLAUSE_SPLIT_RE.split(head)[-1]
    return any(keyword in head.lower() for keyword in _NUMBER_REPORT_KEYWORDS)


def is_number_reporting(text: str) -> bool:
    """文本是否构成「报号语境」（供调用方在入口处整句短路，可选）。

    判据（任一）：≥4 位数字串；≥4 连单字中文数字；或号码关键词 + ≥2 连数字。
    """
    body = _as_text(text)
    if re.search(r"[0-9０-９]{4,}", body):
        return True
    if _SPOKEN_DIGIT_RUN_RE.search(body):
        return True
    if any(keyword in body.lower() for keyword in _NUMBER_REPORT_KEYWORDS):
        if re.search(f"[0-9０-９]{{2,}}|[{_SPOKEN_DIGIT_CHARS}]{{2,}}", body):
            return True
    return False


# ---- 汉语数词解析（§26.2-E7 数字格式要求） ----

_CN_DIGIT_VALUE: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNIT_VALUE: dict[str, int] = {"十": 10, "百": 100, "千": 1000}
_CN_SECTION_VALUE: dict[str, int] = {"万": 10000, "亿": 100000000}

# 只含级量词（十百千万亿）的匹配才算「数值式」——纯单字连读报号不匹配。
_CN_MAGNITUDE_RE = re.compile(r"[十百千万亿]")

# 成语/惯用形黑名单：匹配段整段相等即跳过（否则「万一→10001」「千万→10000000」
# 「十万火急→100000火急」）。只收**会与数词形态撞车**的段（纯单字撞车已被
# 「段长 ≥2 + 含级量词」两条结构性排除，见 ``normalize_numbers``）。
_CN_IDIOMS: frozenset[str] = frozenset(
    {
        "万一",
        "千万",
        "十分",
        "一点",
        "有点",
        "一起",
        "一样",
        "一直",
        "一定",
        "一般",
        "一些",
        "一半",
        "一下",
        "一会儿",
        "一阵",
        "一齐",
        "一同",
        "一心",
        "一概",
        "一举",
        "百般",
        "百分",
        "十万",
        "百万",
        "千百万",
        "一五一十",
        "百八十",
        "二三十",
        "三四十",
        "四五十",
        "五六十",
        "六七十",
        "七八十",
        "八九十",
    }
)

_CN_NUM_RUN_RE = re.compile(f"[{CN_NUMERAL_CHARS}]+")
_PERCENT_RE = re.compile(f"百分之([{CN_NUMERAL_CHARS}]+)")
# 时刻：X点半 / X点一刻 / X点Y分（无时间单位后缀的裸「X点」不匹配——「一点」是副词）。
_TIME_RE = re.compile(
    f"([一二两三四五六七八九十]{{1,3}})点(半|一刻|([一二三四五六七八九十]{{1,3}})分)"
)


def _parse_cn_numeral(text: str) -> int | None:
    """汉语数词串 → 整数；含非法字符返回 ``None``。

    「十五」=10+5、``十`` 位缺省按 1（十→10）、``零`` 占位不参与折算。
    """
    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGIT_VALUE:
            number = _CN_DIGIT_VALUE[ch]
        elif ch in _CN_UNIT_VALUE:
            unit = _CN_UNIT_VALUE[ch]
            if number == 0:
                number = 1  # 「十五」的十 = 1×10
            section += number * unit
            number = 0
        elif ch in _CN_SECTION_VALUE:
            section = (section + number) * _CN_SECTION_VALUE[ch]
            total += section
            section = 0
            number = 0
        else:
            return None
    return total + section + number


# ---- 口水词 ----

# 中文停顿/语气残片（当独立段才删，见 remove_fillers）。
_CN_FILLER_CHARS = "呃嗯唔啊诶唉哦噢呀嘛"
_CN_FILLER_RUN_RE = re.compile(f"[{_CN_FILLER_CHARS}]+")
# 英文整词口水词（保守集：不含 like / you know 这类易伤语义的）。
_EN_FILLER_RE = re.compile(
    r"(?i)(?<![A-Za-z])(?:um+|uh+|erm?|hmm+|mm+|ah+|oh+)(?![A-Za-z])"
)
# 「独立段」判定用的边界：Unicode 空白 或 标点。CJK 字与字母都算「内容」（不定界）。
_BOUNDARY_PUNCT = set(" \t\r\n\u3000，,。.、；;：:！!？?…~～\"'“”‘’()（）[]【】<>《》·—-/\\")


def _is_boundary(text: str, index: int) -> bool:
    """``index`` 处是否为边界（越界=边界；空白/标点=边界；CJK/字母/数字=内容）。"""
    if index < 0 or index >= len(text):
        return True
    return text[index] in _BOUNDARY_PUNCT


def remove_fillers(text: str) -> str:
    """删口水词：中文残片仅当**左右皆为边界**才删（「好啊」内嵌的「啊」保留）；
    英文整词口水词（前后紧邻字母不删）。"""
    body = _as_text(text)
    out: list[str] = []
    last = 0
    for match in _CN_FILLER_RUN_RE.finditer(body):
        if _is_boundary(body, match.start() - 1) and _is_boundary(body, match.end()):
            out.append(body[last : match.start()])
            last = match.end()
    out.append(body[last:])
    joined = "".join(out)
    joined = _EN_FILLER_RE.sub(" ", joined)
    return joined


# ---- 重复折叠 ----

# 紧邻重复（段长 ≥2——单字叠词是构词、不折）；笑声/拟声（哈/嘿/嘻/呵）不折。
# 两侧 ASCII 字母/数字边界（2026-09-21 批次 6 修）：`exceeded → exceed`、
# `preceded → preced` 这类**把英文词尾当重复折掉**的破坏由这两枚 lookaround 拦住
# （真库实测逼出；`_REPEAT_TRIPLE_RE` 同款见下）。
_REPEAT_ADJACENT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?![哈嘿嘻呵])([^\s，,。！!？?、；;：:]{2,8})\1(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 逗号分隔的重复（段长 ≥2）。IGNORECASE 让 "Hello, hello" 也折叠（中文无大小写，无影响）。
_REPEAT_COMMA_RE = re.compile(
    r"(?<![A-Za-z0-9])(?![哈嘿嘻呵])([^\s，,。！!？?、；;：:]{2,8})[，,、]\s*\1(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 三连及以上的同一单字（我我我 → 我）；汉语双字叠词（看看/想想/谢谢）是构词、**不折**，
# 故只收 ≥3 连；笑声/拟声（哈/嘿/嘻/呵）不折。
#
# ASCII 字母/数字同样不折（2026-09-21 批次 6 修，真库实测逼出）：`MT3000 → MT30`、
# `soak111 → soak1` 这类**吃掉号码一位**的破坏原规则照做，而出 Guard **拦不住**
# ——Guard 的硬保护 token 面只认「两侧皆非字母数字」的整数串，粘在字母上的数字串
# 不产出保护 token。**数字是数据不是口水词**，故 ASCII run 一律不折
# （判例见 tests/test_polish.py 的 test_ascii_runs_never_collapsed）。
_REPEAT_TRIPLE_RE = re.compile(r"(?<![A-Za-z0-9])(?![哈嘿嘻呵A-Za-z0-9])(.)\1{2,}", re.DOTALL)


def collapse_repetitions(text: str) -> str:
    """折叠紧邻/逗号分隔的短段重复、≥3 连的同一单字；感叹号分隔的强调重复与
    汉语双字叠词（看看/想想/谢谢）永不折叠。

    反复应用至稳定（「我我我」类连叠交给多轮折叠）。
    """
    out = _as_text(text)
    prev: str | None = None
    while prev != out:
        prev = out
        out = _REPEAT_ADJACENT_RE.sub(lambda m: m.group(1), out)
        out = _REPEAT_COMMA_RE.sub(lambda m: m.group(1), out)
        out = _REPEAT_TRIPLE_RE.sub(lambda m: m.group(1), out)
    return out


# ---- 改口取后值 / 丢弃废弃半句（Polish 口径） ----

# 对比结构：不是 X，(而)是 Y → Y（X 限 40 字、不含句末标点，防跨句乱折）。
_NOT_IS_RE = re.compile(r"不是(?P<gone>[^，,。！!？?；;\n]{1,40})[，,]\s*(?:而)?是(?P<keep>.+)")
# 显式改口标记（Polish 口径）：标记 + 标点之后的后值。
_MARKER_RE = re.compile(r"(?<![不别])(?:不对|哦不|改口|重说|算了)\s*[，,。.、]\s*")


def fold_self_correction(text: str) -> str:
    """改口取后值 + 丢弃被废弃的半句：

    1. ``不是X，(而)是Y`` → ``Y``（对比结构；无该结构不动——防误伤合法否定）；
    2. 显式标记（不对/哦不/改口/重说/算了）+ 标点 → 标记之后的后值（前缀整段丢弃），
       后值若以 ``是`` 起头（≠「是不是」）则一并剥掉连接词。

    取**最靠后**的切点（后值优先），切点之后为空则不动（标记是句末语气时保留原文）。
    """
    body = _as_text(text)
    cut = 0
    marker_tail: str | None = None
    for match in _NOT_IS_RE.finditer(body):
        cut = max(cut, match.start("keep"))
    for match in _MARKER_RE.finditer(body):
        tail = body[match.end() :]
        if tail.strip():
            if match.end() > cut:
                cut = match.end()
                marker_tail = tail
    if cut <= 0:
        return body
    if marker_tail is not None:
        stripped = marker_tail.strip()
        connector = re.match(r"而是|是(?!不)", stripped)
        if connector:
            stripped = stripped[connector.end() :].strip()
        return stripped
    return body[cut:].strip()


# ---- 口语数字规范化（带报号防线） ----

def _norm_time(match: re.Match[str]) -> str | None:
    hour_cn = match.group(1)
    tail = match.group(2)
    hour = _parse_cn_numeral(hour_cn)
    if hour is None or not 1 <= hour <= 24:
        return None
    if tail == "半":
        minute = 30
    elif tail == "一刻":
        minute = 15
    else:
        minute_cn = match.group(3) or ""
        parsed = _parse_cn_numeral(minute_cn)
        if parsed is None or not 0 <= parsed <= 59:
            return None
        minute = parsed
    return f"{hour}:{minute:02d}"


def _collect_edits(text: str) -> list[tuple[int, int, str]]:
    """收集所有可接受的替换 ``(start, end, replacement)``，按优先级解析重叠。

    优先级：百分比 > 时刻 > 数值式（更具体的形态先占位，数值式不覆盖它们）。
    """
    protected = _number_report_spans(text)
    accepted: list[tuple[int, int, str]] = []

    def _add(start: int, end: int, replacement: str) -> None:
        if _in_spans(start, end, protected):
            return
        for s, e, _r in accepted:
            if start < e and end > s:
                return
        accepted.append((start, end, replacement))

    for match in _PERCENT_RE.finditer(text):
        value = _parse_cn_numeral(match.group(1))
        if value is not None:
            _add(match.start(), match.end(), f"{value}%")

    for match in _TIME_RE.finditer(text):
        if _keyword_protected(text, match.start()):
            continue
        replacement = _norm_time(match)
        if replacement is not None:
            _add(match.start(), match.end(), replacement)

    for match in _CN_NUM_RUN_RE.finditer(text):
        token = match.group(0)
        if len(token) < 2 or not _CN_MAGNITUDE_RE.search(token):
            continue
        if token in _CN_IDIOMS:
            continue
        # 紧邻 ASCII 数字则不碰（防「3千」这类混写被拆坏）。
        if _is_digit_at(text, match.start() - 1) or _is_digit_at(text, match.end()):
            continue
        if _keyword_protected(text, match.start()):
            continue
        value = _parse_cn_numeral(token)
        if value is not None:
            _add(match.start(), match.end(), str(value))

    accepted.sort(key=lambda item: item[0])
    return accepted


def _is_digit_at(text: str, index: int) -> bool:
    if index < 0 or index >= len(text):
        return False
    return bool(_ASCII_OR_FULLWIDTH_DIGIT.match(text[index]))


def normalize_numbers(text: str) -> str:
    """口语数字规范化：``两千三百→2300``、``百分之十五→15%``、``三点半→3:30``。

    **报号串一律不动**（数字串/连读单字串区间保护 + 号码关键词前窗保护 + 数值式只认
    级量词）——见模块 docstring 的「数字铁律」两防线。
    """
    body = _as_text(text)
    edits = _collect_edits(body)
    if not edits:
        return body
    out: list[str] = []
    last = 0
    for start, end, replacement in edits:
        out.append(body[last:start])
        out.append(replacement)
        last = end
    out.append(body[last:])
    return "".join(out)


# ---- 收尾整理 ----

_EDGE_PUNCT = " \t\r\n\u3000，,。.、；;：:！!？?…~～"
_SPACE_RUN_RE = re.compile(r"[ \t\u3000]{2,}")
# 只整理 **CJK 标点** 两侧空格（中英混排里 ASCII 标点后的空格是英文正常词距，勿动）。
_CJK_PUNCT = "，。、；：！？…"
_SPACE_BEFORE_CJK_PUNCT_RE = re.compile(rf"[ \t\u3000]+([{_CJK_PUNCT}])")
_SPACE_AFTER_CJK_PUNCT_RE = re.compile(rf"([{_CJK_PUNCT}])[ \t\u3000]+")
# 空格分开的重复标点折叠为单个（"I am, , not sure" → "I am, not sure"；口水词删除的残渣）。
# 只收「有空格分隔」的形态——紧邻的 "。。"/"..." 是原文（省略号/强调），不动。
_SPACED_DUP_PUNCT_RE = re.compile(r"([，,。.、；;：:！!？?])(?:[ \t\u3000]+\1)+")


def _tidy(text: str) -> str:
    """收尾：折叠空格、整理 CJK 标点两侧空格、折叠空格分隔的重复标点、剥首尾残留。"""
    out = _SPACE_RUN_RE.sub(" ", _as_text(text))
    out = _SPACED_DUP_PUNCT_RE.sub(r"\1", out)
    out = _SPACE_BEFORE_CJK_PUNCT_RE.sub(r"\1", out)
    out = _SPACE_AFTER_CJK_PUNCT_RE.sub(r"\1", out)
    return out.strip().lstrip(_EDGE_PUNCT).strip()


# ---- 策略与出口 ----

@dataclass(frozen=True)
class PolishPolicy:
    """润色开关（默认全开；关掉某步 = 该步零行为变化，便于 A/B 归因）。"""

    fold_corrections: bool = True
    drop_fillers: bool = True
    collapse_repeats: bool = True
    normalize_numbers: bool = True
    guard: bool = True


class PolishResult(NamedTuple):
    """一次润色的完整结果：``text``=落地文本、``polished``=Guard 前的润色稿、
    ``applied``=真正改动了文本的步骤名、``guard``=Guard 裁决（留档可审计）。"""

    text: str
    polished: str
    applied: tuple[str, ...]
    guard: GuardResult


def polish_text(text: str, *, policy: PolishPolicy | None = None) -> PolishResult:
    """确定性润色出口：五步流水 + E3 Guard（豁免数字新增）后验。

    步骤序：改口取后值 → 删口水词 → 折叠重复 → 数字规范化 → 收尾整理。**Guard 挂在
    出口**（§26.2-E7「出口必挂 E3 Guard」），拒绝即回退原文（``text`` = 原文，
    ``polished`` 仍留档）。Guard 走 ``GuardPolicy(exempt_digit_addition=True)``：豁免
    数字新增（2300/15%/3:30 是模板授权的），保留硬保护 token / 语言 / 硬否定 / 敏感新增。
    """
    options = policy or PolishPolicy()
    src = _as_text(text)
    out = src
    applied: list[str] = []

    if options.fold_corrections:
        folded = fold_self_correction(out)
        if folded != out:
            applied.append("correction")
            out = folded
    if options.drop_fillers:
        cleaned = remove_fillers(out)
        if cleaned != out:
            applied.append("fillers")
            out = cleaned
    if options.collapse_repeats:
        collapsed = collapse_repetitions(out)
        if collapsed != out:
            applied.append("repeats")
            out = collapsed
    if options.normalize_numbers:
        numbered = normalize_numbers(out)
        if numbered != out:
            applied.append("numbers")
            out = numbered
    tidied = _tidy(out)
    if tidied != out:
        applied.append("tidy")
        out = tidied

    if options.guard:
        verdict = guard_output(
            src, out, policy=GuardPolicy(exempt_digit_addition=True)
        )
    else:
        verdict = GuardResult(True, "", "guard disabled", src, out)

    return PolishResult(verdict.final_text, out, tuple(applied), verdict)
