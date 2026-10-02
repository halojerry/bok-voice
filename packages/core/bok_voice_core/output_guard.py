"""输出后验 Guard（E3，2026-09-21）：确定性「候选输出是否可接受」纯函数单点。

出处：type4me（macOS 听写 App，Swift）``IntelliSenseOutputValidator.swift`` ＋
``CorrectionIntentAnalysis.swift`` 内联的 ``ProtectedFactExtractor`` —— 移植规格见
``docs/superpowers/plans/2026-09-21-a-line-speed-asr-decision-verification.md``
§26.2-E3（抄什么/改什么/别抄什么）与 §26.3 速查参数表的 Guard 四行。

**本模块实现的就是 §26.3 参数表钉死的那四行**（扩张上限 / 语言漂移 / 硬否定键 /
敏感新增），外加两条规格明确要求、且零阈值的硬保护腿：

1. **硬保护 token 丢失**（§26.2-E3 硬拒绝序里的一条，也是 §26.2-E7 明确要求润色面
   「保留硬保护 token 检查」的那条）：URL / 邮箱 / 字面路径 / 数字串 / 星期——
   整 token 完全匹配才算硬（候选 token 集合里找不到该原 token = 丢失）。
2. **编造数字事实**（§26.2-E3 硬拒绝序末条）：候选引入了输入没有的、含数字的保护
   token（且输入本就有保护 token）。**这条正是 E7 润色面要「豁免数字新增」的对象**
   ——见 ``GuardPolicy.exempt_digit_addition``。

口径与纪律：

- **reject → ``final_text`` = 原文**（candidate 永远留档可审计，见 ``GuardResult``）。
  与 VoxType「失败回退原文」同源（§26.1-②：VoxType 的契约只有这一条是真的）。
- **硬否定只比对 ``prohibition`` / ``never`` 两类**（§26.3 单点参数），其余四类
  （``inability`` / ``absence`` / ``contradiction`` / ``general``）**只告警不拒**——
  分类口径直接复用 ``correction_intent.semantic_negation_counts``（同一份六分类，
  避免两处词表漂移）。
- **敏感新增只在「引入」时拒**：候选出现了输入没有的凭据形 token（key/secret/
  token/password 赋值、``Bearer`` + ≥12 字符、PEM 头）→ 拒；输入本来就有 → 不拒。
- **语言无关**：只数 CJK（U+4E00–U+9FFF）与 ASCII 字母的占比，不写任何语言字面量
  （AGENTS.md 术语铁律与「不改写数字/否定/语言」三条后验纪律同源）。

**明确不做的两条**（E3 硬拒绝序里有、但本模块不实现，理由写明防后人重查）：

- ``回应标记被改``：依赖 IntelliSense 指令回显的私有哨兵串，本仓无该哨兵、来源不可
  复刻，硬编码一个猜测值只会变成假拒。
- ``「答案是/好的/以下是…」抢答前缀`` 与 ``「已为你/操作完成」执行声明``：它们判的是
  **LLM 把指令当内容回显**，活在我们「LLM 客户端边界」（出口剥锚/剥引号/围栏处理）
  的地盘；搬到通用 Guard 上会把「好的，我帮您查一下」这类合法对话首句整批判死。
  同理 ``空输出 / ``` 围栏 / tool_call 标记`` 三条是无争议的结构卫生，已实现。

消费点（本模块只出纯函数，接线留给后续实施）：QA 快路匹配前、意图判定结果后、以及
E7 离线润色出口（§26.2-E3 挂点）。

纯函数：零 I/O、零全局状态、零时间依赖；同输入恒同输出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .correction_intent import semantic_negation_counts

__all__ = [
    "GUARD_SOLICIT_LINES",
    "NEGATION_HARD_KEYS",
    "NEGATION_SOFT_KEYS",
    "REASON_EMPTY",
    "REASON_EXPANSION",
    "REASON_FABRICATED_FACT",
    "REASON_FENCE",
    "REASON_LANGUAGE_DRIFT",
    "REASON_NEGATION_DRIFT",
    "REASON_PROTECTED_TOKEN_LOST",
    "REASON_SENSITIVE_ADDITION",
    "REASON_TOOL_CALL",
    "GuardPolicy",
    "GuardResult",
    "apply_guard",
    "contains_sensitive_content",
    "guard_fabricated_number",
    "guard_output",
    "number_guard_pending",
    "protected_tokens",
]


# ---- 原因码（调用方按码分支，绝不按 detail 文本分支） ----

REASON_EMPTY = "empty"
REASON_FENCE = "fence"
REASON_TOOL_CALL = "tool_call"
REASON_PROTECTED_TOKEN_LOST = "protected_token_lost"
REASON_NEGATION_DRIFT = "negation_drift"
REASON_EXPANSION = "expansion"
REASON_LANGUAGE_DRIFT = "language_drift"
REASON_SENSITIVE_ADDITION = "sensitive_addition"
REASON_FABRICATED_FACT = "fabricated_fact"

# 硬否定键 / 软否定键（§26.3：仅 prohibition/never 计数相等；其余四类只告警）。
NEGATION_HARD_KEYS: tuple[str, ...] = ("prohibition", "never")
NEGATION_SOFT_KEYS: tuple[str, ...] = (
    "inability",
    "absence",
    "contradiction",
    "general",
)


# ---- 硬保护 token 正则（§26.2-E3，逐字可移植） ----

_URL_RE = re.compile(r"https?://[^\s<>]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PATH_RE = re.compile(r"(?:/[^\s/]+){2,}")
# 数字串：前后邻字母/数字都不算整 token（'web2' / '2web' 里不匹配）。
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,:/-]\d+)*(?![A-Za-z0-9])")
_WEEKDAY_RE = re.compile(r"(?:周|星期|礼拜)[一二三四五六日天]")

_PROTECTED_RES: tuple[re.Pattern[str], ...] = (
    _URL_RE,
    _EMAIL_RE,
    _PATH_RE,
    _NUMBER_RE,
    _WEEKDAY_RE,
)

# 敏感 token 三条正则（§26.3「敏感新增」行，逐字）。
_SENSITIVE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(?:api[_-]?key|secret|access[_-]?token|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+\S{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

# 结构卫生两条（无争议，零误报面）：围栏 / tool_call 标记。
_FENCE_RE = re.compile(r"```")
_TOOL_CALL_RE = re.compile(r"(?i)<\s*/?\s*(?:tool_call|tool_calls|function_call)\b")

# 数字面（ASCII + 全角）：豁免扩张预算与「编造数字事实」的判定面。
_DIGIT_RE = re.compile(r"[0-9０-９]")
# CJK 统一表意文字（仅 U+4E00–U+9FFF）+ ASCII 字母：语言漂移的唯二计数面。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
# 行首列表标记（"1." / "2)" / "- 1." 等）：编造事实判定要放行的形态。
_LIST_MARKER_RE = re.compile(r"(?m)^[ \t]*(?:[-*+]|\d+[.)])[ \t]")


def _as_text(value: object) -> str:
    """``None`` → ``""``；其余 ``str()``（纯函数不做类型校验，宽容入参）。"""
    return "" if value is None else str(value)


def protected_tokens(text: str) -> list[str]:
    """抽取文本里的硬保护 token（URL/邮箱/路径/数字串/星期），按出现顺序去重。

    「整 token 完全匹配才算硬」——返回值就是**整 token 字面量**（不做大小写/空白
    归一），调用方拿它做集合成员判定即是硬丢失判据（见 ``_lost_protected_tokens``）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for pattern in _PROTECTED_RES:
        for match in pattern.finditer(_as_text(text)):
            token = match.group(0)
            if token not in seen:
                seen.add(token)
                out.append(token)
    return out


