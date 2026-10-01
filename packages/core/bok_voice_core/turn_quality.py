"""用户轮质量判据纯函数(2026-09-28,ASR 碎裂/复问车道)。

零依赖(仅 re)。两件事:
- ``band_from_confidence``:把 sidecar 句级置信度读数折成三态
  ``low/ok/unknown``,供 garbled-reask 车道的门控用。**置信度只是辅助信号**
  ——窄带数字错听实测高置信(conf 高而错),所以数字 run/热词/verdict 旁路
  才是主护栏,lane 的 fire 判据必须是 ``band == "low"`` 或
  ``band == "unknown"`` 且文本确实像碎片,不能单靠 conf。
- ``looks_garbled``:确定性「这轮转写像碎片/回声残渣」判据——剥掉数字串、
  空白标点、热词词表命中后,剩余实质内容(CJK/拉丁字母)不足 ``min_content_chars``
  → 判 garbled。数字串与热词本身不算内容:报号码轮、纯词表回声轮都会被
  剥空,正是「错语境垫话/胡答」的高发形态。

风格镜像 ``qa_cluster.py``:纯 stdlib、模块级常量、可离线单测。
"""

from __future__ import annotations

import re

# 数字串:ASCII、全角、中文数字(含「俩/两」)一个字一个字地剥。
_DIGIT_CHAR_RE = re.compile(r"[0-9０-９〇零一二三四五六七八九俩两]")
# 标点/空白(ASCII + CJK)全剥——它们不构成语义内容。全角标点只取标点段,
# 全角字母(Ａ-ｚ)不算标点、照当内容(CJK 标点 U+3000-303F,全角标点 FF01-FF0F/
# FF1A-FF20/FF3B-FF40/FF5B-FF65,通用破折号/引号 2010-2027/2030-205E)。
_PUNCT_SPACE_RE = re.compile(
    r"[\s!-/:-@\[-`{-~"
    r"\u3000-\u303f"
    r"\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
    r"\u2010-\u2027\u2030-\u205e]"
)


def band_from_confidence(
    conf: dict | None,
    mean_thr: float,
    low_ratio: float,
) -> str:
    """句级置信度 → "low"|"ok"|"unknown"(纯函数,窄带高置信错听不迷信)。

    conf 形状同 sidecar ``/api/finish`` 的 ``{mean,min,low_tokens,n_tokens}``
    (QWEN3_ASR_CONFIDENCE 开档暴露):None/缺键/非数值 → "unknown";mean 低于
    ``mean_thr`` → "low";``n_tokens > 0`` 且 ``low_tokens/n_tokens >= low_ratio``
    → "low";否则 "ok"。任何异常一律 fail-open 回 "unknown"(绝不因置信度读数
    异常而误伤/误开 lane)。
    """
    if not isinstance(conf, dict):
        return "unknown"
    try:
        mean = conf.get("mean")
        if mean is None:
            return "unknown"
        mean_f = float(mean)
        if mean_f < float(mean_thr):
            return "low"
        n_tokens = conf.get("n_tokens")
        low_tokens = conf.get("low_tokens")
        if n_tokens is not None and low_tokens is not None:
            n = int(n_tokens)
            if n > 0 and int(low_tokens) / n >= float(low_ratio):
                return "low"
    except (TypeError, ValueError):
        return "unknown"
    return "ok"


def _is_content_char(ch: str) -> bool:
    """实质内容字符:CJK(含扩展)或拉丁字母。"""
    return ("\u4e00" <= ch <= "\u9fff") or ch.isalpha()


def looks_garbled(
    text: str,
    hotword_terms: tuple = (),
    min_content_chars: int = 2,
) -> bool:
    """这轮转写像碎片/回声残渣吗(纯函数,确定性)。

    剥序:数字串(ASCII/全角/中文数字) → 空白与标点(ASCII+CJK) → 热词词表
    命中子串(双侧 casefold)。剩余实质内容(CJK 或拉丁字母)少于
    ``min_content_chars`` → True。空/纯空白 → True。

    ``hotword_terms`` 传**已切好的词**(调用方用 ``_parse_vocab_terms`` 拆),
    长词先剥防短词吃掉长词(拼多多 先于 多多)。
    """
    if not text or not str(text).strip():
        return True
    s = str(text)
    # 词表命中子串剥除:长词优先(稳定排序按长度降序)。
    terms = sorted(
        {str(t).strip().casefold() for t in (hotword_terms or ()) if str(t).strip()},
        key=len,
        reverse=True,
    )
    cf = s.casefold()
    for term in terms:
        if term and term in cf:
            cf = cf.replace(term, "")
    cf = _DIGIT_CHAR_RE.sub("", cf)
    cf = _PUNCT_SPACE_RE.sub("", cf)
    content = sum(1 for ch in cf if _is_content_char(ch))
    return content < max(0, int(min_content_chars))
