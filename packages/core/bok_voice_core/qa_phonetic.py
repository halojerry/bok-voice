"""粤语音系容错匹配(纯函数,2026-09-22;qa_gate 字面 miss 后的第二段)。

治「同音/碎裂变体字面失配」:客户说「你要怎么陪我呢」(赔→陪)、「什么快递?
係啊!」vs 词条「咩快遛」——字面闸(0.6 余弦+0.4 子串@0.90)对简繁混杂+同音
替换+插语气词的真实 ASR 转写结构性失配。本模块把词条与用户话语都转成粤拼
音节候选序列(vendored ToJyutping,简繁双收、非汉字落 None 天然免疫数字/英文
混排),再做声韵调加权槽位对齐。

权重与阈值有真库实测依据(2026-09-22,873 条粤语真实客户轮 × 35 粤语词条):
声母 0.35/韵母 0.45/声调 0.20、阈值 0.80 → 精确率 95%(唯一 FP=陈述句
「这好像是我的快递」)、FP≈1‰/轮、端到端 <1ms/轮;候选读音展开必须保留
(关掉丢 6 个 TP,「怎么赔给我」跨方言桥靠 怹 的异读 dim2 桥接 點)。
zh 线(拼音)实测精确率仅 54%@0.80(「怎么截图→怎么赔」半同音凑分),**不
提供 zh 档**——本模块只服务 cantonese 通话。

已知边界:口语词汇变体(客户说「打錯嗰喎」根本没说「電話」)音系层救不了,
靠词条带口语变体行(L-② intent_keyword 提案管线挖);≥6 音节长问句分数被
摊薄,长改写归语义层管。

无 env 读取(kill-switch 在 agent_runtime.qa_gate——test_forward_env 扫描面);
trie 解析 ~160ms 是一次性 import 成本,由 qa_gate 侧惰性 import 兜住(真有
粤语条目才付)。
"""

from __future__ import annotations

# 默认放行阈值(真库实测推荐值;qa_gate 侧 BOK_QA_PHONETIC_THRESHOLD 可调)
DEFAULT_THRESHOLD = 0.80

# 候选读音截断(每字至多保留这么多异读——「投宿」类多音字靠它桥接)
_MAX_CANDIDATES_PER_CHAR = 4

# 转换缓存(词条每通装配都会重转,QaIndex 按通话重建;容量截断防无界)
_CACHE_MAX = 4096
_syllable_cache: dict[str, list] = {}


def _converter():
    """惰性取 vendored ToJyutping 单例(首次 import 触发 trie 解析 ~160ms)。"""
    from .tojyutping_vendor.ToJyutping import get_jyutping_candidates, get_jyutping_list
    from .tojyutping_vendor.ToJyutping.Jyutping import Jyutping

    return get_jyutping_list, get_jyutping_candidates, Jyutping


def text_to_syllables(text: str) -> list:
    """文本 → 每汉字一格的粤拼候选读音序列(非汉字跳过;带进程级缓存)。

    返回 list[list[Jyutping]]:外层按汉字序,内层是该字的候选读音(首选居首,
    至多 _MAX_CANDIDATES_PER_CHAR 个)。无任何可转换汉字 → []。
    """
    key = str(text or "")
    if not key:
        return []
    hit = _syllable_cache.get(key)
    if hit is not None:
        return hit
    get_list, get_candidates, Jyutping = _converter()
    out: list[list] = []
    for ch, jp in get_list(key):
        if jp is None:
            continue
        try:
            cands = [Jyutping(jp)]
        except ValueError:
            continue
        for _tag, cl in get_candidates(ch):
            for c in cl[:3]:
                try:
                    j = Jyutping(c)
                except ValueError:
                    continue
                if all(j.id != x.id for x in cands):
                    cands.append(j)
                if len(cands) >= _MAX_CANDIDATES_PER_CHAR:
                    break
            if len(cands) >= _MAX_CANDIDATES_PER_CHAR:
                break
        out.append(cands)
    if len(_syllable_cache) >= _CACHE_MAX:
        _syllable_cache.clear()
    _syllable_cache[key] = out
    return out


def pair_sim(x, y) -> float:
    """单音节相似度:声母 0.35/韵母 0.45/声调 0.20(零声母与 ng/w 给半程分)。"""
    osim = 1.0 if x.onset_id == y.onset_id else (0.5 if {x.onset_id, y.onset_id} in ({0, 19}, {0, 14}) else 0.0)
    return 0.35 * osim + 0.45 * (1.0 if x.rhyme_id == y.rhyme_id else 0.0) + 0.20 * (
        1.0 if x.tone_id == y.tone_id else 0.0
    )


def syl_sim(a, b) -> float:
    """槽位相似度:两侧候选读音取最大(治多音字/异读桥接)。"""
    return max((pair_sim(x, y) for x in a for y in b), default=0.0)


def align_score(kw: list, utt: list) -> float:
    """gappy 单调对齐:词条音节在话语音节流中各自向后找最优槽,均值计分。

    「打 错电话」类碎裂空格在转换层已被吞掉(非汉字跳过);插语气词只稀释
    不阻断(每个词条槽都能跳过间隙找到目标音节)。空序列 → 0.0。
    """
    m, n = len(kw), len(utt)
    if m == 0 or n == 0:
        return 0.0
    total = 0.0
    i = 0
    for a in kw:
        best, bestj = 0.0, -1
        for j in range(i, n):
            s = syl_sim(a, utt[j])
            if s > best:
                best, bestj = s, j
        total += best
        if bestj >= 0:
            i = bestj + 1
    return total / m