def contains_sensitive_content(text: str) -> bool:
    """文本是否含任一敏感 token（凭据赋值 / ``Bearer`` + ≥12 字符 / PEM 头）。"""
    body = _as_text(text)
    return any(pattern.search(body) for pattern in _SENSITIVE_RES)


def _sensitive_matches(text: str) -> set[str]:
    """文本里命中的敏感 token 字面量集合（lower 归一后比较——大小写不算「新增」）。"""
    body = _as_text(text)
    hits: set[str] = set()
    for pattern in _SENSITIVE_RES:
        for match in pattern.finditer(body):
            hits.add(match.group(0).lower())
    return hits


def _cjk_count(text: str) -> int:
    return len(_CJK_RE.findall(_as_text(text)))


def _letter_count(text: str) -> int:
    return len(_ASCII_LETTER_RE.findall(_as_text(text)))


def _language_drift(original: str, candidate: str) -> bool:
    """§26.3 语言漂移双判据（只数 CJK + ASCII 字母）：

    - 原 CJK ≥4 且候选 CJK = 0 → 漂移（中文整段变纯拉丁/空）;
    - 双侧 total ≥8 且 CJK 占比 0.65 ↔ 0.2 **交叉**（一方高 CJK、另一方低 CJK）→ 漂移。
    """
    orig_cjk = _cjk_count(original)
    cand_cjk = _cjk_count(candidate)
    if orig_cjk >= 4 and cand_cjk == 0:
        return True
    orig_total = orig_cjk + _letter_count(original)
    cand_total = cand_cjk + _letter_count(candidate)
    if orig_total >= 8 and cand_total >= 8:
        orig_ratio = orig_cjk / orig_total
        cand_ratio = cand_cjk / cand_total
        if (orig_ratio >= 0.65 and cand_ratio <= 0.2) or (
            orig_ratio <= 0.2 and cand_ratio >= 0.65
        ):
            return True
    return False


