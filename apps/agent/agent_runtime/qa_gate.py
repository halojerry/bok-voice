"""Q→A 检索快路:匹配器与资格闸(纯函数,PR-3;设计见 docs/superpowers/specs/2026-09-08-qa-fastpath-design.md)。

用户轮提交后、LLM 调用前:四道闸全过 + 条目命中 + 应答音频已预生成 →
跳过 LLM 直接播缓存音频(命中轮 ~50ms)。任一不满足 → 照旧走 LLM。

打分照抄知识库 InMemoryVectorStore:0.6×余弦 + 0.4×子串长度比,向量用
HybridLexicalEmbedding(CJK 逐字+bigram 哈希,零外部依赖)。阈值默认 0.90
宁缺毋滥;语义向量(MlxEmbedding)与步骤作用域条目放量为 v2。
"""

from __future__ import annotations

import os

from bok_voice_core.embeddings import HybridLexicalEmbedding
from bok_voice_core.qa_text import normalize_question

# 允许走快路的 rule_verdict:确认/含糊/提问(QUESTION 仅限 FAQ 条目命中)。
# REFUSE/OBJECTION 让位(收线/安抚需要临场生成);推进轮由调用方传 advanced 拦。
_ALLOWED_VERDICTS = {"", "confirm", "unclear", "question"}


def qa_fastpath_enabled() -> bool:
    return os.environ.get("BOK_QA_FASTPATH", "1") == "1"


def qa_threshold() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("BOK_QA_MATCH_THRESHOLD", "0.90"))))
    except ValueError:
        return 0.90


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / max(1e-9, na * nb)


def qa_exclude_reason(
    user_text: str,
    *,
    verdict: str = "",
    flow_done: bool = False,
    closing: bool = False,
    wa_signal: str = "",
    wa_captured: bool = False,
    wa_step_locked: bool = False,
    advanced: bool = False,
) -> str:
    """四道闸:返回旁路原因(空串=允许进入匹配)。

    全部复用 flow.py 既有信号——闸门绝不与 WhatsApp 捕获/拒绝收线/流程推进
    抢话,任何含数字串的轮交回 LLM+flow(数字读法零降级铁律)。数字探测用
    _digit_runs_in(汉字/英文数字词归一成 ASCII 后逐 run 归一),与 WA 侦测
    同源,粤式「三七七八九零」照拦。
    """
    from .flow import _REFUSE_RE, _digit_runs_in

    if advanced:
        return "advanced"
    if closing or flow_done:
        return "closing"
    if wa_signal:
        return "wa_signal"
    if wa_step_locked and not wa_captured:
        return "wa_step_locked"
    if _digit_runs_in(user_text):
        return "digits"
    if _REFUSE_RE.search(user_text):
        return "refuse"
    if str(verdict or "").lower() not in _ALLOWED_VERDICTS:
        return "verdict"
    return ""


class QaIndex:
    """启用条目的预建向量索引:每会话装配一次,查询只 embed 用户话语一次。"""

    def __init__(self, entries: list[dict]):
        self._embed = HybridLexicalEmbedding(512)
        self._items: list[tuple[dict, str, list[float]]] = []
        for e in entries or []:
            q = normalize_question(str(e.get("question_text") or ""))
            if not q:
                continue
            try:
                vec = self._embed.embed([q])[0]
            except Exception:  # noqa: BLE001 - 单条向量失败跳过该条
                continue
            self._items.append((e, q, vec))

    def __len__(self) -> int:
        return len(self._items)

    def match(
        self,
        user_text: str,
        *,
        lang: str = "",
        step_index: int | None = None,
        threshold: float | None = None,
    ) -> tuple[dict | None, float]:
        """返回 (命中条目, 得分);未过阈值/作用域不符/语言不符 → (None, best)。"""
        q = normalize_question(user_text)
        if not q or not self._items:
            return None, 0.0
        qv = self._embed.embed([q])[0]
        q_low = q.lower()
        thr = qa_threshold() if threshold is None else threshold
        best: dict | None = None
        best_score = 0.0
        for entry, e_q, e_vec in self._items:
            if lang and str(entry.get("lang") or "") and str(entry["lang"]) != lang:
                continue
            if str(entry.get("scope") or "global") == "step":
                if step_index is None or int(entry.get("step_index") or -1) != int(step_index):
                    continue
            score = 0.6 * _cos(qv, e_vec)
            # 子串长度比(照抄 InMemoryVectorStore:包含才计,长串含短串占比)
            if q_low in e_q.lower():
                score += 0.4 * (len(q_low) / max(1, len(e_q)))
            if score > best_score:
                best, best_score = entry, score
        if best is not None and best_score >= thr:
            return best, best_score
        return None, best_score
