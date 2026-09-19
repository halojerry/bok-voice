"""Q→A 同义聚类纯函数(2026-09-19 W3-T1 从 scripts/mine_qa.py --cluster 上移)。

零网络零 IO:LLM 决策解析、三列计划、按语言分批的消息组装。CLI(tts-mine
--cluster)与 CP 端点(POST /api/qa/cluster)共用同一份,保证「LLM 提议、
字数门处决」「候选原话即匹配面」「答案继承目标词条防漂移」的口径全仓唯一。

与 mine_qa.py 的分工:这里只有可单测的纯函数;LLM 调用/模型发现/单飞/缓存
在各自调用方(scripts/mine_qa.py、apps/control-plane/control_plane/qa_cluster.py)。
"""

from __future__ import annotations

import json
import re

from .qa_text import AUTO_MAX_Q_LEN, AUTO_MIN_Q_LEN, normalize_question

_CLUSTER_SYSTEM_PROMPT = (
    "你是客服快答词库的管理员。输入是现有词条列表和新挖掘的问答候选。"
    "对每个候选独立判断:\n"
    '1. "variant":候选问题与某条现有词条意图相同,且候选答案与该词条答案语义一致'
    " → 给出该词条 id 作 target;question 字段必须原样抄候选的问题(不要改写,"
    "真实用户措辞就是匹配面)。\n"
    '2. "new":全新问答,现有库里没有同义词条 → target 留空字符串。\n'
    '3. "junk":寒暄/语气词/与业务无关/答案与相关词条语义冲突。\n'
    '只输出 JSON 数组,不要任何解释或代码块标记:'
    '[{"i":候选序号,"decision":"variant|new|junk","target":"词条id或空串","note":"不超过8字的理由"}]'
)


def parse_llm_decisions(text: str) -> dict[int, dict]:
    """宽松解析 LLM 输出 → {候选序号: decision dict};非 JSON/缺字段静默跳过。"""
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
        decision = str(d.get("decision") or "").strip().lower()
        if decision not in ("variant", "new", "junk"):
            continue
        out[i] = {
            "decision": decision,
            "target": str(d.get("target") or "").strip(),
            "note": str(d.get("note") or "")[:16],
        }
    return out


def plan_cluster(
    pairs: list[dict],
    existing_rows: list[dict],
    decisions: dict[int, dict],
) -> tuple[list[dict], list[dict], list[tuple[dict, str]]]:
    """决策 → 三列(可测核心):variants 入库 payload / new 交回质量闸 / junk。

    variant 入库 payload:question=候选原话(真实措辞即匹配面),answer=继承目标
    词条 answer_text(答案以既有为准,防答案漂移),cluster_head_id=目标词条 id
    (W3-T1 补,接 qa-canvas 同义簇;CLI 旧版缺此键已由本模块统一补齐)。
    保守门:目标词条必须存在且同语言;归一后与现有词条重复 → 丢。
    """
    by_id = {str(e.get("id") or ""): e for e in existing_rows or []}
    existing_norms = {
        normalize_question(str(e.get("question_text") or ""))
        for e in existing_rows or []
        if str(e.get("question_text") or "").strip()
    }
    variants: list[dict] = []
    fresh: list[dict] = []
    junk: list[tuple[dict, str]] = []
    for i, r in enumerate(pairs or []):
        d = decisions.get(i)
        if d is None:
            junk.append((r, "no-decision"))
            continue
        if d["decision"] == "variant":
            target = by_id.get(d["target"])
            if target is None:
                junk.append((r, "target-missing"))
                continue
            if str(target.get("lang") or "") != str(r.get("lang") or ""):
                junk.append((r, "lang-mismatch"))
                continue
            q = str(r.get("question") or "").strip()
            if not q:
                junk.append((r, "empty-question"))
                continue
            # 问法长度门(与 --sync 闸同源 AUTO_MIN/MAX_Q_LEN):4B judge 对
            # 「啊」「多多」类碎片会误判 variant——LLM 提议,字数门处决。
            if not (AUTO_MIN_Q_LEN <= len(q) <= AUTO_MAX_Q_LEN):
                junk.append((r, "q-len-out-of-range"))
                continue
            if normalize_question(q) in existing_norms:
                junk.append((r, "dup-existing"))
                continue
            variants.append(
                {
                    "question_text": q,
                    "answer_text": str(target.get("answer_text") or ""),
                    "lang": r["lang"],
                    "scope": "global",
                    "source": "mined",
                    "enabled": True,
                    "account_id": str(target.get("account_id") or "acc-001"),
                    "cluster_head_id": d["target"],
                }
            )
            existing_norms.add(normalize_question(q))
        elif d["decision"] == "new":
            fresh.append(r)
        else:
            junk.append((r, d["note"] or "junk"))
    return variants, fresh, junk


def build_cluster_messages(pairs: list[dict], existing_rows: list[dict]) -> list[dict]:
    """按语言分批组装 LLM 请求(照 CLI _cluster 口径,2026-09-19 上移)。

    返回 [{"lang", "message", "rows"}, ...]:
    - 每语言一批,候选序号=该语言局部索引(i 与 rows 下标一一对应,调用方拿
      rows+同批 decisions 跑 plan_cluster);
    - 现有词条每语言截 60 条、其 answer 截 160 字、候选 answer 截 120 字;
    - 该语言还没有词条时 message="" 且 rows=整表——调用方按 CLI 同款语义把
      这些候选整批交回 fresh(质量闸),不出 LLM 请求。
    """
    out: list[dict] = []
    langs = sorted({str(r.get("lang") or "") for r in pairs or []} - {""})
    for lang in langs:
        lang_rows = [r for r in pairs if str(r.get("lang")) == lang]
        entries = [
            {
                "id": str(e.get("id") or ""),
                "question": str(e.get("question_text") or ""),
                "answer": str(e.get("answer_text") or "")[:160],
            }
            for e in existing_rows or []
            if str(e.get("lang")) == lang and str(e.get("question_text") or "").strip()
        ][:60]
        if not entries:
            out.append({"lang": lang, "message": "", "rows": lang_rows})
            continue
        cands = [
            {"i": i, "question": str(r.get("question") or ""), "answer": str(r.get("answer") or "")[:120]}
            for i, r in enumerate(lang_rows)
        ]
        message = (
            f"现有词条(lang={lang}):{json.dumps(entries, ensure_ascii=False)}\n"
            f"候选:{json.dumps(cands, ensure_ascii=False)}"
        )
        out.append({"lang": lang, "message": message, "rows": lang_rows})
    return out