def _lost_protected_tokens(original: str, candidate: str) -> list[str]:
    """原文本里「整 token 完全匹配」却在候选中消失的保护 token。"""
    orig_tokens = protected_tokens(original)
    if not orig_tokens:
        return []
    cand_set = set(protected_tokens(candidate))
    return [token for token in orig_tokens if token not in cand_set]


def _is_list_marker(candidate: str, token: str) -> bool:
    """``token`` 在候选里是否位于行首列表标记位（"1." / "2)" 形态）——放行不判编造。"""
    for match in _LIST_MARKER_RE.finditer(candidate):
        span = match.group(0)
        if span.strip(" \t")[: len(token)] == token:
            return True
    return False


def _fabricated_digit_token(original: str, candidate: str) -> str | None:
    """候选引入的、含数字的保护 token（且输入本就有保护 token）；无 → ``None``。

    §26.2-E3 末条「编造事实」：新增 token 含数字且非行首列表标记，且输入本有保护
    token。返回第一个命中 token（供 detail 展示）。
    """
    orig_tokens = protected_tokens(original)
    if not orig_tokens:
        return None
    orig_set = set(orig_tokens)
    for token in protected_tokens(candidate):
        if token in orig_set:
            continue
        if not _DIGIT_RE.search(token):
            continue
        if _is_list_marker(candidate, token):
            continue
        return token
    return None


def _budget_len(text: str, exempt_digit_addition: bool) -> int:
    """扩张预算的长度面：默认 = 原长；豁免数字新增时先剥数字（ASCII+全角）。"""
    body = _as_text(text)
    if exempt_digit_addition:
        return len(_DIGIT_RE.sub("", body))
    return len(body)


def _check_expansion(
    original: str, candidate: str, exempt_digit_addition: bool
) -> bool:
    """§26.3 扩张上限：候选长 > ``max(3N, N+120)`` 即拒（N = 原长）。

    豁免数字新增时，两侧都按「剥数字后的长度」计——否则润色面把「两千三百」写成
    「2300」这类**被规格授权的**数字新增会被长度预算误杀（见 ``GuardPolicy``）。
    """
    n = _budget_len(original, exempt_digit_addition)
    cap = max(3 * n, n + 120)
    return _budget_len(candidate, exempt_digit_addition) > cap


@dataclass(frozen=True)
class GuardPolicy:
    """Guard 的调用策略位（默认 = 原样，零行为变化）。

    ``exempt_digit_addition``（E7 润色面专用）：**豁免「数字新增」**——同时关掉
    「编造数字事实」判定、并在扩张预算里剥数字计长。§26.2-E7 的单句规格：
    「润色面豁免数字新增告警、保留硬保护 token 检查」。**硬保护 token 丢失、
    语言漂移、硬否定、敏感新增、空输出一律照旧**——豁免的只有「新增数字」这一件事，
    不是豁免「丢数字」。
    """

    exempt_digit_addition: bool = False


