"""免费无 key 的联网检索：给本地 LLM 补充实时事实。

数据源（均免费、无需 key）：
- Wikipedia：按目标语言查百科条目（zh/cantonese/en 各自子域 + 摘要）。
- DuckDuckGo Instant Answer：即时答案/相关主题。

返回统一格式的文本片段，注入 LLM system 上下文。任何失败都静默降级为空，
绝不阻塞通话（联网检索是增强，不是依赖）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import httpx

_TIMEOUT = httpx.Timeout(10.0)
_MAX_RESULTS = 3
_MAX_CHARS_PER = 600
# Wikipedia 拒默认 python-httpx UA（403）。用简短、真实的 UA 反而更稳（伪浏览器 UA 触发风控）。
_HEADERS = {"User-Agent": "BokVoice/0.1 (local voice assistant; contact: dev@bokvoice.local)"}


def _wiki_domain(lang: str) -> str:
    key = (lang or "").lower()
    if key in {"cantonese", "yue"}:  # yue = 旧数据只读别名;外部域名本身仍是 zh-yue
        return "zh-yue.wikipedia.org"
    if key == "en":
        return "en.wikipedia.org"
    return "zh.wikipedia.org"


def _trim(text: str, limit: int = _MAX_CHARS_PER) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _wiki_search(client: httpx.AsyncClient, query: str, lang: str) -> list[str]:
    domain = _wiki_domain(lang)
    url = f"https://{domain}/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 3,
        "format": "json",
        "utf8": 1,
    }
    try:
        r = await client.get(url, params=params, headers=_HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    hits = (data.get("query") or {}).get("search") or []
    out: list[str] = []
    for h in hits[:3]:
        title = h.get("title", "")
        snippet = _trim(str(h.get("snippet", "") or ""))
        # 摘要用 REST API 拿纯文本，比 search snippet 更干净。
        try:
            rest = await client.get(f"https://{domain}/api/rest_v1/page/summary/{_quote(title)}", headers=_HEADERS)
            if rest.status_code == 200:
                summary = rest.json().get("extract", "")
                if summary:
                    snippet = _trim(summary)
        except Exception:
            pass
        out.append(f"[{title}] {snippet}")
    return out


def _quote(title: str) -> str:
    import urllib.parse

    return urllib.parse.quote(title.replace(" ", "_"))


async def _ddg_search(client: httpx.AsyncClient, query: str) -> list[str]:
    url = "https://api.duckduckgo.com/"
    r = await client.get(url, params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}, headers=_HEADERS)
    r.raise_for_status()
    data = r.json()
    out: list[str] = []
    abstract = data.get("AbstractText")
    if abstract:
        out.append(f"[Instant Answer] {_trim(str(abstract))}")
    for topic in data.get("RelatedTopics") or []:
        if isinstance(topic, dict) and topic.get("Text"):
            out.append(f"[{topic.get('FirstURL', '相关')}] {_trim(str(topic['Text']))}")
        if len(out) >= _MAX_RESULTS:
            break
    return out


async def web_search(query: str, lang: str = "zh") -> list[str]:
    """检索维基百科 + DDG，返回文本片段列表。失败/无结果返回 []（绝不抛给调用方）。"""
    query = (query or "").strip()
    if not query:
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            results = await asyncio.gather(
                _wiki_search(client, query, lang),
                _ddg_search(client, query),
                return_exceptions=True,
            )
        out: list[str] = []
        for group in results:
            if isinstance(group, list):
                out.extend(group)
        # wiki 常对长问句匹配差：无结果时用精简关键词(去疑问词/标点)重试一次。
        if not out:
            short = _shorten_query(query)
            if short != query:
                try:
                    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client2:
                        retry = await _wiki_search(client2, short, lang)
                    out.extend(retry)
                except Exception:
                    pass
        return out[:_MAX_RESULTS]
    except Exception as exc:  # pragma: no cover - 联网失败静默
        print(f"[web_search] failed: {exc!r}", flush=True)
        return []


def _shorten_query(query: str) -> str:
    """把口语问句压成搜索关键词：去疑问词/标点/语气词，截前 12 字。"""
    import re

    q = re.sub(r"[？?。，,！!的了吗呢吧啊请问什么怎样怎么多少]", "", query)
    return q.strip()[:12]


async def web_search_text(query: str, lang: str = "zh") -> str:
    """给 LLM 用的纯文本：无结果时返回空串。"""
    hits = await web_search(query, lang)
    if not hits:
        return ""
    return "\n".join(hits)
