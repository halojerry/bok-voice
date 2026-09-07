from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Optional


class InMemoryVectorStore:
    """In-memory VectorStore for tests / no-embedding local fallback.

    2026-09-07 升级:可选 embedder（HybridLexical/Mlx）——search 改「向量余弦
    ×0.6 + 子串命中 ×0.4」混合排序;无 embedder 时保持纯子串（旧行为不变）。
    分析检索（跨通话找相似问题）用混合档;账户隔离不变。"""

    def __init__(self, embedder=None) -> None:
        self._items: dict[str, dict] = {}
        self._embedder = embedder

    def _key(self, account_id: str, text: str) -> str:
        return hashlib.sha256(f"{account_id}:{text}".encode()).hexdigest()[:16]

    async def upsert(self, items: list[dict], account_id: str) -> int:
        for item in items:
            pk = item.get("id") or self._key(account_id, item.get("text", ""))
            self._items[pk] = {**item, "account_id": account_id, "id": pk}
        return len(items)

    async def search(self, query: str, account_id: str, limit: int = 5) -> list[dict]:
        query_low = query.lower()
        qvec = None
        if self._embedder is not None:
            try:
                qvec = self._embedder.embed([query])[0]
            except Exception:
                qvec = None
        doc_vecs = None
        if qvec is not None:
            texts = [it.get("text", "") for it in self._items.values()]
            try:
                doc_vecs = self._embedder.embed(texts)
            except Exception:
                doc_vecs = None

        def _cos(a: list[float], b: list[float]) -> float:
            num = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return num / max(1e-9, na * nb)

        scored: list[tuple[float, dict]] = []
        for idx, item in enumerate(self._items.values()):
            if item.get("account_id") != account_id:
                continue
            text = item.get("text", "")
            score = 0.0
            if qvec is not None and doc_vecs is not None:
                score += 0.6 * _cos(qvec, doc_vecs[idx])
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
