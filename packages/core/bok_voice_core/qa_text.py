"""Q→A 快路共享文本归一(2026-09-09)。

挖掘报告(CP /api/reports/qa-pairs)与运行时匹配(agent qa_gate)必须用同一个
归一,否则报告聚出来的高频问句到运行时对不上。只做全半角标点/空白宽度归一;
**不做数字归一**——含数字串的用户轮在闸门早被旁路(WhatsApp/单号轮绝不走快路),
归一不需要也不应该处理数字。
"""

from __future__ import annotations

import re

_PUNCT_TRANS = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "！": "!",
        "？": "?",
        "；": ";",
        "：": ":",
        "（": "(",
        "）": ")",
        "、": ",",
        "～": "~",
        "—": "-",
        "–": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "　": " ",
    }
)


def normalize_question(text: str) -> str:
    """归一:统一全半角后**剥掉全部标点/符号/空白**,只留文字与数字。

    挖掘聚类与运行时匹配同源。用户话尾常带「?/啊/」等变化,标点必须剥掉
    (否则「幾時送到?」与词条「幾時送到」哈希向量不同、子串也断),语尾词
    的差异交给 0.6 余弦项吸收,阈值 0.90 兜底。
    """
    t = str(text or "").translate(_PUNCT_TRANS)
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", t)


def mine_qa_pairs(
    conversations: list[list[dict]],
    *,
    min_calls: int = 5,
    limit: int = 100,
) -> list[dict]:
    """高频问答对挖掘:用户轮 → 紧随 assistant 轮配对,归一化聚类。

    计数按「出现的通话数」而非轮数(同通复读只算一通);答案取众数。
    conversations 形如 repository.iter_call_conversations 的返回:
    [[{"role","text","lang"}, ...], ...]
    """
    from collections import Counter

    stats: dict[tuple[str, str], dict] = {}
    for turns in conversations or []:
        seen_in_call: set[str] = set()
        for i in range(len(turns) - 1):
            a, b = turns[i], turns[i + 1]
            if str(a.get("role") or "") != "user" or str(b.get("role") or "") != "assistant":
                continue
            q = normalize_question(str(a.get("text") or ""))
            ans = str(b.get("text") or "").strip()
            lang = str(a.get("lang") or b.get("lang") or "zh") or "zh"
            if not q or not ans or q in seen_in_call:
                continue
            seen_in_call.add(q)
            st = stats.setdefault(
                (lang, q),
                {"lang": lang, "question": q, "calls": 0, "answers": Counter()},
            )
            st["calls"] += 1
            st["answers"][ans[:120]] += 1
    out: list[dict] = []
    for st in stats.values():
        if st["calls"] < min_calls:
            continue
        top_ans, votes = st["answers"].most_common(1)[0]
        out.append(
            {
                "lang": st["lang"],
                "question": st["question"],
                "calls": st["calls"],
                "answer": top_ans,
                "answer_votes": votes,
            }
        )
    out.sort(key=lambda r: (-r["calls"], r["question"]))
    return out[:limit]


# ---- 自动入库闸(2026-09-11 自动学习闭环:mine_qa --sync) ----
# 保守优先:错答案一旦罐头化就是复读机。闸必须严;闸外条目打印给人看,
# 人可用 DELETE /api/qa-entries/{id} 否决。
AUTO_MIN_CALLS = 5
AUTO_MIN_VOTE_RATIO = 0.8
AUTO_MIN_Q_LEN = 4
AUTO_MAX_Q_LEN = 24

_DIGIT_RUN_RE = re.compile(r"\d{4,}")

# 闸门用中文数字→阿拉伯映射(仅 digits 检查用,不进 normalize_question——问题
# 键与运行时匹配必须同源同形)。运行时旁路(flow._digit_runs_in)归一中文数字,
# 「三七七八九零」类问句运行时永远走不到快路,入库即死重,闸要同口径拦。
_CJK_DIGIT_TRANS = str.maketrans(
    {"零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
     "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
)


def auto_apply_verdict(pair: dict, existing_norm_questions: set[str] | None = None) -> tuple[bool, str]:
    """自动入库闸(2026-09-11 自动学习闭环)。保守:错答案罐头化=复读机。

    闸(依次判,首个不过即拒):
    - calls≥AUTO_MIN_CALLS(5);
    - 众数答案得票占比 votes/calls≥AUTO_MIN_VOTE_RATIO(0.8,答案稳定性;
      挖掘已按通话去重,总票数=通话数);
    - 归一问法长度 [AUTO_MIN_Q_LEN, AUTO_MAX_Q_LEN](4-24,滤单字应承与
      超长叙述);
    - 无 ≥4 位连续数字(ASCII 或中文数字,数字轮运行时被四道闸旁路,库内
      是死重);
    - 归一后不与现有词条重复(existing_norm_questions=None 跳过该项)。
    返回 (入库?, 原因码);过闸原因码为空串。
    """
    q = str(pair.get("question") or "")
    calls = int(pair.get("calls") or 0)
    votes = int(pair.get("answer_votes") or 0)
    if calls < AUTO_MIN_CALLS:
        return False, "low_calls"
    if votes / calls < AUTO_MIN_VOTE_RATIO:
        return False, "unstable_answer"
    if not AUTO_MIN_Q_LEN <= len(q) <= AUTO_MAX_Q_LEN:
        return False, "question_length"
    if _DIGIT_RUN_RE.search(q) or _DIGIT_RUN_RE.search(q.translate(_CJK_DIGIT_TRANS)):
        return False, "digits"
    if existing_norm_questions is not None and q in existing_norm_questions:
        return False, "duplicate"
    return True, ""
