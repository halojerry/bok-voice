from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from bok_voice_core.providers import EmbeddingService

from . import vector_models


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class SqlVectorStore:
    """Postgres/pgvector-backed VectorStore.

    Keeps the durable ``knowledge_chunks`` table as the persistent vector index.
    Recall is deterministic (account-scoped ILIKE) while ranking uses the stored
    vector cosine; production can rely on vector-only ordering once a real embedder
    (BGE/ONNX) is configured.
    """

    def __init__(self, session: Session, embedder: EmbeddingService, dim: int = 384):
        self.session = session
        self.embedder = embedder
        self.dim = dim

    async def upsert(self, items: list[dict], account_id: str) -> int:
        vecs = self.embedder.embed([item.get("text", "") for item in items])
        for item, vec in zip(items, vecs):
            self.session.add(
                vector_models.KnowledgeChunk(
                    id=item.get("id") or uuid.uuid4().hex[:12],
                    account_id=account_id,
                    text=item.get("text", ""),
                    path=item.get("path", ""),
                    source=item.get("source", "import"),
                    embedding=vec,
                )
            )
        self.session.commit()
        return len(items)

    async def search(self, query: str, account_id: str, limit: int = 5) -> list[dict]:
        rows = self.session.scalars(
            select(vector_models.KnowledgeChunk).filter_by(account_id=account_id)
        ).all()
        qvec = self.embedder.embed([query])[0]

        scored: list[tuple[int, float, vector_models.KnowledgeChunk]] = []
        for row in rows:
            ilike = query.lower() in row.text.lower()
            try:
                vec = list(row.embedding)
            except TypeError:
                vec = []
            scored.append((0 if ilike else 1, _cosine(vec, qvec), row))

        scored.sort(key=lambda x: (x[0], -x[1]))
        return [self._to_dict(r) for _, _, r in scored[:limit]]

    async def delete(self, account_id: str, ids: list[str]) -> int:
        removed = 0
        for pk in ids:
            row = self.session.get(vector_models.KnowledgeChunk, pk)
            if row and row.account_id == account_id:
                self.session.delete(row)
                removed += 1
        self.session.commit()
        return removed

    async def list(self, account_id: str) -> list[dict]:
        rows = self.session.scalars(
            select(vector_models.KnowledgeChunk).filter_by(account_id=account_id)
        ).all()
        return [self._to_dict(r) for r in rows]

    @staticmethod
    def _to_dict(row: vector_models.KnowledgeChunk) -> dict:
        return {
            "id": row.id,
            "account_id": row.account_id,
            "text": row.text,
            "path": row.path,
            "source": row.source,
        }
