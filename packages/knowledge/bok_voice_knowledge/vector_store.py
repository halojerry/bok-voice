from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Optional


class InMemoryVectorStore:
    """In-memory VectorStore for tests / no-embedding local fallback."""

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    def _key(self, account_id: str, text: str) -> str:
        return hashlib.sha256(f"{account_id}:{text}".encode()).hexdigest()[:16]

    async def upsert(self, items: list[dict], account_id: str) -> int:
        for item in items:
            pk = item.get("id") or self._key(account_id, item.get("text", ""))
            self._items[pk] = {**item, "account_id": account_id, "id": pk}
        return len(items)

    async def search(self, query: str, account_id: str, limit: int = 5) -> list[dict]:
        query_low = query.lower()
        scored: list[tuple[int, dict]] = []
        for item in self._items.values():
            if item.get("account_id") != account_id:
                continue
            text = item.get("text", "").lower()
            if query_low in text:
                scored.append((len(query_low) / max(1, len(text)), item))
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
