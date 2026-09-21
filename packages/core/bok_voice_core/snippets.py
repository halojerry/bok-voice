"""snippet 后置正则轨（E1，2026-09-20）：ASR 出稿后的确定性词级替换纯函数单点。

出处：type4me（macOS 听写 App）``SnippetStorage.swift`` 的源码级移植 —— 移植规格见
``docs/superpowers/plans/2026-09-21-a-line-speed-asr-decision-verification.md``
§26.2-E1（源文件/函数 → 抄什么/改什么/别抄什么）与 §26.3 速查参数表。

抄过来的机制（逐条对应源实现）：
- ``build_flex_pattern``：trigger 去掉**全部** Unicode 空白 → 剩余每个字符单独
  ``re.escape`` → 用 ``\\s*`` 连接 → 整体包 ``(?<![a-zA-Z0-9])`` … ``(?![a-zA-Z0-9])``
  （ASCII lookaround，**不用** ``\\b``——``\\b`` 把 CJK 当词字符，中文句内「单后」这类
  邻字是 CJK 的命中会被它整批判掉，边界形同失效）；
- ``re.IGNORECASE`` 编译；
- 替换值走 ``re.sub(pat, lambda m: value, text)`` 回调形式（模板字符串里 ``$`` / ``\\``
  会被当展开语法，回调形式天然免疫）；
- 规则**串行链式**执行：前一条的输出就是后一条的输入。

修掉它的两个已知缺陷（type4me 没有的守卫，我们必须有）：
1. **数字铁律**：trigger 或 replacement 含任何数字（ASCII ``0-9`` 与全角 ``０-９``）的
   规则**拒绝加载**。snippet 跑在 WhatsApp/微信号码捕获与快递单号语义的前后，绝不能
   改写客户报的数字串——这是「数字零降级」铁律的同族守卫。
2. **空 trigger 铁律**：归一后为空的 trigger 拒绝加载。type4me 会为它生成匹配空串的
   正则，逐位置插入替换值——破坏性。

另加长度护栏（审查补的第三条铁律与原护栏）：**归一后 trigger ≥ 2 字**（单字 trigger
在 CJK 邻字可命中的机制下=全句该字皆被替换，与 §26-E6「单汉字替换永不生成全局映射」
同款纪律）、trigger ≤ 32 字符、replacement ≤ 64 字符。

**语言分域（2026-09-21 批次 3，词表挖掘改判逼出）**：规则带 ``lang`` 字段——``""``
= 全语言生效，``zh`` / ``cantonese`` / ``en`` = 仅该语言通话生效。挖掘实弹发现真实
错误形态**大部分是繁体形**（``集運`` 类）：在中文通话里是错字（该改成简体）、在粤语
通话里是**正确写法**（改了反而错）。词表不分域就是双向伤害，所以分域是硬需求而非
优化。装配序**必须先分域再合并**（``rules_for_lang`` → ``merge_rules`` → 编译）：
反序会让「本语言专用规则在合并时压掉全语言规则、随后又被分域滤掉」= 全语言规则
凭空消失。

另外，type4me 把内置词表存了不用（``SnippetStorage.apply`` 只编译用户 ``snippets.json``，
``builtin-snippets.json`` 那 100+ 条映射从不进入替换路径）——这里 ``merge_rules`` 是
**单点合并生效**：调用方按 ``[内置, 账号, 模板]`` 顺序传，后者覆盖前者。

消费点（本模块只出纯函数，接线留给后续实施）：agent 收到 ASR FINAL 转写之后、
意图判定（话术图 / 规则推进）与 QA 快路匹配之前。

术语：语言相关字面量只用 ``zh`` / ``cantonese`` / ``en``（AGENTS.md 术语铁律）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

__all__ = [
    "ALLOWED_LANGS",
    "MAX_REPLACEMENT_CHARS",
    "MAX_TRIGGER_CHARS",
    "MIN_TRIGGER_CHARS",
    "CompilationReport",
    "CompiledRule",
    "SkippedRule",
    "SnippetApplication",
    "SnippetRule",
    "apply_snippets",
    "build_flex_pattern",
    "compile_rules",
    "compile_rules_with_skipped",
    "merge_rules",
    "normalize_key",
    "normalize_lang",
    "parse_rule",
    "rules_for_lang",
    "validate_rule",
]

# 语言分域的合法值域（AGENTS.md 术语铁律：全时空唯一拼写，只有这三态）。
# ``""``（缺省）不在本集合内但合法——语义是「全语言生效」，由 ``rules_for_lang``
# 的 ``in ("", lang)`` 判定承载，与「写错的 lang」严格区分。
ALLOWED_LANGS: tuple[str, ...] = ("zh", "cantonese", "en")

# 长度护栏（归一后 trigger 的字符数 / replacement 的原始字符数）。
MIN_TRIGGER_CHARS = 2  # 单字 trigger 拒绝：CJK 邻字可命中=全句该字皆被替换（§26-E6 同款纪律）
MAX_TRIGGER_CHARS = 32
MAX_REPLACEMENT_CHARS = 64

# 数字铁律的判定面：ASCII 数字 + 全角数字。
_DIGIT_RE = re.compile(r"[0-9０-９]")

# 与 pattern 里 ``\s*`` 同一定义（同一 re 模块语义），保证「去空白」与「跳空白」
# 互相一致：归一后剩下的字符恰好是 pattern 里逐个 escape 的那些字符。
_WS_RE = re.compile(r"\s+")

# 词边界 lookaround（ASCII 类）。**勿改**为 ``\b``：CJK 是 ``\w``，中文句内的命中
# 会被 ``\b`` 整批判掉（plan §26.2-E1/§26.3 单点参数）。
_LOOKBEHIND = r"(?<![a-zA-Z0-9])"
_LOOKAHEAD = r"(?![a-zA-Z0-9])"


@dataclass(frozen=True)
class SnippetRule:
    """一条词级替换规则：trigger（含空白亦可）→ replacement，可选语言分域。"""

    trigger: str
    replacement: str
    source: str = ""  # 审计位：builtin / account / template 等来源标记
    lang: str = ""  # 分域位：""=全语言 / "zh"|"cantonese"|"en"=仅该语言通话

    def norm_key(self) -> str:
        """归一化去重键：去全部空白 + lower（空格/大小写不敏感的词表身份）。

        注意：这是 **trigger 身份**（不含 lang）——合并去重发生在分域过滤**之后**，
        所以同 trigger 的跨语言条目此时已不可能同场（见 ``rules_for_lang`` 与
        ``merge_rules`` 的顺序契约）。
        """
        return normalize_key(self.trigger)


@dataclass(frozen=True)
class CompiledRule:
    """已编译规则：原规则 + ``re.IGNORECASE`` 正则。"""

    rule: SnippetRule
    pattern: re.Pattern[str]

    @property
    def trigger(self) -> str:
        return self.rule.trigger

    @property
    def replacement(self) -> str:
        return self.rule.replacement


@dataclass(frozen=True)
class SkippedRule:
    """被守卫拦下的规则 + 原因码（供审计/日志）。"""

    rule: SnippetRule
    reason: str


@dataclass(frozen=True)
class CompilationReport:
    """一次编译的完整结果：可用规则 + 被跳过清单。"""

    compiled: tuple[CompiledRule, ...]
    skipped: tuple[SkippedRule, ...]


class SnippetApplication(NamedTuple):
    """一次应用的结果：``(text, applied)``，applied = 实际命中的 (trigger, replacement)。"""

    text: str
    applied: list[tuple[str, str]]


def normalize_key(trigger: str) -> str:
    """去全部 Unicode 空白 + lower（去重/覆盖的唯一键）。"""
    return _WS_RE.sub("", str(trigger or "")).lower()


def normalize_lang(lang: object) -> str:
    """lang 归一：去首尾空白 + lower；缺省/空值 → ``""``（全语言）。

    只做大小写与空白归一（``"Cantonese "`` → ``"cantonese"``），**不做别名映射**
    ——AGENTS.md 术语铁律要求语言三态唯一拼写，旧拼写/方言别名一律视为非法值由
    ``validate_rule`` 拦下（``bad_lang``），绝不在此静默纠正。
    """
    return str(lang or "").strip().lower()


def rules_for_lang(rules: Iterable[Any] | None, lang: str) -> list[SnippetRule]:
    """按通话语言分域过滤：保留 ``lang == ""``（全语言）与 ``lang == 通话语言`` 的规则。

    顺序契约：**必须在 ``merge_rules`` 之前调用**。若反序（先合并后分域），本语言
    专用规则会在合并时按 trigger 压掉全语言规则（后者胜出），随后又因语言不匹配被
    滤掉——该 trigger 在本通电话里变成「无规则」，全局兜底凭空消失。

    ``lang`` 传空串 = 不限域（返回全部规则，含各语言专用条目）；调用方不知道通话
    语言时用这一档（等价旧行为）。本函数只过滤不校验——非法值由 ``validate_rule``
    的 ``bad_lang`` 拦下并进 skipped 审计面（此处比较两侧都走 ``normalize_lang``，
    与 ``validate_rule`` 同口径，避免「大小写不同 → 分域滤掉但校验不管」的静默丢规则）。
    """
    want = normalize_lang(lang)
    out: list[SnippetRule] = []
    for raw in rules or []:
        rule = parse_rule(raw)
        if rule is None:
            continue
        rl = normalize_lang(rule.lang)
        if not want or not rl or rl == want:
            out.append(rule)
    return out


def build_flex_pattern(trigger: str) -> str:
    """trigger → 空白不敏感的词边界正则串（type4me ``buildFlexPattern`` 移植）。

    归一去空白后为空即非法：调用方必须先过 ``validate_rule``（这里直接 raise，
    防止有人绕过守卫造出匹配空串的破坏性正则）。
    """
    compact = _WS_RE.sub("", str(trigger or ""))
    if not compact:
        raise ValueError("empty trigger: refusing to build a match-empty pattern")
    body = r"\s*".join(re.escape(ch) for ch in compact)
    return f"{_LOOKBEHIND}{body}{_LOOKAHEAD}"


def validate_rule(rule: SnippetRule) -> str:
    """严格轨校验：返回错误原因码（空串 = 合法）。

    判定序：``digits``（数字铁律）→ ``bad_lang``（语言值域）→ ``empty_trigger``
    （空 trigger 铁律）→ ``trigger_too_short``（单字铁律）→ 长度护栏。

    ``bad_lang`` 比较走 ``normalize_lang``（大小写/空白不算错），但**不做别名映射**
    ——旧粤语拼写（见 AGENTS.md 术语铁律）一律 ``bad_lang``。
    """
    trigger = str(rule.trigger or "")
    replacement = str(rule.replacement or "")
    if _DIGIT_RE.search(trigger) or _DIGIT_RE.search(replacement):
        return "digits"
    if normalize_lang(rule.lang) not in ("", *ALLOWED_LANGS):
        return "bad_lang"
    compact = _WS_RE.sub("", trigger)
    if not compact:
        return "empty_trigger"
    if len(compact) < MIN_TRIGGER_CHARS:
        return "trigger_too_short"
    if len(compact) > MAX_TRIGGER_CHARS:
        return "trigger_too_long"
    if len(replacement) > MAX_REPLACEMENT_CHARS:
        return "replacement_too_long"
    return ""


def parse_rule(raw: Any) -> SnippetRule | None:
    """宽容解析：``SnippetRule`` 本体直通；mapping → 取 trigger/replacement/source/lang；
    结构坏（非 mapping / 缺关键键）返回 None（由调用方计入 skipped=``bad_rule``）。

    ``lang`` 走 ``normalize_lang`` 归一（缺省/None → ``""``）；归一后仍非法（旧拼写
    之类）**不在解析层拦截**——宽容解析只保证结构，值域由 ``validate_rule``
    的 ``bad_lang`` 严格轨拦下（与 digits 同为「解析宽容 / 校验严格」分工）。
    """
    if isinstance(raw, SnippetRule):
        return raw
    if isinstance(raw, Mapping):
        trigger = raw.get("trigger")
        replacement = raw.get("replacement")
        if trigger is None or replacement is None:
            return None
        return SnippetRule(
            trigger=str(trigger),
            replacement=str(replacement),
            source=str(raw.get("source") or ""),
            lang=normalize_lang(raw.get("lang")),
        )
    return None


def compile_rules_with_skipped(rules: Iterable[Any] | None) -> CompilationReport:
    """编译 + 完整审计面（compiled / skipped）。守卫拦下的规则跳过并记原因。"""
    compiled: list[CompiledRule] = []
    skipped: list[SkippedRule] = []
    for raw in rules or []:
        rule = parse_rule(raw)
        if rule is None:
            skipped.append(SkippedRule(SnippetRule("", ""), "bad_rule"))
            continue
        reason = validate_rule(rule)
        if reason:
            skipped.append(SkippedRule(rule, reason))
            continue
        try:
            pattern = re.compile(build_flex_pattern(rule.trigger), re.IGNORECASE)
        except re.error:  # pragma: no cover - 逐字符 escape 后理论上不可达
            skipped.append(SkippedRule(rule, "bad_pattern"))
            continue
        compiled.append(CompiledRule(rule=rule, pattern=pattern))
    return CompilationReport(tuple(compiled), tuple(skipped))


def compile_rules(rules: Iterable[Any] | None) -> list[CompiledRule]:
    """规则集 → 已编译规则（守卫拦下的静默跳过；审计面见 ``compile_rules_with_skipped``）。

    注意：这里**不做去重**——词表合并的唯一入口是 ``merge_rules``（[内置, 账号, 模板]，
    后者覆盖前者）。直接传重叠规则集会让重复项各跑一遍。
    """
    return list(compile_rules_with_skipped(rules).compiled)


def apply_snippets(text: str, rules: Iterable[Any] | None) -> SnippetApplication:
    """对 ``text`` 串行链式应用规则，返回 ``(text, applied)``。

    每条规则做**全文替换**，前一条的输出即后一条的输入；``applied`` 只记真正命中的
    ``(trigger, replacement)``（未命中的不记账）。
    """
    out = str(text or "")
    applied: list[tuple[str, str]] = []
    for compiled in compile_rules_with_skipped(rules).compiled:
        if not compiled.pattern.search(out):
            continue
        replacement = compiled.rule.replacement
        # 回调形式：replacement 里的 ``$`` / ``\`` 不会被当模板展开语法。
        out = compiled.pattern.sub(lambda _m, _v=replacement: _v, out)
        applied.append((compiled.rule.trigger, replacement))
    return SnippetApplication(out, applied)


def merge_rules(*rule_lists: Iterable[Any] | None) -> list[SnippetRule]:
    """多来源词表合并去重（单点生效）：后者覆盖前者。

    去重键 = ``normalize_key(trigger)``（去空白 + lower），所以 "web coding" /
    "webcoding" / "Web  Coding" 是同一身份。调用方按 ``[内置, 账号, 模板]`` 顺序传，
    后面的列表覆盖前面的：**值取最后一次定义**，输出位置取该键**首次出现**处
    （保持调用方给出的相对顺序，便于串行链式的行为可预测）。

    键为空串（空 trigger）的规则照常参与合并（多条空 trigger 折叠为一条），
    它们会在 ``compile_rules`` 被空 trigger 铁律拦下。

    **顺序契约**：调用方必须先 ``rules_for_lang`` 分域、再进本函数（见该函数与模块
    docstring 的反序失效说明）。合并键只有 trigger 身份，不含 lang——分域后同场
    不存在跨语言同 trigger 条目，该假设才成立。
    """
    merged: dict[str, SnippetRule] = {}
    for rules in rule_lists:
        for raw in rules or []:
            rule = parse_rule(raw)
            if rule is None:
                continue
            merged[rule.norm_key()] = rule  # 已存在的键：值覆盖、位置保持首次出现
    return list(merged.values())