@dataclass(frozen=True)
class GuardResult:
    """一次后验的结果：``accepted`` 真 = 通过；假 = 拒绝（``reason`` 为原因码）。

    ``final_text`` 直接落地「reject → 原文」纪律：通过用候选、拒绝用原文；``candidate``
    永远留档可审计（不是被丢弃的临时值）。``warnings`` 是「只告警不拒」的软面读数
    （软否定四类漂移 / 未实现原因码的说明性信息）。
    """

    accepted: bool
    reason: str
    detail: str
    original: str
    candidate: str
    warnings: tuple[str, ...] = field(default=())

    @property
    def rejected(self) -> bool:
        """是否被拒（``accepted`` 的逆，供调用方读起来直白些）。"""
        return not self.accepted

    @property
    def final_text(self) -> str:
        """落地文本：通过取候选、拒绝回退原文（candidate 仍留在 ``.candidate``）。"""
        return self.candidate if self.accepted else self.original

    def __bool__(self) -> bool:  # 便于 ``if guard_output(...):``
        return self.accepted


def _reject(
    reason: str, detail: str, original: str, candidate: str
) -> GuardResult:
    return GuardResult(False, reason, detail, original, candidate)


def guard_output(
    original: str,
    candidate: str,
    *,
    policy: GuardPolicy | None = None,
) -> GuardResult:
    """对 ``(原始输入, 候选输出)`` 做确定性后验，返回 ``GuardResult``。

    判定序（与 §26.2-E3 硬拒绝序对齐，前条命中即返回）：

    1. ``empty``：候选 strip 后为空；
    2. ``fence`` / ``tool_call``：候选含 ``` 围栏或 tool_call 标记（结构卫生）；
    3. ``protected_token_lost``：原硬保护 token（整 token 匹配）在候选中消失；
    4. ``negation_drift``：``prohibition`` / ``never`` 两类计数不等；
    5. ``expansion``：候选长 > ``max(3N, N+120)``（N = 原长）；
    6. ``language_drift``：CJK≥4→0 或 total≥8 且 0.65↔0.2 交叉；
    7. ``sensitive_addition``：候选引入输入没有的凭据形 token；
    8. ``fabricated_fact``：候选引入含数字的新保护 token（豁免数字新增时不判）。

    通过时返回 ``accepted=True``、``reason=""``；软否定四类的漂移进 ``warnings``。
    """
    active = policy or GuardPolicy()
    orig = _as_text(original)
    cand = _as_text(candidate)

    if not cand.strip():
        return _reject(REASON_EMPTY, "candidate is empty", orig, cand)
    if _FENCE_RE.search(cand):
        return _reject(REASON_FENCE, "candidate contains a ``` fence", orig, cand)
    if _TOOL_CALL_RE.search(cand):
        return _reject(
            REASON_TOOL_CALL, "candidate contains a tool_call marker", orig, cand
        )

    lost = _lost_protected_tokens(orig, cand)
    if lost:
        return _reject(
            REASON_PROTECTED_TOKEN_LOST,
            "protected token(s) lost: " + ", ".join(lost[:5]),
            orig,
            cand,
        )

    orig_neg = semantic_negation_counts(orig)
    cand_neg = semantic_negation_counts(cand)
    hard_drift = [
        key for key in NEGATION_HARD_KEYS if orig_neg[key] != cand_neg[key]
    ]
    if hard_drift:
        detail = ", ".join(
            f"{key} {orig_neg[key]}->{cand_neg[key]}" for key in hard_drift
        )
        return _reject(REASON_NEGATION_DRIFT, "hard negation changed: " + detail, orig, cand)

    if _check_expansion(orig, cand, active.exempt_digit_addition):
        return _reject(
            REASON_EXPANSION,
            f"candidate length {len(cand)} exceeds budget "
            f"(original {len(orig)})",
            orig,
            cand,
        )

    if _language_drift(orig, cand):
        return _reject(
            REASON_LANGUAGE_DRIFT, "CJK/latin ratio drifted", orig, cand
        )

    new_sensitive = _sensitive_matches(cand) - _sensitive_matches(orig)
    if new_sensitive:
        return _reject(
            REASON_SENSITIVE_ADDITION,
            "candidate introduces sensitive token(s)",
            orig,
            cand,
        )

    if not active.exempt_digit_addition:
        fabricated = _fabricated_digit_token(orig, cand)
        if fabricated is not None:
            return _reject(
                REASON_FABRICATED_FACT,
                f"candidate introduces numeric token {fabricated!r}",
                orig,
                cand,
            )

    warnings: list[str] = []
    soft_drift = [
        key for key in NEGATION_SOFT_KEYS if orig_neg[key] != cand_neg[key]
    ]
    if soft_drift:
        warnings.append("soft_negation_drift:" + ",".join(soft_drift))
    return GuardResult(True, "", "ok", orig, cand, tuple(warnings))


