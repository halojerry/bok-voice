"""沉默戳话指标(2026-09-24 W4-③,防 main.py 膨胀;纯函数模块镜像 qa_drift.py 形状)。

真实通话里最大单一客户轮次是「有冇人知道」×479——定性结论见
docs/superpowers/plans/2026-09-24-a-line-flow-latency-intent.md(W1a 落地记录):
这不是意图,是**沉默戳话=延迟症状**(AI 回复慢/哑轮后客户戳一句确认对面还在不在)。
本模块把它翻成 dashboard 可见指标:客户轮 transcript 命中戳话词族 → 计数,
按通话语言分桶,供 ``GET /api/stats/dashboard`` 响应的 ``silence_pokes`` 段消费。

口径(钉死,tests/test_silence_poke.py 钉住):

- 只认客户轮(role=="user" 且 speaker=="customer")。分析账本 speaker 列
  2026-09-10 起才有,旧行为空不计——报告偏保守不误归因,与 gap_mining 同纪律;
  B 线轮 speaker=me/other 天然被滤;
- **一轮至多计一次**:同轮命中多条词族短语(「有冇人知道,仲喺度嗎」)不重复计数;
- 语言桶取轮上 ``language`` 列(A 线通话语言装配时一次钉死、全程不切)——不是
  命中短语自身的词族语言:粤语通话被转写漂移抄成普通话词族时,按通话语言归桶,
  运营看到的才是「哪条语言线在卡」;旧行 language 为空时退按命中短语的词族语言;
- 匹配=casefold 后**字面子串**(零正则、零归一化依赖,可预测)。宁可少算不可滥算:
  词面变体靠扩词族解决,不靠放宽匹配;命中不了不进数,绝不猜;
- 零手写 SQL(查询面只走 BusinessRepository 公开方法 get_turns 逐通取数,
  gap_mining/qa_drift 同款;dashboard 复用其既有 calls 清单,不另开窗口),
  不新增表/列/迁移。
"""

from __future__ import annotations

# ---- 词族(保守起步,扩词族改这里) ----
# (lang, phrase);lang 为三态规范值 zh / cantonese / en,表示短语词族来源
# (语言铁律见 AGENTS.md:粤语规范值=小写 cantonese,全栈唯一拼写)。
# 只在客户轮 transcript 上做 casefold 子串匹配;变体写法逐条加行,不加通配。
SILENCE_POKE_PHRASES: tuple[tuple[str, str], ...] = (
    ("cantonese", "有冇人知道"),
    ("cantonese", "有人知道嗎"),
    ("cantonese", "喂喂"),
    ("cantonese", "仲喺度嗎"),
    ("cantonese", "聽到嗎"),
    ("zh", "有人吗"),
    ("zh", "在吗"),
    ("zh", "你还在吗"),
    ("zh", "听得到吗"),
    ("zh", "喂？喂"),
    ("en", "are you there"),
    ("en", "hello? hello"),
    ("en", "can you hear me"),
    ("en", "anyone there"),
)

# 加载期 casefold 一次(词族表是常量,不在热路径反复折大小写)。
_COMPILED: tuple[tuple[str, str], ...] = tuple(
    (lang, phrase.casefold()) for lang, phrase in SILENCE_POKE_PHRASES
)


def is_silence_poke(text: str) -> bool:
    """客户轮文本是否命中戳话词族(casefold 子串;空文本恒 False)。纯函数。"""
    hay = str(text or "").strip().casefold()
    if not hay:
        return False
    return any(phrase in hay for _lang, phrase in _COMPILED)


def _phrase_lang(text: str) -> str:
    """命中短语自身的词族语言(旧行 language 列缺省时的归桶兜底)。

    多命中取词族表序首个(表序固定,结果确定);不命中(调用方已先过闸,
    理论不可达)兜 zh,永不返回空串。
    """
    hay = str(text or "").strip().casefold()
    for lang, phrase in _COMPILED:
        if phrase in hay:
            return lang
    return "zh"


def count_silence_pokes(turns) -> dict:
    """turns(单通或跨通混排均可)→ {"pokes": 命中轮数, "by_lang": {lang: n}}。

    只数客户轮(role=="user" 且 speaker=="customer");一轮至多计一次;
    语言桶=轮 language 列(空缺省退命中短语的词族语言,见模块 docstring)。
    零 I/O,turns 缺失/空列表/None 一律返回零值形状(不炸调用方)。
    """
    total = 0
    by_lang: dict[str, int] = {}
    for t in turns or []:
        role = str(getattr(t, "role", "") or "")
        speaker = str(getattr(t, "speaker", "") or "")
        if role != "user" or speaker != "customer":
            continue
        text = str(getattr(t, "transcript", "") or "")
        if not is_silence_poke(text):
            continue
        total += 1
        lang = str(getattr(t, "language", "") or "").strip()
        if not lang:
            lang = _phrase_lang(text)
        by_lang[lang] = by_lang.get(lang, 0) + 1
    return {"pokes": total, "by_lang": by_lang}


def merge_poke_stats(per_call: list[dict]) -> dict:
    """逐通 count_silence_pokes 结果列表 → dashboard ``silence_pokes`` 段。

    {"pokes": 命中轮数合计, "calls": 涉及通话数(≥1 轮命中的通数), "by_lang": 合并桶}。
    零命中通不计入 calls;空输入返回全零形状。
    """
    total = 0
    calls = 0
    by_lang: dict[str, int] = {}
    for s in per_call or []:
        n = int((s or {}).get("pokes") or 0)
        if n <= 0:
            continue
        total += n
        calls += 1
        for lang, k in ((s or {}).get("by_lang") or {}).items():
            key = str(lang)
            by_lang[key] = by_lang.get(key, 0) + int(k)
    return {"pokes": total, "calls": calls, "by_lang": by_lang}
