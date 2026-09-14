from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Optional


class InMemoryVectorStore:
    """In-memory VectorStore for tests / no-embedding local fallback.

    2026-09-07 升级:可选 embedder（HybridLexical/Mlx）——search 改「向量余弦
    ×0.6 + 子串命中 ×0.4」混合排序;无 embedder 时保持纯子串（旧行为不变）。
    2026-09-07 分析检索（跨通话找相似问题）用混合档;账户隔离不变。
    2026-09-10 增量索引:文档向量在 upsert 时计算并按 pk 缓存,search 只嵌入
    查询文本（缓存缺失的条目兜底补算一次）——消除每查询 O(全部 chunk) 重嵌入。
    """

    def __init__(self, embedder=None) -> None:
        self._items: dict[str, dict] = {}
        self._vecs: dict[str, list[float]] = {}
        self._embedder = embedder

    def _key(self, account_id: str, text: str) -> str:
        return hashlib.sha256(f"{account_id}:{text}".encode()).hexdigest()[:16]

    async def upsert(self, items: list[dict], account_id: str) -> int:
        texts = [it.get("text", "") for it in items]
        vecs: list[list[float]] | None = None
        if self._embedder is not None and texts:
            try:
                vecs = self._embedder.embed(texts)
            except Exception:
                vecs = None
        for idx, item in enumerate(items):
            pk = item.get("id") or self._key(account_id, item.get("text", ""))
            self._items[pk] = {**item, "account_id": account_id, "id": pk}
            if vecs is not None:
                self._vecs[pk] = vecs[idx]
            else:
                self._vecs.pop(pk, None)
        return len(items)

    async def search(self, query: str, account_id: str, limit: int = 5) -> list[dict]:
        query_low = query.lower()
        qvec = None
        if self._embedder is not None:
            try:
                qvec = self._embedder.embed([query])[0]
            except Exception:
                qvec = None

        items = [it for it in self._items.values() if it.get("account_id") == account_id]
        doc_vecs: list[list[float] | None] = []
        if qvec is not None:
            # 缓存缺失(旧数据/嵌入失败)的条目兜底补算一次并回填
            missing = [str(it.get("id")) for it in items if str(it.get("id")) not in self._vecs]
            if missing:
                try:
                    backfill = self._embedder.embed(
                        [self._items[pk].get("text", "") for pk in missing]
                    )
                    for pk, vec in zip(missing, backfill):
                        self._vecs[pk] = vec
                except Exception:
                    pass
            doc_vecs = [self._vecs.get(str(it.get("id"))) for it in items]

        def _cos(a: list[float], b: list[float]) -> float:
            num = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return num / max(1e-9, na * nb)

        scored: list[tuple[float, dict]] = []
        for idx, item in enumerate(items):
            text = item.get("text", "")
            score = 0.0
            vec = doc_vecs[idx] if idx < len(doc_vecs) else None
            if qvec is not None and vec is not None:
                score += 0.6 * _cos(qvec, vec)
            if query_low in text.lower():
                score += 0.4 * (len(query_low) / max(1, len(text)))
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:limit]]

    async def delete(self, account_id: str, ids: list[str]) -> int:
        removed = 0
        for pk in ids:
            if self._items.get(pk, {}).get("account_id") == account_id:
                self._items.pop(pk, None)
                self._vecs.pop(pk, None)
                removed += 1
        return removed

    async def list(self, account_id: str) -> list[dict]:
        return [item for item in self._items.values() if item.get("account_id") == account_id]


class SqlVectorStore:
    """Postgres/pgvector implementation placeholder.

    Intended to be implemented against a `vector` column; for now the interface
    allows swapping without touching KnowledgeService.
    """

    async def upsert(self, items: list[dict], account_id: str) -> int:
        raise NotImplementedError("pgvector upsert not wired in this skeleton")

    async def search(self, query: str, account_id: str, limit: int = 5) -> list[dict]:
        raise NotImplementedError("pgvector search not wired in this skeleton")

    async def delete(self, account_id: str, ids: list[str]) -> int:
        raise NotImplementedError("pgvector delete not wired in this skeleton")

    async def list(self, account_id: str) -> list[dict]:
        raise NotImplementedError("pgvector list not wired in this skeleton")
