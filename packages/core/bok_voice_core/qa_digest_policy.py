"""闲时消化引擎·策略纯函数层(2026-09-25)。

QA 快答库(qa_entries)命中率的瓶颈在词条供给靠人工。「闲时自动消化引擎」
从真实通话挖高频问法 → LLM 同义聚类(qa_cluster 产 variant/fresh/junk 结论)
→ 按风险分档自动采纳。本模块是其中**零 IO 零 LLM 零 DB 的策略裁决层**:
CP 消化引擎(control_plane 侧,另行落地)喂入候选、拿走分档计划——

- ``auto_variant`` / ``auto_fresh``: 可直接自动入库的候选;
- ``pending``:                敏感域候选,必须人工放行后才可入库;
- ``skipped``:                保守丢弃(附原因码)。

三条纪律:
- **保守优先(宁误禁勿误播)**:错答案一旦罐头化就是复读机;金额/倍数/
  长数字/百分比等「数字出事面」一律不自动采纳(赔偿档位被模型重排、
  「300 蚊」在无关轮乱入均有实弹前科,敏感域闸是最后一道堤);
- **与运行时同源**:文本归一复用 ``qa_text.normalize_question``,保证
  挖掘面与运行时匹配面对同一字符串;
- **同音对只管纯同音替换**:一期 ``mine_homophones`` 问句对整句编辑距离=1
  且差异字符同音才出对;二期 ``mine_homophones_semantic`` 在语义同族
  (embed 余弦,CP 侧算好传入)的前提下,按**逐字拼音序列**做块对齐——
  发音对齐而字形不同的位就是同音替换本体(「裴/赔」族改写+同音叠加),
  改写段/增删段/异音段一律不出对。「改写+同音叠加」单靠词面距离
  判不出对,必须有语义锚,挖掘层绝不越界出对子。

分工:LLM 聚类结论由 ``qa_cluster`` 产;本模块只做裁决与同音对挖掘,
全部输入输出是 plain dict,单测全离线。
"""

from __future__ import annotations

import re

from pypinyin import lazy_pinyin

from .qa_text import normalize_question

# ---- 敏感域判定(自动采纳禁区) ----

# 货币关键词:任一子串命中即敏感。保守口径(宁误禁勿误播):「单元/多元」
# 类误伤落 pending 人工面兜住,也不放数字出事面进自动链。
_MONEY_KEYWORDS: tuple[str, ...] = ("蚊", "元", "港币", "¥", "￥", "$", "＄")

# 阿拉伯数字+货币单位紧邻(允许中间空白):「300 蚊」「赔偿300元」类。
_MONEY_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?\s*(?:蚊|元|块)")

# 英文货币词(「refund 500 dollars」一类),词边界防误伤普通英文词。
_EN_MONEY_RE = re.compile(r"(?i)\b(?:dollars?|usd|hkd|cny|rmb)\b")

# 倍数档位:「两倍/三倍」类中文数字+倍、阿拉伯数字+倍。
_MULTIPLIER_RE = re.compile(r"(?:[0-9]+|[零一二两三四五六七八九十百千]+)\s*倍")

# 赔付比例档(2026-09-25 首轮实弹补):「一赔二」「赔三」类——不带「倍/蚊/元」
# 的比例表述,实测答案「这单我全责,一赔二给您」从敏感闸漏过;「全责/免贴」
# 同属承诺性表述一并入禁区。
_CN_NUM = "[0-9零一二两三四五六七八九十百千]"
_COMP_RATIO_RE = re.compile(rf"(?:{_CN_NUM}+\s*赔\s*{_CN_NUM}+)|全责|免[贴貼]")

# 长数字串:≥4 位连续数字(单号/电话类;与 qa_text 入库闸的 digits 同口径)。
_DIGIT_RUN_RE = re.compile(r"[0-9]{4,}")

# 百分比:「30%」「50％」「百分之三十」。
_PERCENT_RE = re.compile(r"%|％|百分之")