def apply_guard(
    original: str,
    candidate: str,
    *,
    policy: GuardPolicy | None = None,
) -> str:
    """便捷出口：通过取候选、拒绝回退原文（等价 ``guard_output(...).final_text``）。"""
    return guard_output(original, candidate, policy=policy).final_text


# ---------------------------------------------------------------------------
# 编造号码输出守卫（2026-10-01，实弹 call-231aa92a）——与上面的 E3 后验 Guard
# 是两件独立的事，共用本模块只为「出口纯函数守卫」一个家。
#
# 病灶：A 线粤语外呼，流程第 5 步向客户索取 WhatsApp 号码。客户未报任何号码时
# 9B 凭空输出「收到，尾號係七七八八九九八，啱唔啱？」——用编造数字做确认
# （合规级问题；模板/QA 库均无该串，已查证）。必须在 LLM 出口做确定性守卫。
#
# 判定三件同时成立才动手（缺一不动，规则 4 铁律）：
#   ① 语境词门：句含 尾號/尾号/號碼/号码/WhatsApp/單號/单号/phone number/number；
#   ② 数字 run：≥4 位（阿拉伯半/全角、中文数词 七七八八九九八、混排都算）；
#   ③ 确认句式：语境词或 係/是/is/啱唔啱 等确认标记与 run 紧邻。
# 赔偿金额（「最低 300 蚊」<4 位、无语境词）、工作日（「三至五個工作天」不成
# 连续 run）等非确认数字结构性不中。
#
# 数字「合法」判定（不变量，Ethan 第一性原理定案 2026-10-01）：确认句里的
# 数字 run ∈ {捕获账本 captured, 本轮用户原话转写 turn_user_text}。复述内容
# 源唯一=用户真说过的话——听错靠复述环路本身兜住（忠实念回错的、让客户当场
# 纠正），守卫绝不猜「正确」号码；边角=客户当场改口时捕获账本可能滞后，本轮
# 原话匹配放行，防误改回旧值。两者都不含该 run = 编造。
#
# 动作：违约时（编造）有捕获 → 该 run 改正为捕获号码（阿拉伯→中文数位逐位，
# MiniMax 逐位读）；无捕获 → 含确认的那句整句替换为三语索取句。
# 只替换/改正命中的句子，其余句子原样保留；恒等返回=无改动。
#
# 纯函数、零 I/O、零全局状态；幂等（守卫后文本再守卫恒等）。
# ---------------------------------------------------------------------------

# 三语索取句（lang 未知回落 zh——与 `_call_language` 缺省链同口径）。
GUARD_SOLICIT_LINES: dict[str, str] = {
    "cantonese": "你報個常用嘅 WhatsApp 號碼俾我，我再同你確認一次。",
    "zh": "您报一个常用的 WhatsApp 号码给我，我再跟您确认一次。",
    "en": "Could you share your WhatsApp number so I can confirm it?",
}

# 号码语境词（三语；英文按 casefold 匹配）。
_NUMBER_CTX_WORDS: tuple[str, ...] = (
    "尾號", "尾号", "號碼", "号码", "whatsapp", "單號", "单号",
    "phone number", "number",
)

# 数字 run：阿拉伯（半/全角）+ 中文数词（含 〇），混排也算，≥4 位。
_NUMBER_RUN_RE = re.compile(r"[0-9０-９零〇一二三四五六七八九]{4,}")

# 逐位转换表（与 flow.py `_digit_normalize` 同口径；英文数字词不在本守卫面）。
_DIGIT_ALIASES: dict[str, str] = {
    **{d: d for d in "0123456789"},
    **{c: str(i) for i, c in enumerate("０１２３４５６７８９")},
    "零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}
_ASCII_TO_CN = "零一二三四五六七八九"

# 句界（强标点+换行，含分号）——整句连同句末标点一起替换，避免「。，」残句；
# 逗号链不是句界（只替换含确认的整句，不往逗号链里塞索取句）。
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;\n]*")

