"""Q→A 检索快路:匹配器与资格闸(纯函数,PR-3;设计见 docs/superpowers/specs/2026-09-08-qa-fastpath-design.md)。

用户轮提交后、LLM 调用前:四道闸全过 + 条目命中 + 应答音频已预生成 →
跳过 LLM 直接播缓存音频(命中轮 ~50ms)。任一不满足 → 照旧走 LLM。

打分照抄知识库 InMemoryVectorStore:0.6×余弦 + 0.4×子串长度比,向量用
HybridLexicalEmbedding(CJK 逐字+bigram 哈希,零外部依赖)。阈值默认 0.90
宁缺毋滥;**语义补位车道已落地(2026-09-24,W3b 解锁)= QaSemanticIndex**:
词面 0.90 未中轮的本地 embedding 释义档(阈值 0.80,词面恒绝对优先,
BOK_QA_SEMANTIC=0 一键回纯词面档)。

命中语义(Phase 3.2,spec 2026-09-18-flow-graph-phase3 §2):同义簇
(cluster_head_id)在装配时折组——变体命中由簇内代表出场,簇内按「本通最少
播放」轮换(账本在 FlowController.qa_played)。不折分数:胜者键与分数逐字节
不变(BOK_QA_ROTATION=0 回裸索引竞争者档)。

折组/轮换池**绝不比 match 的命中面宽**(C1,2026-09-18 终审):代表与成员表
都按本通 `lang`/`step_index` 过滤(= match 同款判据 `_survives_turn`),跨
scope 变体唔会在异步出线、跨语言成员唔会在本语通话播——整簇滤光回裸胜者。

召回排序 ``QaIndex.rank()``(2026-09-25,Laya QA 验证车道候选供给):与 0.90
快道**完全独立**的评分面——0.60×词面余弦 + 0.25×双向子串(镜像 fillers 的
双向姿势,治「超集句」单向缺口)+ 0.15×拼音 bigram Dice(治 ASR 同音错字,
pypinyin 缺库/``BOK_QA_PINYIN=0`` 时通道恒 0、余两项按比例归一)。幸存面与
折组纪律同 match;``match()`` 本体零改动。

同音归一应用钩子(2026-09-25,**只挂语义召回面**):沉淀引擎把真实通话学到的
同音对子(裴→赔)经 CP 拉取、构造注入 ``QaSemanticIndex(homophones=)``——
``match()``/``rank()`` 的查询文本在 ``normalize_question`` 之后按对子归一再
embed(词条素材向量不动,双方在语义空间自然接近)。词面 ``QaIndex.match``/
``rank`` 恒不吃(0.90 快道零漂移铁律)。策略函数在
``bok_voice_core.qa_digest_policy.apply_homophones``(并行交付):import 失败/
调用失败一律原查询逐字节(优雅降级);``BOK_QA_HOMOPHONE=0`` 整钩子旁路,
表空/无对子=行为逐字节同旧。
"""

from __future__ import annotations

import hashlib
import os

from bok_voice_core.embeddings import HybridLexicalEmbedding
from bok_voice_core.qa_text import normalize_question

try:  # pypinyin 纯 Python 轻依赖;缺失时 rank 拼音通道静默降级为恒 0 分
    from pypinyin import lazy_pinyin
except ImportError:  # pragma: no cover - 取决于安装面
    lazy_pinyin = None  # type: ignore[assignment]
    print("QA_PINYIN unavailable: pypinyin 未安装,召回排序拼音通道恒 0 分")

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


def qa_pinyin_enabled() -> bool:
    """召回拼音通道开关(rank 专用,2026-09-25):0=通道权重置 0,余两项按
    0.60/0.25 比例归一;只影响召回排序,0.90 词面快道字节不变。"""
    return os.environ.get("BOK_QA_PINYIN", "1") == "1"


def qa_recall_k() -> int:
    """rank 默认召回条数(BOK_QA_RECALL_K,坏值回 8,下限 1)。"""
    try:
        return max(1, int(os.environ.get("BOK_QA_RECALL_K", "8")))
    except ValueError:
        return 8


