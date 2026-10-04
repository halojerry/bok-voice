"""确定性 ASR 音近纠错核心(2026-09-27)。

架构定位——**原文单轨契约**:本模块产出只喂 LLM 上下文,业务判据吃 raw。
即:纠错后的文本可以进对话历史/prompt 参考,但**任何业务判据(流程推进、
号码捕获、意图规则、快路匹配、开单字段)一律读原始转写**,绝不用纠错结果。
理由:纠错是「音近吸附」,本质是启发式猜测,一旦被业务判据消费,一次误纠
就会改变流程分支且不可回溯;而只喂 LLM 时,猜错顶多让模型看到一句更干净的
话,伤害有界可逆。故接线层必须把 raw 与 polished 分开持握(接线由主线做)。

设计要点:
- **纯函数 + 零第三方依赖**:运行时只读一份静态 JSON 资产(随源码分发,
  ``assets/asr_variants.json``),不 import ToJyutping/pypinyin 等构建期库。
- **冻结铁律**:连续数字串(阿拉伯数字 + 中文数字词一二三四五六七八九零俩
  廿百千万亿两十)永不改动;zh/cantonese 车道的拉丁 run 冻结;只做「变体
  子串 → 正确词」的等位替换,替换次数 ≤ ``max_edits``,其余字符一个不动;
  替换后粤语特征字集合在输出中不得减少(防把粤语纠成普通话)。
- **v1 只吃表**:zh/cantonese 车道不做「同音未列变体」的兜底(那需要拼音
  运行时,违背零依赖),变体表覆盖不到的错字一律原样放行。
- en 车道走词级 snap:``vocab`` 给出品牌/热词英文形,按编辑距离 ≤2 吸附。

资产来源与许可署名(构建期,详见 ``scripts/seed/build_asr_variants.py``):
- 粤语:ToJyutping(BSD-2-Clause)+ rime-cantonese(rime_char.csv,CC-BY-4.0)。
- 普通话:pypinyin(MIT)+ pycorrector 的 same_pinyin.txt / common_char_set.txt
  (Apache-2.0)。
- 常用字门控:pycorrector common_char_set.txt + OpenCC 繁化(s2t)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

__all__ = [
    "PolishResult",
    "detect_lane",
    "load_variant_table",
    "polish_transcript",
]

# 资产路径用 Path(__file__) 定位,保证源码树与安装包内一致可达。
_ASSET_PATH = Path(__file__).resolve().parent / "assets" / "asr_variants.json"

# 粤语特征字稳健子集:取「几乎只在粤语书面出现、不会误伤普通话」的一撮。
# 少即是多——宁可漏判成 zh(退化=不纠错)也不要误判成 cantonese。
_CANTONESE_MARKERS = frozenset("係哋嘅喺唔掂嚟啱嘢乜嘥咁咗嗰啲冇")

# 中文数字词:出现即冻。任务清单为一二三四五六七八九零俩廿百千万亿,另补
# 「两/十」——它们同样是数词且极易与域词同音(十→實/拾),不冻会踩数字铁律。
_CN_NUMERALS = frozenset("一二三四五六七八九零俩廿百千万亿两十")

_DIGIT_RUN_RE = re.compile(r"[0-9]+")
_CN_NUM_RUN_RE = re.compile(r"[一二三四五六七八九零俩廿百千万亿两十]+")
_LATIN_RUN_RE = re.compile(r"[A-Za-z]+")
_ALPHA_TOKEN_RE = re.compile(r"[A-Za-z]+")


@dataclass
class PolishResult:
    """纠错结果。

    ``text``:纠错后文本(超限/守卫触发时=原文)。
    ``edits``:命中并执行的替换,每项 ``(start, end, before, after)``,
    位置是**原始文本**的字符下标;超限放弃时为空列表。
    ``lane``:本次使用的车道(zh/cantonese/en)。
    """

    text: str
    edits: list[tuple[int, int, str, str]] = field(default_factory=list)
    lane: str = "zh"


def load_variant_table(path: str | Path | None = None) -> dict[str, dict[str, list[str]]]:
    """加载变体表资产,返回 ``{lang: {正确词: [变体...]}}``。

    资产带 meta 头,真正表在 ``variants`` 键下;同时也容忍纯表形态(便于测试
    直接喂手写 JSON)。任何 IO/解析错误都向上抛——资产是随源码分发的必备件,
    静默降级只会掩盖打包缺件。
    """
    p = Path(path) if path is not None else _ASSET_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("variants"), dict):
        raw = data["variants"]
    elif isinstance(data, dict):
        raw = data
    else:  # pragma: no cover - 资产被写坏时早失败
        raise ValueError(f"变体表资产结构非法: {p}")
    table: dict[str, dict[str, list[str]]] = {}
    for lang, words in raw.items():
        if not isinstance(words, dict):
            continue
        table[str(lang)] = {
            str(word): [str(v) for v in (variants or [])]
            for word, variants in words.items()
        }
    return table


def detect_lane(text: str) -> str:
    """判定纠错车道:纯拉丁/数字主导 → en;含粤语特征字 → cantonese;否则 zh。

    只对**纯非汉字**输入判 en——汉拉混排(「我的WhatsApp係…」)按汉字段判,
    因为这类转写的主体语言是中文,拉丁只是专名。
    """
    s = str(text or "")
    han = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    latin = sum(1 for c in s if c.isascii() and c.isalpha())
    digit = sum(1 for c in s if c.isdigit())
    if han == 0:
        # 无汉字:有拉丁或数字才算 en,否则(纯符号/空白)退化 zh(不纠错)。
        return "en" if (latin > 0 or digit > 0) else "zh"
    return "cantonese" if any(c in _CANTONESE_MARKERS for c in s) else "zh"


def _protected_spans(text: str, lane: str) -> list[tuple[int, int]]:
    """冻结区间(半开):数字串恒冻;zh/cantonese 另冻拉丁 run。

    en 车道不冻拉丁(拉丁正是要 snap 的对象),数字仍冻。
    """
    spans: list[tuple[int, int]] = []
    for rx in (_DIGIT_RUN_RE, _CN_NUM_RUN_RE):
        spans.extend((m.start(), m.end()) for m in rx.finditer(text))
    if lane in ("zh", "cantonese"):
        spans.extend((m.start(), m.end()) for m in _LATIN_RUN_RE.finditer(text))
    spans.sort()
    return spans


def _overlaps(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    for s, e in spans:
        if s >= end:
            break
        if e > start:
            return True
    return False


def _marker_count(text: str) -> int:
    return sum(1 for c in text if c in _CANTONESE_MARKERS)


def _reverse_map(table: Mapping[str, Mapping[str, Sequence[str]]], lane: str) -> dict[str, str]:
    """``{变体: 正确词}``。冲突(同一变体挂多词)时**长正确词优先**——更长
    的短语更具体,避免短词把长词的变体抢走;同长按字典序,保证与 JSON 键序
    无关的确定性。
    """
    rev: dict[str, str] = {}
    words = table.get(lane) or {}
    for correct in sorted(words.keys(), key=lambda w: (-len(w), w)):
        for variant in words.get(correct) or []:
            v = str(variant)
            if not v or v == correct:
                continue
            rev.setdefault(v, str(correct))
    return rev


def _scan_matches(
    text: str,
    rev: Mapping[str, str],
    protected: Sequence[tuple[int, int]],
) -> list[tuple[int, int, str, str]]:
    """全串扫描变体命中,返回 ``(start, end, before, after)``(原始坐标)。

    逐位**最长匹配优先**:从当前位可选的最长变体开始试,命中即跳过整段,
    避免「短变体切碎长变体」。命中区间与任何冻结区间相交即放弃该位。
    """
    if not rev:
        return []
    max_len = max(len(v) for v in rev)
    out: list[tuple[int, int, str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        hit_end = -1
        hit_after = ""
        for length in range(min(max_len, n - i), 1, -1):  # 变体最短 2 字
            sub = text[i : i + length]
            correct = rev.get(sub)
            if correct is not None:
                end = i + length
                if not _overlaps(i, end, protected):
                    hit_end = end
                    hit_after = correct
                    break
        if hit_end > 0:
            out.append((i, hit_end, text[i:hit_end], hit_after))
            i = hit_end
        else:
            i += 1
    return out


def _edit_distance(a: str, b: str) -> int:
    """零依赖 Levenshtein 距离(小 DP,只用两行滚动数组)。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _vocab_snap(
    text: str,
    vocab: Sequence[str],
    max_edits: int,
) -> list[tuple[int, int, str, str]]:
    """英文词级 snap:token 窗口与 vocab 词编辑距离 ≤2 且长度 ≥4 时替换。

    规则:
    - 数字一律不动(alpha token 正则天然不含数字;窗口跨数字则整窗放弃)。
    - 全大写缩写且 ≤3 字母不动(如 SF / AI,编辑距离噪声大)。
    - 与多个 vocab 词**等距**(歧义)时不动——宁缺毋滥。
    - 窗口按词数从多到少试,优先吸附多词品牌(「SF Expres」→「SF Express」)。
    """
    words = [str(w) for w in (vocab or []) if str(w).strip()]
    if not words:
        return []
    low_words = [(w, w.lower()) for w in words]
    max_words = max(len(w.split()) for w in words)
    tokens = [(m.start(), m.end(), m.group(0)) for m in _ALPHA_TOKEN_RE.finditer(text)]
    if not tokens:
        return []

    out: list[tuple[int, int, str, str]] = []
    i = 0
    while i < len(tokens):
        chosen: tuple[int, int, str, str] | None = None
        for w in range(max_words, 0, -1):
            if i + w > len(tokens):
                continue
            start = tokens[i][0]
            end = tokens[i + w - 1][1]
            window = text[start:end]
            if re.search(r"[0-9]", window):
                continue  # 跨数字窗口不碰(冻结铁律)
            if len(window) < 4:
                continue
            if window.isupper() and len(window) <= 3:
                continue
            low = window.lower()
            best_dist = 99
            best_targets: list[str] = []
            for w_orig, w_low in low_words:
                if len(w_orig) < 4:
                    continue
                d = _edit_distance(low, w_low)
                if d < best_dist:
                    best_dist = d
                    best_targets = [w_orig]
                elif d == best_dist:
                    best_targets.append(w_orig)
            if best_dist > 2 or len(best_targets) != 1:
                continue  # 超阈值或歧义 → 不动
            if best_targets[0] == window:
                continue
            chosen = (start, end, window, best_targets[0])
            break  # 词数多的窗口优先
        if chosen is not None:
            out.append(chosen)
            # 跳过被消费的 token
            consumed = chosen[1]
            i += 1
            while i < len(tokens) and tokens[i][0] < consumed:
                i += 1
            if len(out) > max_edits:
                return out  # 超限信号交由上层整层放弃
        else:
            i += 1
    return out