# 确认标记紧邻 run 之前（係/是/号码/is/…；容许冒号等连接符）。
_CONFIRM_BEFORE_RE = re.compile(
    r"(?:係|系|是|\b(?:is|are)\b|號|号|碼|码|:|：|=)\s*$",
    re.IGNORECASE,
)
# run 之后的确认词（啱唔啱/对吗/right/…）。
_CONFIRM_AFTER_RE = re.compile(
    r"^\s*(?:啱唔啱|啱嗎|對唔對|對嗎|对吗|對不對|对不对|係咪|係唔係|是不是|"
    r"right|correct|is that right|yes or no)",
    re.IGNORECASE,
)

# 首段早发扣留触发面：缓冲出现任一数字字符或语境词残件（含单字前缀，防语境词
# 被早切点劈开）→ 早发让位到句界（守卫必须整句在手才能替换/改正）。
_NUMBER_PENDING_CHARS = frozenset(
    "0123456789０１２３４５６７８９零〇一二三四五六七八九尾號号码碼單单"
)


def _digits_ascii(text: object) -> str:
    """文本里的数字字符逐位归一成 ASCII（汉字/全角/阿拉伯混排都收；其余丢弃）。"""
    return "".join(_DIGIT_ALIASES[ch] for ch in str(text or "") if ch in _DIGIT_ALIASES)


def _digits_to_cn(digits: str) -> str:
    """ASCII 数字串 → 中文数词（逐位；MiniMax 对汉字数字逐位读）。"""
    return "".join(_ASCII_TO_CN[int(ch)] for ch in digits if ch in "0123456789")


# 分组分隔符（数字夹缝里）先折叠：「7788-998」/「7788、998」是同一个号的
# 分组报号（与 flow.py `_collapse_digit_groups` 同口径），折叠后才抽 run。
_DIGIT_GROUP_SEP_RE = re.compile(
    r"(?<=[0-9０-９零〇一二三四五六七八九])[ \t,，、\-–—·:：/／]+"
    r"(?=[0-9０-９零〇一二三四五六七八九])"
)


def _transcript_digit_runs(text: object) -> tuple[str, ...]:
    """转写里的数字 run 集合（≥4 位，归一 ASCII；分组分隔符先折叠）。"""
    body = _DIGIT_GROUP_SEP_RE.sub("", str(text or ""))
    runs: list[str] = []
    for m in _NUMBER_RUN_RE.finditer(body):
        v = _digits_ascii(m.group(0))
        if v and v not in runs:
            runs.append(v)
    return tuple(runs)


def _context_hit(text: str) -> bool:
    low = str(text or "").lower()
    return any(word in low for word in _NUMBER_CTX_WORDS)


def _ctx_close_before(before: str) -> bool:
    """语境词与 run 之间只隔极短连接成分（係/是/is/冒号/空白/标点）→ 紧邻。"""
    low = str(before or "").lower()
    for word in _NUMBER_CTX_WORDS:
        idx = low.rfind(word)
        if idx == -1:
            continue
        gap = low[idx + len(word):].strip(" \t:：,，、=＝")
        if len(gap) <= 3:
            return True
    return False


# 分句内连接符（逗号/顿号/分号/句读）——确认句式不跨分句认语境词，防
# 「查咗單號，賠償金額係 3000」这类串句误把金额 run 当号码确认。
_CLAUSE_BREAK_RE = re.compile(r"[，,；;。！？!?、\n]")


def _run_is_confirmation(sentence: str, match: "re.Match[str]") -> bool:
    """run 是否处于确认句式（规则 4：係/是/号 + 数字 run 邻近）。"""
    before = sentence[max(0, match.start() - 16): match.start()]
    after = sentence[match.end(): match.end() + 12]
    if _ctx_close_before(before):
        return True
    clause = _CLAUSE_BREAK_RE.split(before)[-1]
    if _context_hit(clause) or _context_hit(after):
        if _CONFIRM_BEFORE_RE.search(before[-6:]):
            return True
        if _CONFIRM_AFTER_RE.search(after):
            return True
    return False