def qa_recall_floor() -> float:
    """rank 默认分数地板(BOK_QA_RECALL_FLOOR,坏值回 0.40,钳 [0,1])。

    0.40=golden 标定拐点(scripts/qa_match_report.py floor sweep):adjacent 负样本
    入侵 0/20 档的最大召回(0.35 档 1/20、0.55 档丢 6pt 召回)。
    """
    try:
        return max(0.0, min(1.0, float(os.environ.get("BOK_QA_RECALL_FLOOR", "0.40"))))
    except ValueError:
        return 0.40


def qa_semantic_enabled() -> bool:
    """语义补位车道总闸(W3b 解锁,2026-09-24):词面 0.90 未中轮的本地 embedding 补位。"""
    return os.environ.get("BOK_QA_SEMANTIC", "1") == "1"


def qa_homophone_enabled() -> bool:
    """同音归一应用钩子开关(语义召回面专用,2026-09-25):0=整个钩子旁路,
    行为逐字节同旧。词面 0.90 快道与词面 rank 恒不吃归一(零漂移铁律)。"""
    return os.environ.get("BOK_QA_HOMOPHONE", "1") == "1"


def qa_semantic_threshold() -> float:
    """释义档阈值(默认 0.80,比意图车道 0.78 高一档):快路播固定罐头答案,
    误命中代价(答非所问的录音)高于意图跳转——宁缺毋滥。"""
    try:
        return max(0.0, min(1.0, float(os.environ.get("BOK_QA_SEM_THRESHOLD", "0.80"))))
    except ValueError:
        return 0.80


def qa_semantic_base_url() -> str:
    raw = os.environ.get("BOK_QA_SEM_BASE_URL", "").strip()
    return raw or "http://127.0.0.1:8789"


def qa_semantic_timeout_s() -> float:
    """毫秒→秒;坏值回默认 400ms(与意图车道同预算:热路径同步等,必须小)。"""
    try:
        return max(0.0, float(os.environ.get("BOK_QA_SEM_TIMEOUT_MS", "400"))) / 1000.0
    except ValueError:
        return 0.4


def _entry_priority(entry: dict) -> int:
    """条目优先级(小者先);旧 CP 响应/坏值宽容回默认 10,域 [0,1000]。"""
    raw = entry.get("priority", 10)
    if raw is None:
        return 10
    try:
        return max(0, min(int(raw), 1000))
    except (TypeError, ValueError):
        return 10


def _survives_turn(entry: dict, *, lang: str | None, step_index: int | None) -> bool:
    """条目是否通过本轮语言/步骤过滤——与 `match()` 逐字同判据(单点防漂移)。

    lang 空/None=不滤语言(同 match `if lang and …`);`scope=="step"` 条目:
    step_index 为 None(=本轮无步骤上下文)或与条目 step_index 不等 → 滤出。
    """
    if lang and str(entry.get("lang") or "") and str(entry["lang"]) != lang:
        return False
    if str(entry.get("scope") or "global") == "step":
        if step_index is None or int(entry.get("step_index") or -1) != int(step_index):
            return False
    return True



def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / max(1e-9, na * nb)


# rank() 召回权重(2026-09-25):词面余弦主导,双向子串次之,拼音补同音错字。
_W_COS = 0.60
_W_SUB = 0.25
_W_PINYIN = 0.15


def _bidir_substring_hit(q_low: str, e_low: str) -> bool:
    """双向子串命中(rank 专用):归一化后 客户话⊂词条 或 词条⊂客户话 任一向
    即真——镜像 fillers.py 的双向姿势(单向只认 user⊂entry 会漏「超集句」)。"""
    return bool(q_low) and bool(e_low) and (q_low in e_low or e_low in q_low)


