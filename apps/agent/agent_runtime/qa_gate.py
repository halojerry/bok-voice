"""Q→A 检索快路:匹配器与资格闸(纯函数,PR-3;设计见 docs/superpowers/specs/2026-09-08-qa-fastpath-design.md)。

用户轮提交后、LLM 调用前:四道闸全过 + 条目命中 + 应答音频已预生成 →
跳过 LLM 直接播缓存音频(命中轮 ~50ms)。任一不满足 → 照旧走 LLM。

打分照抄知识库 InMemoryVectorStore:0.6×余弦 + 0.4×子串长度比,向量用
HybridLexicalEmbedding(CJK 逐字+bigram 哈希,零外部依赖)。阈值默认 0.90
宁缺毋滥;语义向量(MlxEmbedding)与步骤作用域条目放量为 v2。

命中语义(Phase 3.2,spec 2026-09-18-flow-graph-phase3 §2):同义簇
(cluster_head_id)在装配时折组——变体命中由簇头代表出场,簇内按「本通最少
播放」轮换(账本在 FlowController.qa_played)。不折分数:胜者键与分数逐字节
不变(BOK_QA_ROTATION=0 回裸索引竞争者档)。
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


def qa_priority_enabled() -> bool:
    """优先级档开关(Phase 3.1):0=回纯分数档(旧胜者键)。"""
    return os.environ.get("BOK_QA_PRIORITY", "1") == "1"


def qa_rotation_enabled() -> bool:
    """多答案轮换开关(Phase 3.2):0=回裸索引竞争者档(变体自己赢、播自己的答案)。"""
    return os.environ.get("BOK_QA_ROTATION", "1") == "1"


def _entry_priority(entry: dict) -> int:
    """条目优先级(小者先);旧 CP 响应/坏值宽容回默认 10,域 [0,1000]。"""
    raw = entry.get("priority", 10)
    if raw is None:
        return 10
    try:
        return max(0, min(int(raw), 1000))
    except (TypeError, ValueError):
        return 10


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / max(1e-9, na * nb)


def pick_rotation_member(members: list[dict], played: list[str]) -> dict:
    """簇内轮换取员(纯函数,Phase 3.2):本通播放次数最小者先,平则插入序。

    `played` 是已播出条目 id 序(`FlowController.qa_played`);只读不改。
    空成员表=契约违约(上游 len>1 门已挡),响亮抛错而非静默回 None——
    静默会让快路播空音频。
    """
    if not members:
        raise ValueError("pick_rotation_member: members 不可为空")
    # min 稳定:同计数时保留迭代序首(=插入序=created_at),正是轮换的平局语义。
    return min(members, key=lambda m: played.count(str(m.get("id") or "")))


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
        # 同义簇折组(Phase 3.2,spec §2):变体(cluster_head_id 非空且 head 在场可用)
        # 折进 head 的候选组——head 居首、其后按索引插入序(=created_at);head 唔在
        # 索引(删/禁/空问法/自指 id)的变体退独立条目(旧行为,优雅降级)。
        # _cluster_of 是变体 id → 簇头 id 的归一面(cluster_members 传变体 id 时用)。
        self._clusters: dict[str, list[dict]] = {}
        self._cluster_of: dict[str, str] = {}
        self._build_clusters()

    def _build_clusters(self) -> None:
        by_id = {str(e.get("id") or ""): e for e, _q, _v in self._items}
        for e, _q, _v in self._items:
            eid = str(e.get("id") or "")
            hid = str(e.get("cluster_head_id") or "")
            if not hid or hid == eid or hid not in by_id:
                continue  # 无簇 / 自指 / 孤儿(head 不可命中)→ 独立条目
            members = self._clusters.get(hid)
            if members is None:
                members = self._clusters[hid] = [by_id[hid]]  # head 恒居首
            members.append(e)
            self._cluster_of[eid] = hid

    def __len__(self) -> int:
        return len(self._items)

    def cluster_members(self, head_id: str) -> list[dict]:
        """簇成员表(head 自身+在场变体,插入序;副本)。

        传簇头 id 或组内任一成员 id 都归一到同一簇(变体 id 经折组后唔会出线,
        但 by_id/探针等调用面可能直传变体)。无簇条目 → 其自身单元素表(调用方
        按 len>1 判独立档);完全唔在场嘅 id → []。mutate 返回值唔会影响索引。
        """
        wanted = str(head_id or "")
        if not wanted:
            return []
        members = self._clusters.get(wanted)
        if members is not None:
            return list(members)
        owner = self._cluster_of.get(wanted)
        if owner:
            return list(self._clusters.get(owner) or [])
        entry = self.by_id(wanted)
        return [entry] if entry is not None else []

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
        best_key: tuple | None = None
        best_score = 0.0  # 胜者分数(过关者中按序取)
        top_score = 0.0   # 全场最高分(未过关时的诊断返回,旧档语义)
        # 优先级档(Phase 3.1):阈值过关者中小者先,同优先级分数降序,再平吃插入序
        # (=created_at,与旧档平局语义一致);全默认(都 10)时胜者与旧纯分数档
        # 逐字节同。kill-switch BOK_QA_PRIORITY=0 回纯分数档。
        use_priority = qa_priority_enabled()
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
            if score > top_score:
                top_score = score
            if score <= 0.0 or score < thr:
                continue  # 阈值先行(且零分永不入选,保 thr=0 边角与旧档逐字节同)——优先级只在过关者中排
            key = (_entry_priority(entry), -score) if use_priority else (-score,)
            if best_key is None or key < best_key:
                best, best_key, best_score = entry, key, score
        # 折组(Phase 3.2):胜者是变体 → 由簇头代表出场;胜者键与分数逐字节不变
        # (只换出场代表,3.1 优先级键语义零改动)。kill-switch=0 时逐字节同旧档。
        if best is not None and qa_rotation_enabled():
            head = self._team_head(best)
            if head is not None:
                return head, best_score
        if best is not None:
            return best, best_score
        return None, top_score

    def _team_head(self, entry: dict) -> dict | None:
        """胜者所属簇的 head 条目(无簇/孤儿/自指 → None,即原样返回胜者)。"""
        hid = self._cluster_of.get(str(entry.get("id") or ""))
        if not hid:
            return None
        members = self._clusters.get(hid)
        return members[0] if members else None

    def by_id(self, entry_id: str) -> dict | None:
        """按条目 id 直取(话术图 play_qa 绑定,spec §4.1);无命中返回 None。"""
        wanted = str(entry_id or "")
        if not wanted:
            return None
        for entry, _q, _v in self._items:
            if str(entry.get("id") or "") == wanted:
                return entry
        return None