def _apply_edits(
    text: str,
    edits: Sequence[tuple[int, int, str, str]],
) -> str:
    """按原始坐标从后往前替换,支持变长(仅 span 内变化,占位不动)。"""
    out = text
    for start, end, _before, after in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + after + out[end:]
    return out


def polish_transcript(
    text: str,
    lane: str,
    variant_table: Mapping[str, Mapping[str, Sequence[str]]],
    *,
    vocab: Iterable[str] | None = None,
    max_edits: int = 2,
) -> PolishResult:
    """对单条转写做确定性音近纠错。

    参数:
    - ``lane``:车道(``zh``/``cantonese``/``en``);空串时回退 ``detect_lane``。
    - ``variant_table``:``{lang: {正确词: [变体...]}}``。
    - ``vocab``:仅 en 车道使用,品牌/热词英文形列表。
    - ``max_edits``:替换次数上限;命中数**超过**即整层放弃(返回原文、edits
      为空)——不做「改前 N 个」的部分纠错,避免半纠文本比原文更难判读。

    守卫顺序:先按车道求出候选 edits → 超限即放弃 → 应用 → 粤语特征字不减
    校验(仅 cantonese 车道,减少则整层放弃)。
    """
    original = str(text or "")
    lane = lane or detect_lane(original)
    cap = max(0, int(max_edits))

    if not original:
        return PolishResult(text=original, edits=[], lane=lane)

    if lane == "en":
        edits = _vocab_snap(original, list(vocab or []), cap)
        if len(edits) > cap:
            return PolishResult(text=original, edits=[], lane=lane)
    else:
        rev = _reverse_map(variant_table, lane)
        protected = _protected_spans(original, lane)
        edits = _scan_matches(original, rev, protected)
        if len(edits) > cap:
            return PolishResult(text=original, edits=[], lane=lane)

    if not edits:
        return PolishResult(text=original, edits=[], lane=lane)

    polished = _apply_edits(original, edits)

    # 语言不变性:粤语特征字不得减少(防变体表把粤语纠成普通话书面)。
    if lane == "cantonese" and _marker_count(polished) < _marker_count(original):
        return PolishResult(text=original, edits=[], lane=lane)

    return PolishResult(text=polished, edits=list(edits), lane=lane)