def _pinyin_syllables(text: str) -> list[str]:
    """文本→无声调拼音音节表;只留 a-z 音节(lazy_pinyin 会透传标点,滤掉防
    垃圾 bigram)。pypinyin 缺失/单条转换失败 → 空表(通道分恒 0,绝不 raise)。"""
    if lazy_pinyin is None:
        return []
    try:
        raw = lazy_pinyin(text)
    except Exception:  # noqa: BLE001 - 单条转换失败当无拼音
        return []
    return [s for s in raw if s.isascii() and s.isalpha()]


def _pinyin_bigram_score(query_syl: list[str], entry_syl: list[str]) -> float:
    """拼音二元音节对重叠 Dice 系数 2|A∩B|/(|A|+|B|);任一侧 <2 音节 → 0。"""
    if len(query_syl) < 2 or len(entry_syl) < 2:
        return 0.0
    q_big = {query_syl[i] + query_syl[i + 1] for i in range(len(query_syl) - 1)}
    e_big = {entry_syl[i] + entry_syl[i + 1] for i in range(len(entry_syl) - 1)}
    inter = len(q_big & e_big)
    if not inter:
        return 0.0
    return 2.0 * inter / (len(q_big) + len(e_big))


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
        #
        # 单层折(M-r4,2026-09-18 终审勘误):本循环按「变体的目标是不是一个在场
        # 条目」建组,唔追链——链式簇 v2→v1→head 产出**重叠池**
        # {head:[head,v1], v1:[v1,v2]}:v2 命中折到 v1(不是 head)、head 经孙辈
        # 命中不可达,且 v1 在两个池都可播(重复出线)。两层是 web 簇守卫/导入器
        # 的既定上限(CP 不校验);真要根治需建组时归一链根,非本增量范围。
        self._clusters: dict[str, list[dict]] = {}
        self._cluster_of: dict[str, str] = {}
        self._build_clusters()
        # 拼音通道预计算(rank 召回专用,2026-09-25):词条侧每词条一次,查询侧
        # 每次现算。match() 不吃拼音(0.90 快道字节不变);缺库时表恒空=通道 0。
        # 按条目 id 键控;rank 取不到(空 id/重复 id)时现场补算兜底。
        self._pinyin: dict[str, list[str]] = {}
        if lazy_pinyin is not None:
            for e, _q, _v in self._items:
                eid = str(e.get("id") or "")
                if eid and eid not in self._pinyin:
                    self._pinyin[eid] = _pinyin_syllables(str(e.get("question_text") or ""))

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

    def cluster_members(
        self,
        head_id: str,
        *,
        lang: str | None = None,
        step_index: int | None = None,
    ) -> list[dict]:
        """簇成员表(head 自身+在场变体,插入序;副本)。

        传簇头 id 或组内任一成员 id 都归一到同一簇(变体 id 经折组后唔会出线,
        但 by_id/探针等调用面可能直传变体)。无簇条目 → 其自身单元素表(调用方
        按 len>1 判独立档);完全唔在场嘅 id → []。mutate 返回值唔会影响索引。

        过滤(C1,2026-09-18 终审):给 `lang` 或 `step_index`(任一非 None)即进
        过滤档,成员按 `_survives_turn`(= match 同款判据)筛——轮换池绝不比
        本轮命中面宽(跨 scope 变体唔会在异步出线、跨语言成员唔会在本语通话
        播)。`step_index=None` 在过滤档=本轮无步骤上下文 → 步作用域成员滤出。
        两参全 None = 旧调用形态,整簇原样返回(向后兼容)。
        """
        wanted = str(head_id or "")
        if not wanted:
            return []
        if wanted in self._clusters:
            resolved = list(self._clusters[wanted])
        else:
            owner = self._cluster_of.get(wanted)
            if owner:
                resolved = list(self._clusters.get(owner) or [])
            else:
                entry = self.by_id(wanted)
                resolved = [entry] if entry is not None else []
        if lang is None and step_index is None:
            return resolved
        return [m for m in resolved if _survives_turn(m, lang=lang or "", step_index=step_index)]

    def match(
        self,
        user_text: str,
        *,
        lang: str = "",
        step_index: int | None = None,
        threshold: float | None = None,
    ) -> tuple[dict | None, float]:
        """返回 (命中代表条目, 得分);未过阈值/作用域不符/语言不符 → (None, best)。

        折组开(qa_rotation_enabled)且胜者属簇:代表 = 簇内**首个幸存成员**
        (按本轮 lang/step 过滤;可能 head,也可能 head 被滤掉后的另一变体——
        它正是本轮的合法命中面);整簇滤光 → 裸胜者(旧行为)。kill 档=裸胜者
        逐字节旧档。
        """
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
            if not _survives_turn(entry, lang=lang, step_index=step_index):
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
        # 折组(Phase 3.2):胜者是变体 → 由簇内**首个幸存成员**代表出场(按本通
        # lang/step 过滤,绝不比 match 命中面宽——C1);胜者键与分数逐字节不变
        # (只换出场代表,3.1 优先级键语义零改动)。整簇滤光 → 裸胜者。kill=0 同旧档。
        if best is not None and qa_rotation_enabled():
            head = self._team_head(best, lang=lang, step_index=step_index)
            if head is not None:
                return head, best_score
        if best is not None:
            return best, best_score
        return None, top_score

    def rank(
        self,
        query: str,
        *,
        k: int | None = None,
        floor: float | None = None,
        lang: str | None = None,
        step_index: int | None = None,
    ) -> list[tuple[float, str]]:
        """召回排序(Laya QA 验证车道候选供给,2026-09-25)。

        返回 (score, entry_id) 降序、≤k 条、score≥floor。评分与 match() 的
        0.90 快道**完全独立**(match 本体零改动):

          _W_COS × 词面余弦(复用 HybridLexicalEmbedding 同一套向量)
        + _W_SUB × 双向子串(归一化后任一向命中即 1.0,双向同义)
        + _W_PINYIN × 拼音 bigram Dice(ASR 同音错字补位;pypinyin 缺库或
          ``BOK_QA_PINYIN=0`` 时该项置 0,余两项按 0.60/0.25 比例归一——
          即 0.706/0.294,比例恒由权重常量推导,不另硬编码)。

        幸存面与 match 同口径:``_survives_turn(lang, step_index)`` 过滤 +
        同义簇折组——变体命中折到簇代表 team_head 出线(与 match 胜者同纪律,
        同簇多命中只留最高分一行);``BOK_QA_ROTATION=0`` 折组关 → 独立条目。

        ``k``/``floor`` 显式传入优先;缺省(None)读 ``BOK_QA_RECALL_K``/
        ``BOK_QA_RECALL_FLOOR``(坏值回 8 / 0.40)。空查询/空索引 → []。
        """
        q = normalize_question(query)
        if not q or not self._items:
            return []
        n = qa_recall_k() if k is None else max(1, int(k))
        thr = qa_recall_floor() if floor is None else max(0.0, min(1.0, float(floor)))
        use_pinyin = qa_pinyin_enabled() and lazy_pinyin is not None
        w_cos, w_sub, w_pin = _W_COS, _W_SUB, _W_PINYIN
        if not use_pinyin:
            rest = _W_COS + _W_SUB
            w_cos, w_sub, w_pin = _W_COS / rest, _W_SUB / rest, 0.0
        qv = self._embed.embed([q])[0]
        q_low = q.lower()
        q_syl = _pinyin_syllables(q) if use_pinyin else []
        # 同簇多命中只留最高分一行:按代表 dict 对象身份去重(代表可能为空 id,
        # 对象身份在调用期内稳定且跨簇唯一)。
        best_by_rep: dict[int, tuple[float, str]] = {}
        for entry, _e_q, e_vec in self._items:
            if not _survives_turn(entry, lang=lang or "", step_index=step_index):
                continue
            e_q_low = str(entry.get("question_text") or "")
            e_q_low = normalize_question(e_q_low).lower()
            score = w_cos * _cos(qv, e_vec)
            if _bidir_substring_hit(q_low, e_q_low):
                score += w_sub
            if use_pinyin:
                e_syl = self._pinyin.get(str(entry.get("id") or ""))
                if e_syl is None:
                    e_syl = _pinyin_syllables(e_q_low)
                score += w_pin * _pinyin_bigram_score(q_syl, e_syl)
            rep = entry
            if qa_rotation_enabled():
                head = self._team_head(entry, lang=lang or "", step_index=step_index)
                if head is not None:
                    rep = head
            rid = id(rep)
            prev = best_by_rep.get(rid)
            if prev is None or score > prev[0]:
                best_by_rep[rid] = (score, str(rep.get("id") or ""))
        rows = [(s, eid) for s, eid in best_by_rep.values() if s >= thr]
        rows.sort(key=lambda t: -t[0])  # 稳定排序:平分保持插入序(=索引序)
        return rows[:n]

    def _team_head(
        self,
        entry: dict,
        *,
        lang: str = "",
        step_index: int | None = None,
    ) -> dict | None:
        """胜者所属簇的**首个幸存成员**(按本通 lang/step 过滤)。

        折组只在成员通过本轮过滤时换代表——head 被滤掉而变体幸存 → 代表=该
        变体(它正是本轮的合法命中面);整簇滤光 → None(调用方裸胜者旧行为)。
        无簇/孤儿/自指 → None。
        """
        hid = self._cluster_of.get(str(entry.get("id") or ""))
        if not hid:
            return None
        survivors = [
            m for m in (self._clusters.get(hid) or [])
            if _survives_turn(m, lang=lang, step_index=step_index)
        ]
        return survivors[0] if survivors else None

    def by_id(self, entry_id: str) -> dict | None:
        """按条目 id 直取(话术图 play_qa 绑定,spec §4.1);无命中返回 None。"""
        wanted = str(entry_id or "")
        if not wanted:
            return None
        for entry, _q, _v in self._items:
            if str(entry.get("id") or "") == wanted:
                return entry
        return None

    def team_head(
        self,
        entry: dict,
        *,
        lang: str = "",
        step_index: int | None = None,
    ) -> dict | None:
        """语义补位胜者的簇折组出口(公开面,2026-09-24):与词面 match 内部
        `_team_head` 同款判据——胜者属簇且簇内有幸存成员 → 首个幸存成员代表
        出场;无簇/整簇滤光 → None(调用方裸胜者)。"""
        return self._team_head(entry, lang=lang, step_index=step_index)