def _run_is_known(run_digits: str, captured: str, turn_runs: tuple[str, ...]) -> bool:
    """run 数字是否来自已知内容源（捕获账本 ∪ 本轮客户原话转写）。

    匹配形态=run 是已知串的子串（等长/尾号后缀都合法——尾号场景即「捕获串的
    后 N 位等长子串」）。run 比已知串长（多出数字）=新增=违约。"""
    if not run_digits:
        return False
    if captured and run_digits in captured:
        return True
    return any(run_digits in known for known in turn_runs)


def _guard_sentence(
    sentence: str, lang: str, captured: str, turn_runs: tuple[str, ...]
) -> str:
    """单句守卫：命中编造确认 → 索取句；有捕获的错号 → 改正；否则恒等。"""
    if not sentence.strip() or not _context_hit(sentence):
        return sentence
    runs = list(_NUMBER_RUN_RE.finditer(sentence))
    if not runs:
        return sentence
    confirmed = [m for m in runs if _run_is_confirmation(sentence, m)]
    if not confirmed:
        return sentence
    bad = [
        m
        for m in confirmed
        if not _run_is_known(_digits_ascii(m.group(0)), captured, turn_runs)
    ]
    if not bad:
        return sentence
    if not captured:
        return GUARD_SOLICIT_LINES.get(lang, GUARD_SOLICIT_LINES["zh"])
    out: list[str] = []
    last = 0
    for m in bad:
        out.append(sentence[last: m.start()])
        out.append(_digits_to_cn(captured))
        last = m.end()
    out.append(sentence[last:])
    return "".join(out)


def guard_fabricated_number(
    text: str,
    lang: str,
    captured: str | None,
    turn_user_text: str | None = None,
    known_text: str | None = None,
) -> str:
    """编造号码输出守卫（纯函数，零 LLM）：返回守卫后文本，恒等=无改动。

    ``captured`` = 本通已捕获的客户号码文本（``ContextState._whatsapp_note``；
    None/空 = 无捕获）；``turn_user_text`` = 本轮客户原话转写（None=拿不到权威
    源）；``known_text`` = 对象档案等**系统已知事实**文本（快递单号等——AI 念读
    系统数据做确认是合法确认环（客户可当场纠正），同为合法内容源；None=无）。
    确认句里的数字 run 只有落在 {captured, turn_user_text 的数字 run,
    known_text 的数字 run} 之内才算合法（含尾号后缀子串形态）——复述内容源
    唯一=用户真说过的话 ∪ 系统已知事实。
    违约时：有捕获 → 把错误 run 改正为捕获号码（阿拉伯→中文逐位）；无捕获 →
    含确认的句子整句替换为三语索取句（替换后整段为空则整段用索取句）。
    非确认语境的数字（赔偿金额/工作日）结构性不中，绝不动。
    """
    body = "" if text is None else str(text)
    if not body:
        return body
    captured_digits = _digits_ascii(captured or "")
    turn_runs = _transcript_digit_runs(turn_user_text) + _transcript_digit_runs(known_text)
    lang_key = str(lang or "").strip().lower()
    if lang_key not in GUARD_SOLICIT_LINES:
        lang_key = "zh"
    out: list[str] = []
    changed = False
    last = 0
    for m in _SENTENCE_RE.finditer(body):
        out.append(body[last: m.start()])
        seg = m.group(0)
        guarded = _guard_sentence(seg, lang_key, captured_digits, turn_runs)
        if guarded != seg:
            changed = True
        out.append(guarded)
        last = m.end()
    out.append(body[last:])
    if not changed:
        return body
    final = "".join(out)
    # 「替换后为空则整段用索取句」兜底（整段句界字符等 degenerate 输入）。
    return final if final.strip() else GUARD_SOLICIT_LINES[lang_key]


def number_guard_pending(text: str) -> bool:
    """缓冲里是否可能出现「号码确认」→ 首段早发扣留判据（纯函数）。

    出现任一数字字符（阿拉伯/全角/中文数词）或语境词（含其单字残件）即真——
    宁可让首段早发让位到句界（守卫必须整句在手才能替换/改正），不能让号码
    被早切点劈成两段漏检。
    """
    body = str(text or "")
    if any(ch in _NUMBER_PENDING_CHARS for ch in body):
        return True
    return _context_hit(body)
