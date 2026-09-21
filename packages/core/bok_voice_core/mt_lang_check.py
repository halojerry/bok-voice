"""B 线 MT 出口**确定性语言校验器**（纯函数，零 LLM，零网络，无状态）。

出处（逐字对照语义来源）：type4me（macOS 听写 App，Swift）
``TranslationOutputValidator`` 的「目标语置信阈值 → 错语言告警」形态
（plan doc §26.2-E5 / §30.3 增补）：B 线 MT（Hy-MT2 :1236）逐句输出在进
TTS/字幕/账本之前，先用一条**确定性**判据量一下「这串字是不是目标语言的
书写系统」，不像就触发一次强化重试（重试在 interpret 侧接线，本模块只给判据）。

本模块**只做脚本层（书写系统）判定**，不调用任何模型。它只能区分两大脚本族：

- ``cjk``   —— CJK 统一表意文字 ``U+4E00–U+9FFF``（汉字）；
- ``latin`` —— ASCII 拉丁字母 ``[A-Za-z]``。

**能与不能（诚实边界，别把它当语义翻译质检）**：

- ✅ **能**可靠地逮住「脚本族错了」的错语言：译 ``en`` 却出一整句汉字
  （源文回声/模型没翻）、译 ``zh``/``cantonese`` 却出一整句英文——这是我们
  在真实 MT 出口上最可能看到的错语言形态，也是本判据的靶心。
- ⚠️ **不能**区分 ``zh`` 与 ``cantonese``：两者都是 CJK 脚本，仅凭字符构成
  无法把一句普通话和一个粤语句子分开（粤语口语大量用与普通话共用的汉字，
  专属字/词覆盖面有限且会被正常普通话/书面粤语漏掉）。因此本判据把
  ``zh``/``cantonese`` **归入同一脚本族统一处理**：只要目标是 CJK 族且输出
  是 CJK 主导，一律判通过（**故意不因普粤差异触发重试**）。``has_cantonese_markers``
  只作诊断/观测用，**不参与** ``looks_like_language`` 的门控。
  后果：B 线 ``zh→cantonese``（或反向）若 MT 把目标语方向搞反，本校验器
  **测不出来**——这是设计上承认的盲区，不是 bug。
- ⚠️ **不能**在同一脚本族内分辨方言/语体/繁简：简体 vs 繁体虽与
  ``zh``/``cantonese`` 弱相关，但普通话用户也写繁体、粤语输出也常夹简体
  同形字，拿繁简当门控会产生大量假重试（每次假重试都白烧一次 MT 往返 +
  延迟），收益不抵代价，故**明确不做**。

证据不足即放行（fail-open）：纯数字/纯标点/过短文本（字母+汉字自然字符数
低于 ``_MIN_NATURAL``）判不出语言，返回 True（不触发重试）——与 type4me
「自然语言字符过短 → 告警但不拒」同向；错在「没逮到」而不是「乱逮」，因为
乱逮的代价（多一次 MT 往返 + 可能把已经正确的译文换掉）高于漏逮。

契约与不变量：
- 主判据 ``looks_like_language(text, target_lang) -> bool``：True=通过（放行），
  False=不像目标语言（调用方据此触发**一次**强化重试）。
- ``language_match_score`` 返回 ``0.0..1.0`` 的「匹配脚本占比」；与主判据同源，
  供日志/探针读数用（1.0=匹配或证据不足）。
- ``target_lang`` 支持 ``zh``/``cantonese``/``en`` 三个规范值；另宽容常见别名
  （``chinese``/``mandarin``/``english``/``粤``/``粤语``…）。**未知语言值**不判
  （返回 True）——本模块不认识的语言不背锅。
"""

from __future__ import annotations

import re

# 规范语言值（全仓唯一拼写：zh / cantonese / en）。
CANONICAL_LANGS: tuple[str, ...] = ("zh", "cantonese", "en")

# CJK 统一表意文字，仅 U+4E00–U+9FFF（与 hotword_leak.py 同口径：不碰假名/
# 谚文/扩展区——扩展 A/B 在真实电话转写里几乎不出现，纳进来只会徒增误判面）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# ASCII 拉丁字母（不数带变音符的扩展拉丁：电话同传的英文字母就是 a-zA-Z）。
_LATIN_RE = re.compile(r"[A-Za-z]")

# 证据下限：自然字符（CJK + 拉丁字母）少于这个数就不判（fail-open）。
# type4me 的阈是 12（偏保守）；B 线同传短句多（「好的」「OK」「1234」常见），
# 取 4 既挡住纯噪声、又不因短句频繁误判。
_MIN_NATURAL = 4

# 目标 en：拉丁字母占自然字符比例 ≥ 此值算通过（英文译文基本是纯拉丁，
# 少量夹汉字（人名/地名）不该触发重试，故门槛偏宽容）。
_EN_LATIN_MIN = 0.60
# 目标 zh/cantonese：CJK 占自然字符比例 ≥ 此值算通过（中英夹杂是常态，港式
# 粤语尤甚（「OK，我 send 俾你」这类）；门槛偏宽容防假重试——纯错语言的英文
# 输出 CJK 占比≈0，压到 0.30 几乎不损失逮获力，却明显少假阳）。
_CJK_TARGET_CJK_MIN = 0.30