def _homophone_pairs(raw: list | None) -> list[tuple[str, str]]:
    """CP 端点形状 → ``(wrong, right)`` 元组列(装配时一次归一,坏项静默丢弃)。

    接受 dict 列(``{"wrong","right","support"}``;support 仅为溯源信息,此处
    忽略)或二元组列;缺键/非字符串/空串/自反(wrong==right)/形状不明一律
    跳过——装配面坏数据绝不炸通话。
    """
    pairs: list[tuple[str, str]] = []
    for item in raw or []:
        if isinstance(item, dict):
            wrong, right = item.get("wrong"), item.get("right")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            wrong, right = item[0], item[1]
        else:
            continue
        if not isinstance(wrong, str) or not isinstance(right, str):
            continue
        wrong, right = wrong.strip(), right.strip()
        if not wrong or not right or wrong == right:
            continue
        pairs.append((wrong, right))
    return pairs


def _homophone_normalize(q: str, pairs: list[tuple[str, str]]) -> str:
    """查询侧同音归一(策略单点):``apply_homophones`` 缺失/失败 → 原查询逐字节。

    策略函数在 ``bok_voice_core.qa_digest_policy``(并行交付面),**惰性 import**
    ——包缺席是常态部署形态,import 失败=无对子行为;调用期任何异常也兜住,
    召回面绝不因归一炸轮。
    """
    if not q or not pairs:
        return q
    try:
        from bok_voice_core.qa_digest_policy import apply_homophones
    except ImportError:
        return q
    try:
        out = apply_homophones(q, pairs)
    except Exception:  # noqa: BLE001 - 策略面任何失败都不许炸召回
        return q
    return out if isinstance(out, str) and out else q