def is_sensitive_answer(text: str) -> bool:
    """答案/问句文本是否落敏感域(自动采纳禁区)。

    判据(保守,宁误禁勿误播;命中任一即 True):
    - 货币/金额:关键词 蚊/元/港币 与 ¥/$ 符号(含全角),或「数字+(小数)?
      (蚊|元|块)」紧邻金额,或英文货币词(dollars/USD/HKD/CNY/RMB);
    - 倍数档位:「N倍」(阿拉伯或中文数字,「最高赔两倍」类);
    - 长数字串:≥4 位连续数字(单号/电话);
    - 百分比:「30%」「50％」「百分之三十」。

    中文数字金额(「三百蚊」)由关键词「蚊/元」命中兜住——关键词判据故意
    宽于金额模式,敏感域宁可多禁。空文本恒 False。
    """
    t = str(text or "")
    if not t:
        return False
    if any(k in t for k in _MONEY_KEYWORDS):
        return True
    return bool(
        _MONEY_AMOUNT_RE.search(t)
        or _EN_MONEY_RE.search(t)
        or _MULTIPLIER_RE.search(t)
        or _COMP_RATIO_RE.search(t)
        or _DIGIT_RUN_RE.search(t)
        or _PERCENT_RE.search(t)
    )


# ---- 问句性门（2026-09-25 首轮实弹补：策略层纵深防御） ------------------------
# 真栈首轮实证:CP runner 的 fresh 路径零长度/问句门,「嗯」「多多」「啊拼多多」
# 「你好是我」类应承/碎片/身份句被 plan 判 fresh 直通采纳——罐头化一句应承
# 当「答案」是纯垃圾。聚类侧的长度门(qa_cluster plan 只 gate variant 路径,
# AUTO_MIN_Q_LEN=4)管不住 fresh;**策略层必须是最后一道门**(引擎 headless
# 路径可能变化,这层不动)。
# 门 = 长度(与 AUTO_MIN_Q_LEN 同源 4) + 问句性(疑问词族或问号结尾,二者其一)。
_INTERROGATIVE_MARKERS = (
    # zh 书面/口语
    "怎", "如何", "为什么", "为何", "几", "多少", "什么", "什麽",
    "哪", "吗", "嘛", "麽", "能否", "可否", "可不可以", "可不可",
    # cantonese 口语
    "點樣", "点样", "邊度", "边度", "邊", "使唔使", "唔使", "咩", "幾時", "几时",
    # en
    "what", "why", "how", "when", "where", "which", "who", "can i", "could",
    "would", "is it", "are you", "do you", "will you",
)


def looks_like_question(question: str) -> bool:
    """问句性判定:长度门(AUTO_MIN_Q_LEN 同源)+疑问词族/问号。

    「怎么联系我」「上門檢測使唔使錢」→ True;「嗯」「多多」「你好是我」
    「好的淘宝京东单号」→ False。疑问词表宁缺勿滥(「唔好意思」类陈述句
    不带这些标记——bare「唔」故意不进表防误放行)。
    """
    from .qa_text import AUTO_MIN_Q_LEN

    q = str(question or "").strip()
    if len(q) < AUTO_MIN_Q_LEN:
        return False
    if "?" in q or "？" in q:
        return True
    # 挖掘管线 normalize_question 剥全部空白——「can i get a refund」到这层是
    # 「canigetarefund」,英文标记必须双侧去空格比对(2026-09-25 引擎测试抓出)。
    low = q.lower()
    flat = low.replace(" ", "")
    for m in _INTERROGATIVE_MARKERS:
        if m in low or m.replace(" ", "") in flat:
            return True
    return False


# ---- 分档裁决 ----

# variant 档(继承既有词条答案)自动采纳门槛:真实复现 ≥2 次——单次出现
# 分不清是真实问法还是 ASR 噪声,继承答案虽稳,问法键也须二次佐证。
MIN_VARIANT_RECURRENCE = 2
# fresh 档(全新问答)自动采纳门槛:复现 ≥3 次——全新答案自动入库风险
# 最高,门槛比 variant 更高,同样为滤 ASR 噪声单次出现。
MIN_FRESH_RECURRENCE = 3


