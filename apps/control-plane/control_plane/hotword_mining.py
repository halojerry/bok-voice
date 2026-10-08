"""ASR 热词沉淀纯函数（EX-H1，2026-09-28，防 main.py / qa_cluster.py 膨胀）。

把真实通话转写里「客户常说但 ASR 听不好」的词挖成可采纳的热词候选，喂
`hotword_entries` 表（采纳后经 GET /api/asr/hotwords 下发，agent 侧做 ASR
biasing）。与 qa_cluster/gap_proposals 同分工：本模块只有可离线单测的纯函数，
LLM 调用/单飞/缓存全在 qa_cluster runner；CP 侧**不 import agent_runtime**
（云端镜像不含 apps/agent）——重复用语族以 `REPEAT_PHRASES` 镜像
apps/agent/agent_runtime/flow.py 的 `_REPEAT_EXPLICIT_RE`，镜像一致性由
tests/test_hotword_mining.py 用 pathlib 读源文件钉住（同 gap_proposals 对
flow.py `_BRANCH_LINE_RE` 的镜像先例）。

三源（kind）:
- ``polish_fix``：客户轮过一遍运行时同款确定性音近纠错（``asr_polish.polish_transcript``，
  与 scripts/seed/prepare_csc_data.py 的「edits 非空即采」逐字同姿势），raw≠polished
  时取**纠错后 span 的词**为候选（这就是 ASR 听错、应进词表让 ASR 偏置正确的词）。
- ``near_miss``：客户轮**近似**命中「要求重复/没听清」规范用语族（编辑距离预算内
  的同长窗口：≤4 字短语容 1 字差、≥5 字容 2，见 ``near_miss_edit_budget``）——
  候选=该规范用语（让 ASR 把客户喊的「冇听清/再讲一次」识别稳）。
- ``gap_ngram``：反复出现（≥3 次）却没有任何 qa 词条问法覆盖的客户 n-gram
  （2-4 个 CJK 字或单个拉丁词，casefold）+ 尚不在热词表。

## IRON FILTERS（抽取期，tests 钉住）

(i) **数字铁律**：任何含数字 run 或数字主导的候选一律丢（`is_digit_dominant`，
抽取与 GET 出仓两侧同滤）；
(ii) **回声轮排除**：`turn_quality.looks_garbled(text, hotword_terms)` 为真
（剥数字/标点/词表命中后无实质内容的碎片/抄词轮）整轮跳过；
(iii) **语言**：候选 lang=该通通话语言（turns 带 language，缺省 zh），三态
zh/cantonese/en（cantonese 为规范拼写）；
(iv) **子串吞噬**：同批（同语言）候选归一后互为真子串 → 丢短留长
（``filter_substring_swallowed``，聚合出口、每语言截断前）——下游热词表预算
（豆包 100 token 上限取前 40 词、总长 ≤200 字符）每一槽都该花在长词上，
短碎片只稀释。

`existing_words`=已入库热词（两级合并，含停用行——避免重复提议）；`existing_q_norms`
=同账号 qa 词条归一后问法集（gap_ngram 的 qa 覆盖排除用，复用 qa_text.normalize_question
同源归一）。输出每语言按 freq 降序取前 ~20。
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache

from bok_voice_core.asr_polish import detect_lane, load_variant_table, polish_transcript
from bok_voice_core.turn_quality import looks_garbled

# ---- 三源 kind 与优先级（同词多源命中时取优先级高者做展示 kind）----

KIND_POLISH_FIX = "polish_fix"
KIND_NEAR_MISS = "near_miss"
KIND_GAP_NGRAM = "gap_ngram"
KIND_PRIORITY = {KIND_POLISH_FIX: 0, KIND_NEAR_MISS: 1, KIND_GAP_NGRAM: 2}

# 每语言候选上限（任务书 ~20）、每候选证据样本上限。
PER_LANG_CAP = 20
EVIDENCE_MAX = 3

# gap_ngram：n-gram 长度窗（CJK 2-4 字）与拉丁词最小长度（滤 a/is/ok 类碎片）。
NGRAM_MIN = 2
NGRAM_MAX = 4
LATIN_WORD_MIN = 3
NGRAM_MIN_FREQ = 3

# near_miss：只对短轮做窗口扫描（重复用语是短句；长轮既非近似用语又太贵）。
NEAR_MISS_MAX_CHARS = 60
NEAR_MISS_MAX_EDITS = 2
# ≤4 字（CJK 规范语族全长 2-4 字）只容 1 字之差——见 near_miss_edit_budget。
NEAR_MISS_SHORT_MAX_CHARS = 4

CANONICAL_LANGS = ("zh", "cantonese", "en")

# flow.py `_REPEAT_EXPLICIT_RE` 短语族的**字节镜像**（顺序=正则 alternation 顺序；
# 改 flow.py 该正则须同步本元组，tests/test_hotword_mining.py 用 pathlib 读源钉住）。
REPEAT_PHRASES: tuple[str, ...] = (
    "听唔清", "聽唔清", "听不清", "聽不清", "冇聽清", "冇听清", "没听清",
    "聽唔到", "听唔到", "听不到", "听不见", "聽唔見",
    "再说一次", "再說一次", "再讲一次", "再講一次", "再说一遍", "再講一遍",
    "再讲一遍", "讲多次", "講多次", "再讲啦", "再講啦",
    "乜嘢话", "乜嘢啊", "咩话", "咩話", "你说什么", "你說什麼", "你讲乜", "你講乜",
    "大声啲", "大聲啲",
    "repeat", "pardon", "say again", "come again", "didn'?t hear", "can'?t hear",
)


def _display_phrase(phrase: str) -> str:
    """正则短语 → 人话候选形：剥掉可选撇号量词 ``'?`` 里的 ``?``。

    flow.py 里 ``didn'?t hear`` 表示撇号可选；下发的热词应是 ``didn't hear``。
    """
    return str(phrase).replace("'?", "'").replace("?", "")


# 匹配形（小写）→ 展示形（人话）。拉丁短语 casefold 匹配，CJK 原样。
_CANONICAL: tuple[tuple[str, str], ...] = tuple(
    (_display_phrase(p).casefold(), _display_phrase(p)) for p in REPEAT_PHRASES
)

# ---- 数字铁律（抽取 / 出仓同用）----

_ASCII_DIGIT_RE = re.compile(r"[0-9０-９]")
_CJK_NUM_RUN_RE = re.compile(r"[零〇一二两三四五六七八九十百千万亿]{2,}")
_CJK_NUMERALS = "零〇一二两三四五六七八九十百千万亿"


def is_digit_dominant(word: str) -> bool:
    """数字 run 或数字主导 → True（候选丢弃；GET 出仓侧同滤）。

    - 含任意 ASCII/全角数字 → True（数字串绝不进热词，repo 铁律）；
    - 含 ≥2 个连续中文数词（「一二三」「九九」）→ True（号码类形）；
    - 中文数词 ≥2 且占词长一半以上 → True（「三号码」类）。
    单个中文数词（「三通」「九龙」等真词）不误杀。
    """
    s = str(word or "").strip()
    if not s:
        return True
    if _ASCII_DIGIT_RE.search(s):
        return True
    if _CJK_NUM_RUN_RE.search(s):
        return True
    cn = sum(s.count(c) for c in _CJK_NUMERALS)
    return cn >= 2 and cn * 2 >= len(s)


# ---- 音近纠错表（懒加载 + fail-soft；CP 单进程一份）----


@lru_cache(maxsize=1)
def _variant_table() -> dict:
    try:
        return load_variant_table()
    except Exception:  # noqa: BLE001 - 资产缺失/损坏=零 polish_fix，不炸挖掘
        return {}


def _conv_lang(conversation: list[dict]) -> str:
    """通话语言 = 首个带语言的客户轮 lang（A 线每通语言固定），缺省 zh。"""
    for turn in conversation or []:
        if str(turn.get("role") or "") != "user":
            continue
        lang = str(turn.get("lang") or "")
        if lang:
            return lang if lang in CANONICAL_LANGS else "zh"
    return "zh"


# ---- 重复用语近近似（编辑距离窗口）----


def _edit_distance(a: str, b: str) -> int:
    """零依赖 Levenshtein（两行滚动数组）。"""
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


def near_miss_edit_budget(phrase_len: int) -> int:
    """near_miss 窗口编辑预算（按短语长度分档；EX-H1 遗留② 证据样失配修复）。

    ≤4 字（REPEAT_PHRASES 的 CJK 族全长 2-4 字）只容 **1** 字之差：d=2 的同长
    窗口与短语仅共享单字（「听清啊」≈「乜嘢啊」、「你说什」≈「你讲乜」），会把
    整个规范语族从一句话里炸成一堆候选，而证据样（客户原话）与多数候选词根本
    对不上——运营者看到「证据：你说什幺 → 你讲乜」这类失配样本。≥5 字（拉丁族
    repeat/pardon/say again…）保持 2（拼错两个字母仍算近似）。调用方须再与
    ``phrase_len - 1`` 取 min（2 字短语不容整词替换，「至少一字相同」护栏）。
    """
    return 1 if phrase_len <= NEAR_MISS_SHORT_MAX_CHARS else NEAR_MISS_MAX_EDITS


def _near_miss_phrases(text: str) -> list[str]:
    """文本近似命中（编辑预算内）的规范用语展示形列表（确定性、去重、保序）。"""
    s = str(text or "").strip()
    if not s or len(s) > NEAR_MISS_MAX_CHARS:
        return []
    cf = s.casefold()
    out: list[str] = []
    for match_form, display in _CANONICAL:
        L = len(match_form)
        if L < 2:
            continue
        # 至少一字相同才算「近似」：预算 min(分档预算, L-1)——2 字短语不容整词
        # 替换；≤4 字短语只容 1 字差（否则单字共享窗把全族炸成候选，证据样失配）。
        max_ed = min(near_miss_edit_budget(L), L - 1)
        hit = False
        if len(cf) >= L:
            for i in range(len(cf) - L + 1):
                d = _edit_distance(cf[i : i + L], match_form)
                if 1 <= d <= max_ed:
                    hit = True
                    break
        else:
            d = _edit_distance(cf, match_form)
            hit = 1 <= d <= max_ed
        if hit and display not in out:
            out.append(display)
    return out


# ---- n-gram 抽取 ----


_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _customer_ngrams(text: str) -> list[str]:
    """客户文本 → n-gram 候选（CJK 2-4 字全子串 + 拉丁词 casefold，≥3 字符）。"""
    out: list[str] = []
    for m in _CJK_RUN_RE.finditer(str(text or "")):
        run = m.group(0)
        n = len(run)
        for size in range(NGRAM_MIN, NGRAM_MAX + 1):
            for i in range(0, n - size + 1):
                out.append(run[i : i + size])
    for m in _LATIN_WORD_RE.finditer(str(text or "")):
        w = m.group(0).casefold()
        if len(w) >= LATIN_WORD_MIN:
            out.append(w)
    return out


# ---- 子串吞噬过滤（EX-H1 遗留①，聚合出口、每语言截断前）----


def _norm_compare(word: str) -> str:
    """子串比对归一形：NFKC（全半角归一）+ casefold（大小写归一）。仅用于比对，
    候选展示形（word 字段原值）不动。"""
    return unicodedata.normalize("NFKC", str(word or "")).strip().casefold()


def filter_substring_swallowed(candidates: list[dict]) -> tuple[list[dict], list[str]]:
    """同批候选子串吞噬过滤（纯函数）：真子串丢短留长，返回 (保留行, 被吞词)。

    **策略（简单确定，无加权）**：同语言批内，归一形 A 为另一候选 B 的真子串
    （A ⊂ B 且 A ≠ B）→ **无条件丢弃 A、保留 B**。链式包含（A⊂B⊂C）单趟即收敛
    （包含传递 ⇒ A⊂C 直接判掉，与途经 B 无关），保留的恒为极大形。

    **为什么不设「A 频次 ≥2×B 则豁免」**：gap_ngram 同源计数下
    ``freq(子串) ≥ freq(超串)`` 是结构性恒等（超串每出现一次必内嵌子串一次），
    2× 门只在跨源处可能开——而 polish_fix（按纠错 span 计 1）与 gap_ngram（按
    gram 出现计次）的 freq 基准不可比，拿它当豁免=模糊加权。故一刀切留长词。

    **为什么放在聚合出口**：三源汇合后、每语言 PER_LANG_CAP 截断前——被吞短词
    让出的槽位可被下一个非冗余候选顶上（下游预算：豆包热词表 100 token 上限取
    前 40 词、总长 ≤200 字符）。跨语言不比较（热词表按 lang 分表下发，zh 的
    「拼多多」与 cantonese 的「拼多多」互不稀释）。

    空输入=([], []) 零漂移；非 dict 行原样保留（防御，不炸）。保序返回。
    """
    rows = [c for c in (candidates or []) if isinstance(c, dict)]
    norms = [(_norm_compare(r.get("word")), str(r.get("lang") or "")) for r in rows]
    kept: list[dict] = []
    dropped: list[str] = []
    for i, row in enumerate(rows):
        wi, li = norms[i]
        swallowed = bool(wi) and any(
            j != i
            and lj == li
            and wj
            and wi in wj
            and wi != wj
            for j, (wj, lj) in enumerate(norms)
        )
        if swallowed:
            if row.get("word") not in dropped:
                dropped.append(str(row.get("word") or ""))
        else:
            kept.append(row)
    return kept, dropped


# ---- LLM 判据（单次批量；adopt|reject + 一句理由）----

_HOTWORD_SYSTEM_PROMPT = (
    "你是客服语音识别(ASR)热词库的管理员。输入是从真实通话转写里挖出的候选热词"
    "(客户常说的词 / ASR 容易听错的词 / 要求重复的用语)。对每个候选独立判断:\n"
    '1. "adopt":值得让 ASR 偏置识别正确的词——品牌名/地名/业务专名/常被听错的词/'
    "要求重复用语,真实客户高频会说。\n"
    '2. "reject":寒暄/语气词/通用词/无辨识度/与客服业务无关/含数字。\n'
    '只输出 JSON 数组,不要任何解释或代码块标记:'
    '[{"i":候选序号,"verdict":"adopt|reject","reason":"不超过12字的理由"}]'
)


def build_hotword_messages(candidates: list[dict]) -> str:
    """候选 → 单条 LLM user 消息（紧凑 JSON，序号=列表下标，与 plan 下标一致）。"""
    rows = [
        {
            "i": i,
            "word": str(c.get("word") or ""),
            "lang": str(c.get("lang") or ""),
            "kind": str(c.get("kind") or ""),
            "freq": int(c.get("freq") or 0),
        }
        for i, c in enumerate(candidates or [])
    ]
    return "候选热词:" + json.dumps(rows, ensure_ascii=False)


def parse_hotword_plan(text: str) -> dict[int, dict]:
    """宽松解析 LLM 输出 → {候选序号: {verdict, reason}}；非 JSON/缺字段静默跳过。

    与 qa_cluster.parse_llm_decisions 同纪律：剥围栏、取首 [ 到末 ]、坏 JSON 空。
    verdict 仅认 adopt/reject（exact-token，无序号容错）。
    """
    t = re.sub(r"```(?:json)?|```", "", str(text or "")).strip()
    start = t.find("[")
    end = t.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        arr = json.loads(t[start : end + 1])
    except ValueError:
        return {}
    out: dict[int, dict] = {}
    if not isinstance(arr, list):
        return out
    for d in arr:
        if not isinstance(d, dict) or isinstance(d.get("i"), bool):
            continue
        try:
            i = int(d.get("i"))
        except (TypeError, ValueError):
            continue
        verdict = str(d.get("verdict") or "").strip().lower()
        if verdict not in ("adopt", "reject"):
            continue
        out[i] = {"verdict": verdict, "reason": str(d.get("reason") or "")[:24]}
    return out


def attach_verdicts(candidates: list[dict], decisions: dict[int, dict]) -> list[dict]:
    """给每个候选挂 verdict/reason；未判定的候选默认 reject（保守，不出仓为可采纳）。"""
    out: list[dict] = []
    for i, c in enumerate(candidates or []):
        d = (decisions or {}).get(i) or {}
        row = dict(c)
        row["verdict"] = str(d.get("verdict") or "reject")
        row["reason"] = str(d.get("reason") or "")
        out.append(row)
    return out


def select_hotword_rows(plan: dict, hotword_select: list[int] | None) -> list[dict]:
    """apply 侧：按下标从 dry 计划 hotwords.candidates 取选中行（None=不采纳）。

    越界/非 int 静默忽略（与 qa_cluster._select_rows 同纪律；下标只在新鲜缓存
    计划上有意义，守卫在端点/runner）。
    """
    if hotword_select is None:
        return []
    cands = list(((plan or {}).get("hotwords") or {}).get("candidates") or [])
    out: list[dict] = []
    for item in hotword_select or []:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(cands):
            out.append(cands[idx])
    return out


# ---- 抽取主入口（纯函数）----


def extract_hotword_candidates(
    conversations: list[list[dict]],
    existing_words,
    existing_q_norms=None,
    *,
    variant_table=None,
) -> list[dict]:
    """挖掘三源热词候选（纯函数，无 I/O；LLM 判定在调用方）。

    conversations 形如 repository.iter_call_conversations 返回：每通一个轮次
    列表，轮 dict 带 role/text/lang/gen/provider/call_id（缺键宽容）。返回每语言
    按 freq 降序取前 PER_LANG_CAP 的候选行（聚合出口先过 IRON FILTER iv 子串
    吞噬，见 filter_substring_swallowed）：
    {word, lang, freq, kind, evidence:[{call_id, raw, fixed}]}。

    后两个参数是**过滤上下文**：existing_words=已入库热词（避免重复提议）、
    existing_q_norms=qa 词条归一后问法集（gap_ngram 的 qa 覆盖排除）。variant_table
    缺省走随源码分发的音近表（测试可注入手写表）。
    """
    existing = {str(w).strip().casefold() for w in (existing_words or []) if str(w).strip()}
    hotword_terms = tuple(str(w).strip() for w in (existing_words or []) if str(w).strip())
    q_norms = [str(q) for q in (existing_q_norms or []) if str(q)]
    table = _variant_table() if variant_table is None else variant_table

    agg: dict[tuple[str, str], dict] = {}
    ngram_counts: dict[tuple[str, str], int] = {}
    ngram_evi: dict[tuple[str, str], tuple[str, str]] = {}

    def _note(word: str, lang: str, kind: str, freq: int, call_id: str, raw: str, fixed: str) -> None:
        word = str(word or "").strip()
        if not word or is_digit_dominant(word) or word.casefold() in existing:
            return
        key = (word, lang)
        entry = agg.get(key)
        if entry is None:
            entry = {
                "word": word,
                "lang": lang,
                "freq": 0,
                "kind": kind,
                "evidence": [],
                "_priority": KIND_PRIORITY.get(kind, 99),
            }
            agg[key] = entry
        entry["freq"] += int(freq)
        if KIND_PRIORITY.get(kind, 99) < entry["_priority"]:
            entry["kind"] = kind
            entry["_priority"] = KIND_PRIORITY.get(kind, 99)
        if len(entry["evidence"]) < EVIDENCE_MAX:
            entry["evidence"].append({"call_id": call_id, "raw": raw, "fixed": fixed})

    for conversation in conversations or []:
        lang = _conv_lang(conversation)
        for turn in conversation or []:
            if str(turn.get("role") or "") != "user":
                continue
            raw = str(turn.get("text") or "").strip()
            if not raw:
                continue
            # (ii) 回声/碎片轮整轮跳过（数字/标点/词表命中剥空后无实质内容）。
            if looks_garbled(raw, hotword_terms):
                continue
            call_id = str(turn.get("call_id") or "")

            # (a) polish_fix：运行时同款确定性纠错，raw≠polished 取纠错 span 词。
            lane = lang
            if lane == "en":
                lane = detect_lane(raw)
            res = polish_transcript(raw, lane, table)
            if res.edits and res.text != raw:
                for _s, _e, _before, after in res.edits:
                    _note(after, lang, KIND_POLISH_FIX, 1, call_id, raw, str(after))

            # (b) near_miss：近似命中要求重复规范用语 → 候选=规范用语。
            for phrase in _near_miss_phrases(raw):
                _note(phrase, lang, KIND_NEAR_MISS, 1, call_id, raw, phrase)

            # (c) gap_ngram：先计数（覆盖排除在计数后统一做）。
            for gram in _customer_ngrams(raw):
                gkey = (gram, lang)
                ngram_counts[gkey] = ngram_counts.get(gkey, 0) + 1
                ngram_evi.setdefault(gkey, (call_id, raw))

    for (gram, lang), count in ngram_counts.items():
        if count < NGRAM_MIN_FREQ:
            continue
        if any(gram in q for q in q_norms):
            continue  # qa 词条问法已覆盖
        call_id, raw = ngram_evi.get((gram, lang), ("", ""))
        _note(gram, lang, KIND_GAP_NGRAM, count, call_id, raw, "")

    # 子串吞噬（IRON FILTER iv）：三源汇合后、每语言截断前丢短留长；观测对标
    # CP 侧 print 风格（main.py `[cp] …` flush 行），仅在真的吞了东西时出一行。
    kept_rows, swallowed = filter_substring_swallowed(list(agg.values()))
    if swallowed:
        print(
            f"[cp] hotword substring swallow: dropped={len(swallowed)} words={','.join(swallowed)}",
            flush=True,
        )

    # 每语言 top PER_LANG_CAP by freq（并列取字典序，确定性）。
    by_lang: dict[str, list[dict]] = {}
    for entry in kept_rows:
        by_lang.setdefault(entry["lang"], []).append(entry)
    out: list[dict] = []
    for _lang, rows in by_lang.items():
        rows.sort(key=lambda r: (-r["freq"], r["word"]))
        out.extend(rows[:PER_LANG_CAP])
    out.sort(key=lambda r: (r["lang"], -r["freq"], r["word"]))
    for row in out:
        row.pop("_priority", None)
    return out


def build_hotword_section(candidates: list[dict], decisions: dict[int, dict]) -> dict:
    """抽取值 + LLM 判定 → dry 计划的 hotwords 段（供 qa_cluster 装配）。"""
    rows = attach_verdicts(candidates, decisions)
    return {
        "candidates": rows,
        "counts": {
            "candidates": len(rows),
            "adopt": sum(1 for r in rows if r.get("verdict") == "adopt"),
            "reject": sum(1 for r in rows if r.get("verdict") == "reject"),
        },
    }