class QaSemanticIndex:
    """快答库语义补位索引(2026-09-24,W3b 解锁):词面 0.90 未中轮的释义档。

    纪律镜像 W1b 意图车道(intent_semantic):
    - 素材向量经 ``SEMANTIC_VECTOR_CACHE``(模块级 LRU,键=条目 id+问法集
      sha256)——同目录反复外呼零重复 embed,改目录 → 哈希变 → 重算;
    - 装配构建分块批量(64/块,块预算 5s),查询每轮至多一次 embed(~20ms);
    - 端点缺席/连续失败 → EmbedClient 降级闩,本通惰性(快路行为=旧档);
    - **词面档恒绝对优先**:语义只在词面 match 未中时补位(调用方职责),
      命中条目进与词面同款的轮换/PCM 出场链(折组经 QaIndex.team_head);
    - 胜者=最高余弦、平分吃插入序,**不进优先级 duel**——补位车道语义:
      词面档的 (priority,-score) 契约不延伸到这里,最贴近的问法直接出场;
    - **同音归一只在查询侧**(2026-09-25):构造注入 ``homophones=``(CP 已学
      对子),``match``/``rank`` 的查询文本经 ``apply_homophones`` 归一后再
      embed——词条素材向量在 build 时已按原问法固化,**不动**(改素材=改既
      有缓存序列;查询与素材同在语义空间,归一查询自然贴近正字素材)。
      ``BOK_QA_HOMOPHONE=0`` 旁路;表空/策略缺失/失败=行为逐字节同旧。
    """

    def __init__(
        self,
        client,
        items: list[tuple[dict, str, list[float]]],
        *,
        homophones: list | None = None,
    ):
        self._client = client
        self._items = items  # (entry, 归一问法, 语义向量),插入序=created_at
        # 同音对子:CP 形状(dict/元组列)在此一次归一,坏项丢弃;None/空=旧行为。
        self._homophones: list[tuple[str, str]] = _homophone_pairs(homophones)

    def __len__(self) -> int:
        return len(self._items)

    @classmethod
    async def build(
        cls,
        client,
        entries: list[dict],
        *,
        homophones: list | None = None,
    ) -> "QaSemanticIndex | None":
        """装配构建(异步:批量 embed)。素材=去重归一问法(同问法多条目共享
        向量、首条出场——快路命中面按问法);零素材/任一批次失败 → None 整通
        惰性(零行为变化)。``homophones`` 只透传给构造(查询侧归一),素材
        embed 恒吃原归一问法、缓存键零变化(词面素材序列不动)。"""
        from .intent_semantic import (
            _BUILD_EMBED_CHUNK,
            _BUILD_EMBED_TIMEOUT_S,
            SEMANTIC_VECTOR_CACHE,
        )

        items: list[tuple[dict, str]] = []
        seen: set[str] = set()
        for e in entries or []:
            q = normalize_question(str(e.get("question_text") or ""))
            if not q or q in seen:
                continue
            seen.add(q)
            items.append((e, q))
        if not items:
            return None
        key = hashlib.sha256(
            "\n".join(f"{e.get('id')}|{q}" for e, q in items).encode("utf-8")
        ).hexdigest()
        cached = SEMANTIC_VECTOR_CACHE.get(key)
        if cached is None:
            texts = [q for _e, q in items]
            vecs: list[list[float]] = []
            for i in range(0, len(texts), _BUILD_EMBED_CHUNK):
                chunk = await client.embed(
                    texts[i : i + _BUILD_EMBED_CHUNK], timeout_s=_BUILD_EMBED_TIMEOUT_S
                )
                if chunk is None:
                    return None
                vecs.extend(chunk)
            cached = dict(zip(texts, vecs))
            SEMANTIC_VECTOR_CACHE.put(key, cached)
        return cls(client, [(e, q, cached[q]) for e, q in items], homophones=homophones)

    async def match(
        self,
        user_text: str,
        *,
        lang: str = "",
        step_index: int | None = None,
        threshold: float | None = None,
    ) -> tuple[dict | None, float, str]:
        """词面未中轮的语义补位 → (命中条目|None, 最佳余弦, reason)。

        reason ∈ ""|empty|no_embedder|timeout|error:空串=正常评分(未过阈值
        也是正常 miss,调用方可打 miss 日志);其余=车道不可用归因(静默降级,
        与意图车道同纪律——日志归 agent 统一打)。幸存过滤与词面 match 同判据
        `_survives_turn`(单点防漂移)。
        """
        from .intent_semantic import _cosines

        q = normalize_question(user_text)
        # 同音归一应用钩子(2026-09-25):顺序钉死=先 normalize_question 再按
        # 对子替换——归一化清标点/空白,对子替换吃干净文本;仅查询侧,词条
        # 素材向量不动(双方在语义空间自然接近)。表空/env 关/策略缺失/失败
        # → 原查询逐字节(=旧行为)。词面 QaIndex.match 恒不吃此钩子。
        if self._homophones and qa_homophone_enabled():
            q = _homophone_normalize(q, self._homophones)
        if not q or not self._items:
            return None, 0.0, "empty"
        if self._client is None or self._client.dead:
            return None, 0.0, "no_embedder"
        qv = await self._client.embed([q])
        if qv is None:
            return None, 0.0, (self._client.last_reason or "error")
        thr = qa_semantic_threshold() if threshold is None else threshold
        rows: list[list[float]] = []
        entries: list[dict] = []
        for e, _q, vec in self._items:
            if _survives_turn(e, lang=lang, step_index=step_index):
                entries.append(e)
                rows.append(vec)
        if not rows:
            return None, 0.0, "empty"
        coss = _cosines(qv[0], rows)
        best: dict | None = None
        best_score = -1.0
        top = 0.0
        for e, c in zip(entries, coss):
            if c > top:
                top = c
            if c >= thr and c > best_score:
                best, best_score = e, c
        if best is None:
            return None, top, ""
        return best, best_score, ""

    async def rank(
        self,
        user_text: str,
        *,
        k: int = 3,
        floor: float = 0.60,
        lang: str = "",
        step_index: int | None = None,
    ) -> list[tuple[float, str]]:
        """语义召回 top-k（Laya QA 验证车道的第二召回腿，2026-09-25 审计补）。

        golden 标定实证：改写/同音族的真实 miss 形态是「换词+同音叠加」，
        词面 rank 的拼音通道只救得动近逐字同音——语义余弦才是释义档的天然
        召回层。与 match() 同判据（``_survives_turn`` 单点）、同一份素材向量，
        但**不过 0.80 出场阈值**：floor（默认 0.60）是召回地板，精度交给
        Laya 精判车道。车道不可用（no_embedder/timeout/空面）=空列表
        （fail-open，调用方当池空，绝不炸）。返回 (cos, entry_id) 降序 ≤k。
        """
        from .intent_semantic import _cosines

        q = normalize_question(user_text)
        # 同音归一与 match 同款(顺序钉死:normalize→对子替换;仅查询侧,
        # 词面 QaIndex.rank 恒不吃——词面召回的对子收益归语义面管)。
        if self._homophones and qa_homophone_enabled():
            q = _homophone_normalize(q, self._homophones)
        if not q or not self._items:
            return []
        if self._client is None or self._client.dead:
            return []
        qv = await self._client.embed([q])
        if qv is None:
            return []
        entries: list[dict] = []
        rows: list[list[float]] = []
        for e, _q, vec in self._items:
            if _survives_turn(e, lang=lang, step_index=step_index):
                entries.append(e)
                rows.append(vec)
        if not rows:
            return []
        coss = _cosines(qv[0], rows)
        scored = [
            (float(c), str(e.get("id") or ""))
            for e, c in zip(entries, coss)
            if c >= floor and e.get("id")
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        return scored[:k]

