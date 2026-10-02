#!/usr/bin/env python3
"""离线构建 ASR 音近变体词表资产(2026-09-27)。

产物:``packages/core/bok_voice_core/assets/asr_variants.json``
结构:``{"meta": {...}, "variants": {lang: {正确词: [变体...]}}}``
运行时核心 ``bok_voice_core.asr_polish`` 只读这份静态资产,**零第三方依赖**;
以下构建期依赖永远不得进入运行时模块。

## dev 依赖安装(独立 venv,**不要装进仓的 .venv312**)

    python3.12 -m venv /tmp/homophone-research/.venv
    /tmp/homophone-research/.venv/bin/pip install \\
        ToJyutping pycantonese pypinyin opencc-python-reimplemented pycorrector

## 数据文件(放在 ``--data-dir``,默认 /tmp/homophone-research)

- ``rime_char.csv``  —— rime-cantonese 字表(char,jyutping,pron_rank,...),
  粤语反查同音字的**必需件**。取法:clone rime-cantonese 上游仓库后导出
  ``jyut6ping3.char.dict.yaml``;或直接从上一轮调研目录拷贝。
- ``rime_word.csv``  —— 可选,用于「常用字」频次门控(缺省则只用
  common_char_set + OpenCC 繁化)。
- pycorrector 的 ``common_char_set.txt`` / ``same_pinyin.txt`` 会自动从其
  site-packages 定位,无需手动拷。

## 许可署名义务(产物头已带 sources 段,改数据源须同步改注释)

- ToJyutping —— BSD-2-Clause。
- rime-cantonese —— CC-BY-4.0(**要求署名**,产物 meta.sources 已列)。
- pypinyin —— MIT;pycorrector 数据 —— Apache-2.0。
- OpenCC —— Apache-2.0。

## 用法

    /tmp/homophone-research/.venv/bin/python scripts/build_asr_variants.py
    # 追加自定义映射(JSON: {"zh": {"顺丰": ["顺风"]}, ...})
    ... --extra my_extra.json
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import importlib.util
import itertools
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# 种子词表(域词)。粤语用繁形,普通话用简形;拉丁词归 en 车道(供运行时 vocab)。
# ---------------------------------------------------------------------------

_SEED_CANTONESE = [
    # 快递平台
    "順豐", "京東", "拼多多", "中通", "圓通", "韻達", "郵政",
    # 渠道/平台
    "微信", "淘寶", "抖音",
    # 客服/纠纷域
    "詐騙", "賠償", "投訴", "退款", "退貨", "單號", "快遞", "倉庫",
    "賠付", "理賠", "售後",
]

_SEED_ZH = [
    "顺丰", "京东", "拼多多", "中通", "圆通", "韵达", "邮政",
    "微信", "淘宝", "抖音",
    "诈骗", "赔偿", "投诉", "退款", "退货", "单号", "快递", "仓库",
    "赔付", "理赔", "售后",
]

# 拉丁词只进 meta.en_seeds,供接线层喂 polish_transcript(vocab=...) 起手。
_SEED_EN = ["WhatsApp", "SF Express", "Pinduoduo", "JD", "Taobao", "Douyin"]

# 人工核过的确定性音近对(原型实测/现场高频)。排在任何自动生成结果之前,
# 保证 top-k 一定含有这些高价值吸附(如「順風→順豐」在码位排序里会被挤出)。
_CURATED_CANTONESE = {
    "順豐": ["順風"],
}
_CURATED_ZH = {
    "顺丰": ["顺风"],
    "京东": ["金东"],
    "单号": ["单浩"],
}

# 候选枚举上限:防 4 字长词组合爆炸;对现有种子(每字候选 <30)不触顶、零质量损失。
_MAX_POS_CANDS = 30
_MAX_COMBOS = 200_000

# rime_char.csv 里可用于反查的读音等级。
_RANK_OK = {"預設", "常用"}

# 粤语声调轮廓(阴平5-5…),用于声调近邻判定。
_TONE_CONTOUR = {"1": (5, 5), "2": (3, 5), "3": (3, 3), "4": (2, 1), "5": (1, 3), "6": (2, 2)}

# 普通话模糊音规则(平翘舌/前后鼻/n-l/f-h/r-l)。
_INI_FUZZY = {"zh": ["z"], "ch": ["c"], "sh": ["s"], "n": ["l"], "l": ["n"],
              "f": ["h"], "h": ["f"], "r": ["l"]}
_FIN_FUZZY = {
    "ang": ["an"], "an": ["ang"], "eng": ["en"], "en": ["eng"],
    "ing": ["in"], "in": ["ing"], "iang": ["ian"], "ian": ["iang"],
    "uang": ["uan"], "uan": ["uang"], "ong": ["on"], "ong": ["en"],
    "uo": ["o"], "o": ["uo"], "ai": ["ei"], "ei": ["ai"],
}
_INI_RE = re.compile(r"^(zh|ch|sh|[bpmfdtnlgkhjqxrzcsyw])?([a-zü]+)([1-5])?$")


def _die(msg: str) -> None:
    print(f"[build_asr_variants] {msg}", file=sys.stderr)
    raise SystemExit(2)


def _require(mod: str, hint: str):
    """惰性导入构建期依赖;缺失时给人话指引而非 ImportError 栈。"""
    try:
        return __import__(mod)
    except ImportError:
        _die(f"缺少构建依赖 {mod!r}。{hint}")


def _pkg_data_dir(pkg: str) -> Path | None:
    """不经 import 定位包数据目录(pycorrector 顶层 import 会拖 torch)。"""
    spec = importlib.util.find_spec(pkg)
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).resolve().parent / "data"


# ---------------------------------------------------------------------------
# 资源加载
# ---------------------------------------------------------------------------

class _Resources:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.char_csv = data_dir / "rime_char.csv"
        if not self.char_csv.exists():
            _die(f"缺 rime_char.csv({self.char_csv});见脚本头数据获取说明")
        # 粤语反查:音节 -> [字];字 -> [(音节, 等级)]
        self.j2c: dict[str, list[str]] = defaultdict(list)
        self.c2j: dict[str, list[tuple[str, str]]] = defaultdict(list)
        with self.char_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ch, jy, rank = row["char"], row["jyutping"], row.get("pron_rank", "")
                if not ch or not jy:
                    continue
                self.c2j[ch].append((jy, rank))
                self.j2c[jy].append(ch)
        # 可选:词表频次(常用字门控补充)
        self.char_freq: Counter[str] = Counter()
        self.word_csv = data_dir / "rime_word.csv"
        if self.word_csv.exists():
            with self.word_csv.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    for c in row.get("char", ""):
                        self.char_freq[c] += 1
        # 常用字门控:pycorrector common_char_set + OpenCC 繁化(粤语侧)
        common_dir = _pkg_data_dir("pycorrector")
        if common_dir is None or not (common_dir / "common_char_set.txt").exists():
            _die("找不到 pycorrector 的 common_char_set.txt(装 pycorrector 或拷入 data-dir)")
        self.common_simp = set((common_dir / "common_char_set.txt").read_text(encoding="utf-8").split())
        self._common_trad: set[str] | None = None
        # 普通话反查(tone3 拼音 -> 常用字),由 build() 注入。
        self.t3_to_chars: dict[str, list[str]] = {}
        # 普通话同音表
        self.same_pinyin: dict[str, tuple[set[str], set[str]]] = {}
        sp = common_dir / "same_pinyin.txt"
        if sp.exists():
            for line in sp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 3 or len(parts[0]) != 1:
                    continue
                self.same_pinyin[parts[0]] = (set(parts[1]), set(parts[2]))

    def common_trad(self) -> set[str]:
        if self._common_trad is None:
            opencc = _require("opencc", "pip install opencc-python-reimplemented")
            cc = opencc.OpenCC("s2t")
            self._common_trad = set(cc.convert("".join(sorted(self.common_simp))))
        return self._common_trad

    def is_common_canto(self, c: str) -> bool:
        return c in self.common_trad() or self.char_freq.get(c, 0) >= 3

    def is_common_zh(self, c: str) -> bool:
        return c in self.common_simp


# ---------------------------------------------------------------------------
# 粤语车道(镜像 /tmp/homophone-research/proto2.py,已实测)
# ---------------------------------------------------------------------------

def _lazy_variants(syl: str) -> dict[str, str]:
    """一个粤拼音节 -> {近音音节: 规则名}(懒音/前后鼻/塞音尾交替)。"""
    m = re.match(r"^([a-z]+)([1-6])$", syl)
    if not m:
        return {}
    base, tone = m.groups()
    out: dict[str, str] = {}

    def add(nb: str, tag: str) -> None:
        if nb and re.match(r"^[a-z]+[1-6]$", nb + tone):
            out.setdefault(nb + tone, tag)

    if base.startswith("ng") and len(base) > 2:
        add(base[2:], "ng-drop")
    if re.match(r"^[aeiou]", base):
        add("ng" + base, "ng-add")
    if base.startswith("n") and not base.startswith("ng"):
        add("l" + base[1:], "n/l")
    if re.match(r"^(g|k)wo", base):
        add(base[0] + "o" + base[3:], "gw/kw")
    if base == "ng":
        add("m", "ng/m")
    if base.endswith("ng"):
        add(base[:-2] + "n", "ng/n-coda")
    if base.endswith("n"):
        add(base + "g", "n/ng-coda")
    if base.endswith("t"):
        add(base[:-1] + "k", "t/k-coda")
    if base.endswith("k"):
        add(base[:-1] + "t", "k/t-coda")
    if base.endswith("m") and len(base) > 1:
        add(base[:-1] + "n", "m/n-coda")
    if base.endswith("n") and len(base) > 1:
        add(base[:-1] + "m", "n/m-coda")
    if base.endswith("p"):
        add(base[:-1] + "t", "p/t-coda")
    if "eo" in base:
        add(base.replace("eo", "oe"), "eo/oe")
    if "oe" in base:
        add(base.replace("oe", "eo"), "eo/oe")
    if "aa" in base:
        add(re.sub("aa", "a", base), "aa/a")
    return out


def _tone_near(t1: str, t2: str) -> int:
    a, b = _TONE_CONTOUR[t1], _TONE_CONTOUR[t2]
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _canto_options(res: _Resources, word: str, tojyutping):
    """逐字候选:ToJyutping 多音字候选 + rime 多音字 + 声调近邻 + 懒音。

    代价标签 exact/polyphone < tone < lazy,供组装时排序。
    """
    entries = tojyutping.get_jyutping_candidates(word)
    per_char = []
    for ch, c_list in entries:
        if not ("\u4e00" <= ch <= "\u9fff"):
            continue  # 只对汉字做候选(拉丁/数字不纠)
        if not c_list:
            return []
        opts: dict[str, str] = {}
        # 第一候选=该字在此词中最可能的读音 → exact;其余候选=多音字。
        opts[c_list[0]] = "exact"
        for j in c_list[1:]:
            opts.setdefault(j, "polyphone")
        # rime_char 的預設/常用读音补充(与 ToJyutping 候选互为补集)。
        for (j, rank) in res.c2j.get(ch, []):
            if rank in _RANK_OK:
                opts.setdefault(j, "polyphone")
        # 声调近邻(仅对 exact/polyphone 档派生,避免从懒音再派生)。
        for s in list(opts):
            bm = re.match(r"^([a-z]+)([1-6])$", s)
            if not bm:
                continue
            base, tone = bm.groups()
            for t2 in "123456":
                if t2 != tone and _tone_near(tone, t2) <= 2:
                    opts.setdefault(base + t2, "tone")
        # 懒音/前后鼻/塞音尾交替(对当前全部候选派生)。
        for s in list(opts):
            for v, tag in _lazy_variants(s).items():
                opts.setdefault(v, "lazy:" + tag)
        per_char.append((ch, opts))
    return per_char


def _cost_canto(tag: str) -> int:
    # 精确同音/多音字 < 仅声调 < 懒音(模糊)
    if tag in ("exact", "polyphone"):
        return 1
    if tag == "tone":
        return 2
    return 3


def canto_variants(res: _Resources, word: str, tojyutping, k: int = 10) -> list[str]:
    per_char = _canto_options(res, word, tojyutping)
    if not per_char:
        return []
    char_lists = []
    for ch, opts in per_char:
        cs: dict[str, tuple[str, int]] = {ch: ("keep", 0)}
        for syl, tag in opts.items():
            for c in res.j2c.get(syl, []):
                if not res.is_common_canto(c):
                    continue
                cur = cs.get(c)
                # 同字可由多条读音路径到达:精确/多音字档优先于声调/懒音档。
                if cur is None or (tag in ("exact", "polyphone") and cur[0] not in ("exact", "polyphone")):
                    cs[c] = (tag, _cost_canto(tag))
        # 稳定排序后截断,保证枚举确定且不爆组合
        ranked = sorted(cs.items(), key=lambda kv: (kv[1][1], kv[0]))[:_MAX_POS_CANDS]
        char_lists.append(dict(ranked))
    cands = []
    for combo in itertools.product(*[list(cl.keys()) for cl in char_lists]):
        variant = "".join(combo)
        if variant == word:
            continue
        penalty = 0
        for pos, c in enumerate(combo):
            tag, cost = char_lists[pos][c]
            penalty += cost
        commons = sum(1 for c in combo if res.is_common_canto(c))
        cands.append((penalty, -commons, variant))
        if len(cands) >= _MAX_COMBOS:
            break
    cands.sort()
    seen: set[str] = set()
    out: list[str] = []
    for _p, _nc, v in cands:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# 普通话车道(镜像 /tmp/homophone-research/proto_mandarin.py + same_pinyin)
# ---------------------------------------------------------------------------

def _split_syl(t3: str) -> tuple[str | None, str | None, str]:
    t = re.sub(r"\d", "", t3)
    m_tone = re.search(r"\d", t3)
    tone = m_tone.group(0) if m_tone else "5"
    m = _INI_RE.match(t + tone)
    if not m:
        return None, None, tone
    return (m.group(1) or ""), m.group(2), tone


def _fuzzy_syls(t3: str) -> dict[str, str]:
    ini, fin, tone = _split_syl(t3)
    if ini is None:
        return {}
    out: dict[str, str] = {}

    def put(i: str, f: str, tn: str, tag: str) -> None:
        s = f"{i}{f}{tn}"
        if s and s != t3:
            out.setdefault(s, tag)

    for i2 in _INI_FUZZY.get(ini, []):
        put(i2, fin, tone, "ini")
    for f2 in _FIN_FUZZY.get(fin, []):
        put(ini, f2, tone, "fin")
    for i2 in _INI_FUZZY.get(ini, []):
        for f2 in _FIN_FUZZY.get(fin, []):
            put(i2, f2, tone, "ini+fin")
    return out


def _cost_zh(tag: str) -> int:
    """普通话候选代价:精确同音 < 仅声调 < 模糊音(单侧变形 < 声韵双变)。"""
    if tag == "exact":
        return 1
    if tag == "tone":
        return 2
    if tag == "fuzzy:ini+fin":
        return 4
    return 3  # fuzzy:ini / fuzzy:fin


def mandarin_variants(res: _Resources, word: str, pypinyin_mod, k: int = 10) -> list[str]:
    pinyin = pypinyin_mod.pinyin
    Style = pypinyin_mod.Style
    seq = pinyin(word, style=Style.TONE3, errors="default")
    per_char: list[tuple[str, dict[str, str]]] = []
    for ch, (p,) in zip(word, seq):
        if not ("\u4e00" <= ch <= "\u9fff"):
            continue
        opts: dict[str, str] = {p: "exact"}
        ini, fin, tone = _split_syl(p)
        if ini is None:
            per_char.append((ch, {}))
            continue
        for t2 in "12345":
            if t2 != tone:
                opts.setdefault(f"{ini}{fin}{t2}", "tone")
        for s, tag in _fuzzy_syls(p).items():
            opts.setdefault(s, f"fuzzy:{tag}")
        per_char.append((ch, opts))
    if not per_char or any(not pc[1] for pc in per_char):
        return []
    chars_orig = [ch for ch, _ in per_char]
    char_lists = []
    for ch, opts in per_char:
        cs: dict[str, tuple[str, int]] = {}
        for syl, tag in opts.items():
            for c in res.t3_to_chars.get(syl, []):
                if not res.is_common_zh(c):
                    continue
                cur = cs.get(c)
                if cur is None or (tag == "exact" and cur[0] != "exact"):
                    cs[c] = (tag, _cost_zh(tag))
        # same_pinyin.txt 补充:同音同调 → exact;同音异调 → tone
        same, diff = res.same_pinyin.get(ch, (set(), set()))
        for c in same:
            if c != ch and res.is_common_zh(c):
                cs.setdefault(c, ("exact", 1))
        for c in diff:
            if c != ch and res.is_common_zh(c):
                cs.setdefault(c, ("tone", 2))
        cs.setdefault(ch, ("keep", 0))
        ranked = sorted(cs.items(), key=lambda kv: (kv[1][1], kv[0]))[:_MAX_POS_CANDS]
        char_lists.append(dict(ranked))
    cands = []
    for combo in itertools.product(*[list(cl.keys()) for cl in char_lists]):
        v = "".join(combo)
        if v == word:
            continue
        pen = 0
        for pos, c in enumerate(combo):
            if c == chars_orig[pos]:
                continue
            pen += char_lists[pos][c][1]
        commons = sum(1 for c in combo if res.is_common_zh(c))
        cands.append((pen, -commons, v))
        if len(cands) >= _MAX_COMBOS:
            break
    cands.sort()
    seen: set[str] = set()
    out: list[str] = []
    for _p, _nc, v in cands:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def _build_mandarin_index(res: _Resources, pypinyin_mod) -> dict[str, list[str]]:
    """pypinyin 字典反查:(tone3 拼音) -> 常用字列表。"""
    from pypinyin.contrib.tone_convert import to_tone3

    pyd = Path(importlib.util.find_spec("pypinyin").origin).resolve().parent / "pinyin_dict.json"
    raw = json.loads(pyd.read_text(encoding="utf-8"))
    idx: dict[str, list[str]] = defaultdict(list)
    for cp, readings in raw.items():
        ch = chr(int(cp))
        for rd in readings.split(","):
            t3 = to_tone3(rd)
            if t3:
                idx[t3].append(ch)
    return idx


def _merge_variants(
    explicit: list[str], generated: list[str], word: str, k: int
) -> list[str]:
    """合并「人工显式」(curated/--extra,最高优先)与「自动生成」。

    自动生成结果强制与正确词**同长**(等位替换契约,生成器天然满足);人工
    显式条目允许变长(运营可显式写跨长映射,运行时支持),但只在 --extra 里
    才会出现。
    """
    out: list[str] = []
    seen: set[str] = set()
    for v in list(explicit):
        v = str(v)
        if not v or v == word or v in seen:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= k:
            return out
    for v in list(generated):
        if not v or v == word or v in seen:
            continue
        if len(v) != len(word):
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= k:
            break
    return out


def build(args: argparse.Namespace) -> dict:
    data_dir = Path(args.data_dir).expanduser().resolve()
    res = _Resources(data_dir)
    tojyutping = _require("ToJyutping", "pip install ToJyutping")
    pypinyin_mod = _require("pypinyin", "pip install pypinyin")
    res.t3_to_chars = _build_mandarin_index(res, pypinyin_mod)  # type: ignore[attr-defined]

    extra = {"zh": {}, "cantonese": {}}
    if args.extra:
        raw = json.loads(Path(args.extra).read_text(encoding="utf-8"))
        for lang in ("zh", "cantonese"):
            for w, vs in (raw.get(lang) or {}).items():
                extra[lang].setdefault(w, [])
                extra[lang][w].extend(str(v) for v in vs)

    variants: dict[str, dict[str, list[str]]] = {"zh": {}, "cantonese": {}, "en": {}}

    def seed_words(seeds: list[str], extra_lang: dict[str, list[str]]) -> list[str]:
        """种子 + --extra 追加的新词,保序去重(种子在前)。"""
        out: list[str] = []
        seen: set[str] = set()
        for w in list(seeds) + list(extra_lang.keys()):
            if w and w not in seen:
                seen.add(w)
                out.append(w)
        return out

    # 粤语(繁形)
    for word in seed_words(_SEED_CANTONESE, extra["cantonese"]):
        gen = canto_variants(res, word, tojyutping, k=args.k)
        variants["cantonese"][word] = _merge_variants(
            _CURATED_CANTONESE.get(word, []) + extra["cantonese"].get(word, []), gen, word, args.k
        )
    # 普通话(简形)
    for word in seed_words(_SEED_ZH, extra["zh"]):
        gen = mandarin_variants(res, word, pypinyin_mod, k=args.k)
        variants["zh"][word] = _merge_variants(
            _CURATED_ZH.get(word, []) + extra["zh"].get(word, []), gen, word, args.k
        )

    # 卫生:删掉「变体本身就是本车道某个正确词」的条目,防运行时反查歧义。
    for lang in ("zh", "cantonese"):
        keys = set(variants[lang])
        for word, vs in variants[lang].items():
            variants[lang][word] = [v for v in vs if v not in keys]

    meta = {
        "version": 1,
        "built_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "k": args.k,
        "seed_summary": {
            "zh_seeds": len(_SEED_ZH),
            "cantonese_seeds": len(_SEED_CANTONESE),
            "en_seeds": len(_SEED_EN),
            "extra_file": str(args.extra) if args.extra else None,
        },
        "en_seeds": _SEED_EN,
        "counts": {
            lang: {
                "words": len(variants[lang]),
                "variants": sum(len(v) for v in variants[lang].values()),
            }
            for lang in ("zh", "cantonese", "en")
        },
        "sources": [
            "ToJyutping (BSD-2-Clause)",
            "rime-cantonese rime_char.csv (CC-BY-4.0) — attribution required",
            "pypinyin (MIT)",
            "pycorrector same_pinyin.txt / common_char_set.txt (Apache-2.0)",
            "OpenCC s2t (Apache-2.0)",
        ],
    }
    return {"meta": meta, "variants": variants}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建 ASR 音近变体词表资产")
    ap.add_argument("--data-dir", default="/tmp/homophone-research",
                    help="rime_char.csv/rime_word.csv 所在目录(默认 /tmp/homophone-research)")
    ap.add_argument("--extra", default=None, help="追加映射 JSON(可选)")
    ap.add_argument("--out", default=None, help="输出路径(默认仓内 assets/asr_variants.json)")
    ap.add_argument("--k", type=int, default=10, help="每词变体上限(默认 10)")
    args = ap.parse_args(argv)

    payload = build(args)
    root = Path(__file__).resolve().parents[1]
    out = Path(args.out) if args.out else root / "packages" / "core" / "bok_voice_core" / "assets" / "asr_variants.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    c = payload["meta"]["counts"]
    print(f"[build_asr_variants] wrote {out}")
    print(f"  zh        : {c['zh']['words']} words / {c['zh']['variants']} variants")
    print(f"  cantonese : {c['cantonese']['words']} words / {c['cantonese']['variants']} variants")
    print(f"  en        : {c['en']['words']} words (vocab seeds in meta)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
