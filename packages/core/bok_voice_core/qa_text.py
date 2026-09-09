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