def classify_candidates(candidates: list[dict]) -> dict:
    """分档裁决:LLM 聚类结论 + 复现计数 → 自动采纳/待审/丢弃三列。

    输入候选形状::

        {"question": str(原话), "lang": "zh"|"cantonese"|"en",
         "count": int(真实复现次数), "cluster_verdict": "variant"|"fresh"|"junk",
         "target_entry_id": str|None(variant 的归属词条),
         "sample_answer": str(AI 当时实答原句), "sample_lang": str}

    输出::

        {"auto_variant": [candidate...], "auto_fresh": [candidate...],
         "pending": [candidate...], "skipped": [(candidate, reason), ...]}

    规则(依次判,首条命中即定档):
    - junk / count<1 → skipped(junk / low_count);
    - variant:target_entry_id 在场且 count≥MIN_VARIANT_RECURRENCE →
      auto_variant(答案继承既有词条,敏感闸不重判);缺 target →
      skipped(variant_no_target),复现不足 → skipped(variant_low_count);
    - fresh:count≥MIN_FRESH_RECURRENCE 且 sample_answer 非空且
      sample_lang==lang(语言不一致——AI 实答语言≠问句语言,真实踩过
      中文词条被建议另一语言答案——直接弃),再过敏感闸:非敏感 →
      auto_fresh,敏感 → pending(人工放行);复现不足/空答案 →
      skipped(fresh_low_count / empty_answer);
    - 未知 verdict → skipped(unknown_verdict,保守丢弃)。

    纯函数:不改入参,输出列表持有调用方的原 dict 引用。
    """
    out: dict = {"auto_variant": [], "auto_fresh": [], "pending": [], "skipped": []}
    for raw in candidates or []:
        cand = raw if isinstance(raw, dict) else {}
        verdict = str(cand.get("cluster_verdict") or "").strip().lower()
        try:
            count = int(cand.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if verdict == "junk":
            out["skipped"].append((cand, "junk"))
            continue
        if count < 1:
            out["skipped"].append((cand, "low_count"))
            continue
        # 问句性门(2026-09-25 首轮实弹补,variant/fresh 两档同守):应承/碎片/
        # 身份句不是问题,罐头化它们的「答案」是纯垃圾——「嗯」「多多」「你好
        # 是我」「啊拼多多」类在真栈首轮全部漏过,这层是最后一道门。
        if not looks_like_question(str(cand.get("question") or "")):
            out["skipped"].append((cand, "not_question"))
            continue
        if verdict == "variant":
            target = str(cand.get("target_entry_id") or "").strip()
            if not target:
                out["skipped"].append((cand, "variant_no_target"))
                continue
            if count < MIN_VARIANT_RECURRENCE:
                out["skipped"].append((cand, "variant_low_count"))
                continue
            out["auto_variant"].append(cand)
            continue
        if verdict == "fresh":
            if count < MIN_FRESH_RECURRENCE:
                out["skipped"].append((cand, "fresh_low_count"))
                continue
            answer = str(cand.get("sample_answer") or "").strip()
            if not answer:
                out["skipped"].append((cand, "empty_answer"))
                continue
            lang = str(cand.get("lang") or "")
            sample_lang = str(cand.get("sample_lang") or "")
            if sample_lang != lang:
                out["skipped"].append((cand, "lang_mismatch"))
                continue
            if is_sensitive_answer(answer):
                out["pending"].append(cand)
                continue
            out["auto_fresh"].append(cand)
            continue
        out["skipped"].append((cand, "unknown_verdict"))
    return out


# ---- 同音对挖掘 ----

# 同音对挖掘只看复现 ≥2 的未命中问法:单次出现无法与 ASR 噪声区分,
# 「裴/赔」类替换对必须被真实用户重复说过才值得铸进查询侧归一。
MIN_HOMOPHONE_MISS_COUNT = 2

# 二期(语义锚定)replace 段单侧长度上限:超过即「改写」不是「同音替换」,
# 逐位对齐在长段上是噪声放大器,保守丢弃(任务契约:两侧各 ≤2 字)。
_SEMANTIC_REPLACE_MAX = 2


def _edit_distance(a: str, b: str) -> int:
    """自写 O(nm) DP 编辑距离(Levenshtein;勿引第三方编辑距离库)。"""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return max(la, lb)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def _single_sub_pair(a: str, b: str) -> tuple[str, str] | None:
    """等长字符串恰有一处字符差异 → 返回 (a 侧字, b 侧字);否则 None。

    编辑距离=1 但两侧长度不等是插入/删除,不构成单字符对(两侧都是单字
    才算),由调用方先按长度短路。
    """
    if len(a) != len(b):
        return None
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    if len(diffs) != 1:
        return None
    return diffs[0]


def _pinyin_of(ch: str) -> tuple[str, ...]:
    """单字符无声调拼音(pypinyin lazy_pinyin 默认 NORMAL 档,无调号)。"""
    return tuple(lazy_pinyin(ch))


def mine_homophones(misses: list[dict], entries: list[dict]) -> list[dict]:
    """同音对挖掘:未命中问法 × 词条问法 →「用户说错字」候选替换对。

    参数::

        misses  = [{"question": str(未命中任何词条的问法原话), "count": int}]
        entries = [{"id": str, "question_text": str}]

    只看 count≥MIN_HOMOPHONE_MISS_COUNT 的 miss(单次出现=噪声嫌疑)。
    对每个 miss 与每条词条问法:``normalize_question`` 归一后自写 DP 算
    编辑距离——距离=0(miss 与词条完全同形)不算;距离=1 且差异位两侧
    都是单字符、这对字符 lazy_pinyin 无声调拼音相同 → 同音对候选;
    距离>1(改写+同音叠加)**不出对子**,那归语义召回管。

    输出按 support 降序::

        [{"wrong": "裴", "right": "赔",
          "support": int(支持该对的去重 miss 问法数),
          "example": str(支持 miss 的原话,取首个)}]

    同一 (wrong, right) 对跨 miss 去重聚合(同问法多词条复现只计一次);
    example 取首个支持该对的 miss 原话(未归一原文)。确定性排序:
    (-support, wrong, right)。
    """
    entry_norms: list[str] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        n = normalize_question(str(e.get("question_text") or ""))
        if n:
            entry_norms.append(n)
    support: dict[tuple[str, str], dict] = {}
    for m in misses or []:
        if not isinstance(m, dict):
            continue
        try:
            cnt = int(m.get("count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt < MIN_HOMOPHONE_MISS_COUNT:
            continue
        raw_q = str(m.get("question") or "")
        norm = normalize_question(raw_q)
        if not norm:
            continue
        for en in entry_norms:
            if en == norm:  # 距离 0:完全同形,不算
                continue
            if _edit_distance(norm, en) != 1:
                continue  # 距离>1=改写叠加,不出对子
            pair = _single_sub_pair(norm, en)
            if pair is None:
                continue
            wrong, right = pair
            if _pinyin_of(wrong) != _pinyin_of(right):
                continue  # 差异字符异音:不是同音替换
            rec = support.setdefault(
                (wrong, right), {"missers": set(), "example": raw_q}
            )
            if norm not in rec["missers"]:
                rec["missers"].add(norm)
    out = [
        {"wrong": w, "right": r, "support": len(rec["missers"]), "example": rec["example"]}
        for (w, r), rec in support.items()
    ]
    out.sort(key=lambda d: (-d["support"], d["wrong"], d["right"]))
    return out


def _syl_seq(norm: str) -> list[str]:
    """归一文本 → 逐字无声调拼音序列(每字恰一音;非汉字原样透传)。

    多音字取 lazy_pinyin 默认首选(与一期 ``_pinyin_of`` 同判据);序列长度
    恒等于字符数,块对齐的位移可直接映射回字符。
    """
    out: list[str] = []
    for ch in norm:
        py = lazy_pinyin(ch)
        out.append("".join(py) if py else ch)
    return out


def _harvest_semantic_pairs(
    norm_m: str, norm_e: str, raw_q: str, support: dict[tuple[str, str], dict]
) -> None:
    """单对 (miss 归一形, 词条归一形) → 把挖到的同音字符对累进 support。

    对齐在**逐字拼音序列**上做(difflib.SequenceMatcher opcodes),同音字天然
    落进 equal 块——equal 块内「拼音相同而字形不同」的位就是同音替换本体
    (「裴/赔」),这是二期比一期(整句距离=1)多治的「改写+同音叠加」;
    replace 块按任务契约收紧(两侧各 ≤2 字、等长、逐位拼音相等才逐位取对,
    覆盖 SequenceMatcher 对齐歧义把同音位留在 replace 块里的边缘形态);
    delete/insert(增删)与长度不等的 replace(改写)一律跳过。
    """
    from difflib import SequenceMatcher

    sm = SequenceMatcher(None, _syl_seq(norm_m), _syl_seq(norm_e))
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if i2 - i1 != j2 - j1:
            continue  # 等长才逐位对:增删段/改写段(长段)直接弃
        if tag == "equal":
            # 发音对齐块:同字位跳过,异字形位=同音替换候选
            for k in range(i2 - i1):
                wrong, right = norm_m[i1 + k], norm_e[j1 + k]
                if wrong == right:
                    continue
                rec = support.setdefault(
                    (wrong, right), {"missers": set(), "example": raw_q}
                )
                rec["missers"].add(norm_m)
        elif tag == "replace":
            a_seg, b_seg = norm_m[i1:i2], norm_e[j1:j2]
            if not a_seg or len(a_seg) > _SEMANTIC_REPLACE_MAX:
                continue  # 长段是改写不是同音(保守)
            # 对齐歧义兜底:同音位被留在 replace 块时,逐位拼音相等才逐位取对
            # (错位同音/异音整段弃)。
            if any(
                _pinyin_of(x) != _pinyin_of(y) for x, y in zip(a_seg, b_seg)
            ):
                continue
            for x, y in zip(a_seg, b_seg):
                if x == y:
                    continue
                rec = support.setdefault(
                    (x, y), {"missers": set(), "example": raw_q}
                )
                rec["missers"].add(norm_m)


def mine_homophones_semantic(
    misses: list[dict],
    entries: list[dict],
    sims: dict[tuple[str, str], float],
    *,
    sim_floor: float = 0.75,
) -> list[dict]:
    """语义锚定同音挖掘(二期,VectorQ 配套):治「改写+同音叠加」。

    一期 ``mine_homophones`` 只认整句编辑距离=1 的纯同音替换,「我想先问下
    裴几多」vs「可以点样赔」这类改写叠加(距离>1)治不动。本函数在 **语义
    同族**(embed 余弦 ≥sim_floor,由 CP 侧算好传入——本层零 IO 零模型)
    的前提下,把 miss 与词条问法各归一(``normalize_question``)后按逐字
    拼音序列做块对齐:发音对齐而字形不同的位即同音替换,产出字符对。

    参数::

        misses    = [{"question": str(未命中问法原话), "count": int}]
        entries   = [{"id": str, "question_text": str}](enabled 词条)
        sims      = {(miss 原话, entry_id): float 余弦}——键用 misses 里的
                    **原话**(未归一),由调用方(CP 引擎)按同一批 misses 计算
        sim_floor = 语义同族门槛(默认 0.75);缺席/低于门槛的对不看

    规则(与一期同守):count<MIN_HOMOPHONE_MISS_COUNT 的 miss 不看;
    对齐细节见 ``_harvest_semantic_pairs``(equal 块取异字形位,replace 块
    两侧各 ≤2 字且逐位同音才取,增删/改写段跳过)。

    输出与 ``mine_homophones`` 同形,按 (-support, wrong, right) 排序::

        [{"wrong": "裴", "right": "赔",
          "support": int(支持该对的去重 miss 归一形数),
          "example": str(首个支持 miss 的原话)}]
    """
    norms_by_id: dict[str, str] = {}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        n = normalize_question(str(e.get("question_text") or ""))
        if eid and n:
            norms_by_id[eid] = n
    support: dict[tuple[str, str], dict] = {}
    for m in misses or []:
        if not isinstance(m, dict):
            continue
        try:
            cnt = int(m.get("count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt < MIN_HOMOPHONE_MISS_COUNT:
            continue
        raw_q = str(m.get("question") or "")
        norm_m = normalize_question(raw_q)
        if not norm_m:
            continue
        for eid, norm_e in norms_by_id.items():
            sim = (sims or {}).get((raw_q, eid))
            if sim is None:
                continue
            try:
                if float(sim) < sim_floor:
                    continue
            except (TypeError, ValueError):
                continue
            if norm_e == norm_m:
                continue  # 完全同形:无替换可言
            _harvest_semantic_pairs(norm_m, norm_e, raw_q, support)
    out = [
        {"wrong": w, "right": r, "support": len(rec["missers"]), "example": rec["example"]}
        for (w, r), rec in support.items()
    ]
    out.sort(key=lambda d: (-d["support"], d["wrong"], d["right"]))
    return out


def apply_homophones(query: str, pairs: list) -> str:
    """查询侧同音归一:把 query 中出现的 wrong 单字替换为 right。

    pairs 形状兼容两种::

        [{"wrong": "裴", "right": "赔", "support": 3}, ...]   # mine 输出
        [("裴", "赔"), ...]                                   # 裸元组

    规则:单字符逐个替换(wrong/right 都必须是单字);冲突对(同 wrong
    多 right)取 support 最高者,同 support 取先出现者;元组形态无
    support,恒让位于带计数的 dict 对。单遍替换、不串联(原文同字只按
    映射表换一次,替换结果不参与再替换);纯函数无状态,无匹配原样返回。
    """
    best: dict[str, tuple[int, str]] = {}
    for p in pairs or []:
        if isinstance(p, dict):
            wrong = str(p.get("wrong") or "")
            right = str(p.get("right") or "")
            try:
                sup = int(p.get("support") or 0)
            except (TypeError, ValueError):
                sup = 0
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            wrong, right = str(p[0] or ""), str(p[1] or "")
            sup = -1  # 无计数形态,恒低于任何 dict 对
        else:
            continue
        if len(wrong) != 1 or len(right) != 1 or wrong == right:
            continue
        cur = best.get(wrong)
        if cur is None or sup > cur[0]:
            best[wrong] = (sup, right)
    q = str(query or "")
    if not best:
        return q
    return "".join(best[ch][1] if ch in best else ch for ch in q)