# 粤语专属字/词（诊断用，**不参与门控**；来源与 providers/livekit_plugins.py
# `_CANTONESE_CHARS`/`_CANTONESE_WORDS` 同族的保守子集：「普通话基本不出现」的
# 字，加少量高频粤语词）。仅用于 ``has_cantonese_markers`` 观测，
# 帮人判断「目标 cantonese 但输出疑似普通话」这类本判据拦不住的情形。
_CANTONESE_MARKERS_CHARS = frozenset("冇嘅哋佢喺嚟啲嗰喎㗎乜嘢咩啫哂啩嗌唔係咗睇俾畀")
_CANTONESE_MARKERS_WORDS: tuple[str, ...] = (
    "唔該",
    "唔使",
    "唔好",
    "唔知",
    "唔係",
    "唔會",
    "而家",
    "依家",
    "邊度",
    "幾時",
    "點解",
    "點樣",
    "係咪",
    "冇問題",
    "聽日",
)


def normalize_target_lang(raw: str) -> str:
    """把 ``target_lang`` 归一成 zh/cantonese/en；无法识别返回空串（调用方据此不判）。

    宽容常见别名（``chinese``/``mandarin``/``普通话``/``中文`` → ``zh``；
    ``粤``/``粤语``/``广东话`` → ``cantonese``；``english``/``英语`` → ``en``）。
    归一只是**入参防呆**：生产路径上 interpret 已用 ``_norm_lang`` 归一，这里
    再收一道保证纯函数单测直喂别名也稳。
    """
    key = str(raw or "").strip().lower()
    if key in {"zh", "chinese", "mandarin", "普通话", "中文"}:
        return "zh"
    if key in {"cantonese", "粤", "粤语", "广东话", "廣東話"}:
        return "cantonese"
    if key in {"en", "english", "英语"}:
        return "en"
    return ""


def script_counts(text: str) -> tuple[int, int]:
    """返回 ``(cjk, latin)``：文本里 CJK 汉字数与 ASCII 拉丁字母数（纯函数）。"""
    t = str(text or "")
    return len(_CJK_RE.findall(t)), len(_LATIN_RE.findall(t))


def script_ratio(text: str) -> tuple[float, float]:
    """返回 ``(cjk_ratio, latin_ratio)``（占自然字符 cjk+latin 的比例）。

    自然字符为 0（纯数字/标点/空）时返回 ``(0.0, 0.0)``——调用方应按
    ``_MIN_NATURAL`` 走「证据不足」分支。
    """
    cjk, latin = script_counts(text)
    natural = cjk + latin
    if natural <= 0:
        return 0.0, 0.0
    return cjk / natural, latin / natural


def _target_family(target_lang: str) -> str:
    """目标语言的期望脚本族：``latin``（en）或 ``cjk``（zh/cantonese）；未知→空串。"""
    lang = normalize_target_lang(target_lang)
    if not lang:
        return ""
    return "latin" if lang == "en" else "cjk"


def language_match_score(text: str, target_lang: str) -> float:
    """脚本匹配度 ``0.0..1.0``（纯函数，供日志/探针读数）。

    - 未知目标语言或自然字符不足 → ``1.0``（＝放行，证据不足）。
    - 目标 en → 拉丁字母占比；目标 zh/cantonese → CJK 占比。
    注意该分数**只看脚本族**：zh 与 cantonese 输出同分（见模块 docstring 的
    能/不能边界），不要拿它当普粤判别置信度用。
    """
    family = _target_family(target_lang)
    if not family:
        return 1.0
    cjk, latin = script_counts(text)
    natural = cjk + latin
    if natural < _MIN_NATURAL:
        return 1.0
    if family == "latin":
        return latin / natural
    return cjk / natural


def looks_like_language(text: str, target_lang: str) -> bool:
    """确定性主判据：``text`` 的书写系统是否像 ``target_lang``。True=通过（放行）。

    判序（与 type4me TranslationOutputValidator 的「置信阈值」形态同向）：

    1. 目标语言未知/为空 → True（不判，放行）。
    2. 自然字符（CJK+拉丁字母）``< _MIN_NATURAL`` → True（证据不足，放行）。
    3. 目标 ``en``：拉丁占比 ``>= _EN_LATIN_MIN`` → True，否则 False。
    4. 目标 ``zh``/``cantonese``：CJK 占比 ``>= _CJK_TARGET_CJK_MIN`` → True，
       否则 False（**两者同族同判**，不因普粤差异触发重试）。
    """
    family = _target_family(target_lang)
    if not family:
        return True
    cjk, latin = script_counts(text)
    natural = cjk + latin
    if natural < _MIN_NATURAL:
        return True
    if family == "latin":
        return (latin / natural) >= _EN_LATIN_MIN
    return (cjk / natural) >= _CJK_TARGET_CJK_MIN


def has_cantonese_markers(text: str) -> bool:
    """文本是否含粤语专属字/词（**诊断用，不参与门控**）。

    存在的唯一目的：当目标是 ``cantonese``、输出却是「疑似普通话」时，人能借
    这个信号判断本判据的结构性盲区是否被踩到（``looks_like_language`` 会放行，
    因为普粤同属 CJK 族——见模块 docstring）。
    """
    t = str(text or "")
    if any(ch in _CANTONESE_MARKERS_CHARS for ch in t):
        return True
    return any(w in t for w in _CANTONESE_MARKERS_WORDS)
