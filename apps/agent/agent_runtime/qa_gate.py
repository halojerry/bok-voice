"""Q→A 检索快路:匹配器与资格闸(纯函数,PR-3;设计见 docs/superpowers/specs/2026-09-08-qa-fastpath-design.md)。

用户轮提交后、LLM 调用前:四道闸全过 + 条目命中 + 应答音频已预生成 →
跳过 LLM 直接播缓存音频(命中轮 ~50ms)。任一不满足 → 照旧走 LLM。

打分:词法档照抄知识库 InMemoryVectorStore——0.6×余弦 + 0.4×子串长度比(哈希
向量靠子串比补足「几乎逐字」的强信号,阈值 0.90 宁缺毋滥);语义档改用**纯
余弦**(0.6/0.4 混合下纯向量命中上限只有 0.6,任何 >0.6 的阈值都结构性不可达;
真 embedding 的释义相似度本身就是信号)。向量默认 HybridLexicalEmbedding
(CJK 逐字+bigram 哈希,零外部依赖);步骤作用域条目已随表结构上线(scope=step+
step_index,见 match)。

语义档(v2,2026-09-14 接线):设 `QA_EMBEDDING_MODEL`(或复用
`KB_EMBEDDING_MODEL`)=mlx embedding 模型名时,装配点用 MlxEmbedding 真加载
探针,能加载才启用;未设/加载失败自动回退词法(=历史行为),不让快答库失效。
阈值:显式 BOK_QA_MATCH_THRESHOLD 优先;否则语义档 _QA_SEMANTIC_DEFAULT_THRESHOLD
(纯余弦尺度,待真实流量标定),词法档 0.90。
"""

from __future__ import annotations

import os

from bok_voice_core.embeddings import HybridLexicalEmbedding
from bok_voice_core.qa_text import normalize_question

# 允许走快路的 rule_verdict:确认/含糊/提问(QUESTION 仅限 FAQ 条目命中)。
# REFUSE/OBJECTION 让位(收线/安抚需要临场生成);推进轮由调用方传 advanced 拦。
_ALLOWED_VERDICTS = {"", "confirm", "unclear", "question"}

# 语义档默认阈值:纯余弦尺度(见模块 docstring),释义句在真 embedding 下
# cos 通常 0.75-0.95;先取 0.72 起步,真实流量标定后再调。显式 env 覆盖优先。
_QA_SEMANTIC_DEFAULT_THRESHOLD = 0.72


def qa_fastpath_enabled() -> bool:
    return os.environ.get("BOK_QA_FASTPATH", "1") == "1"


def qa_threshold() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("BOK_QA_MATCH_THRESHOLD", "0.90"))))
    except ValueError:
        return 0.90


def qa_embedding_model() -> str:
    """语义 embedding 模型名(空=纯词法档)。QA_EMBEDDING_MODEL 优先,KB 变量复用。"""
    return (os.environ.get("QA_EMBEDDING_MODEL") or os.environ.get("KB_EMBEDDING_MODEL") or "").strip()


def build_qa_embedder() -> tuple[object, str]:
    """选 embedding 服务:(service, backend 标签)。

    只有模型名设了且真能加载(懒加载探针一次 embed)才走神经向量;否则回退
    HybridLexicalEmbedding(=历史行为)。backend 标签进装配日志/测试,现场可查
    当前哪档在跑;注入替身(测试/嵌入方)走 QaIndex(embed=...)。
    """
    model = qa_embedding_model()
    if model:
        try:
            from bok_voice_core.embeddings import MlxEmbedding

            emb = MlxEmbedding(model)
            emb.embed(["启动探针"])  # 懒加载探针:模型未就位/依赖缺失当场失败
            return emb, f"mlx:{model}"
        except Exception as exc:  # noqa: BLE001 - 加载失败回退词法,不让快答库失效
            print(f"QA_EMBEDDING fallback lexical err={exc!r}", flush=True)
    return HybridLexicalEmbedding(512), "lexical"


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
    """启用条目的预建向量索引:每会话装配一次,查询只 embed 用户话语一次。

    embed/backend 可注入(测试与嵌入方);缺省走 build_qa_embedder()(词法/
    语义按 env 选入)。阈值:显式 BOK_QA_MATCH_THRESHOLD 优先;否则语义档用
    _QA_SEMANTIC_DEFAULT_THRESHOLD,词法档维持 0.90。
    """

    def __init__(self, entries: list[dict], embed=None, backend: str = ""):
        if embed is None:
            self._embed, self.backend = build_qa_embedder()
        else:
            self._embed, self.backend = embed, (backend or "injected")
        # 语义档判定与阈值:mlx 档用纯余弦 + 语义阈值;词法/未知注入档维持
        # 0.6/0.4 混合 + 0.90。显式 BOK_QA_MATCH_THRESHOLD 恒优先。
        self._semantic = self.backend.startswith("mlx:")
        if os.environ.get("BOK_QA_MATCH_THRESHOLD", "").strip():
            self._threshold = qa_threshold()
        elif self._semantic:
            self._threshold = _QA_SEMANTIC_DEFAULT_THRESHOLD
        else:
            self._threshold = qa_threshold()
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
        thr = self._threshold if threshold is None else threshold
        best: dict | None = None
        best_score = 0.0
        for entry, e_q, e_vec in self._items:
            if lang and str(entry.get("lang") or "") and str(entry["lang"]) != lang:
                continue
            if str(entry.get("scope") or "global") == "step":
                if step_index is None or int(entry.get("step_index") or -1) != int(step_index):
                    continue
            if self._semantic:
                # 语义档:纯余弦(0.6/0.4 混合下纯向量命中上限 0.6,阈值不可达)
                score = _cos(qv, e_vec)
            else:
                score = 0.6 * _cos(qv, e_vec)
                # 子串长度比(照抄 InMemoryVectorStore:包含才计,长串含短串占比)
                if q_low in e_q.lower():
                    score += 0.4 * (len(q_low) / max(1, len(e_q)))
            if score > best_score:
                best, best_score = entry, score
        if best is not None and best_score >= thr:
            return best, best_score
        return None, best_score
