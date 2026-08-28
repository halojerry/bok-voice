from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from bok_voice_core.context import DefaultContextAssembler
from bok_voice_core.providers import MarkdownSource, VectorStore
from bok_voice_core.types import ContextBundle, TurnEvent


def _aid() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class DefaultKnowledgeService:
    """KnowledgeService that composes MarkdownSource + VectorStore."""

    markdown: MarkdownSource
    vector: VectorStore
    assembler: DefaultContextAssembler = field(default_factory=DefaultContextAssembler)

    async def search(self, query: str, account_id: str, limit: int = 5) -> list[dict]:
        return await self.vector.search(query, account_id, limit)

    async def context(self, task: str, account_id: str, limit: int = 5) -> ContextBundle:
        snippets = await self.vector.search(task, account_id, limit)
        return self.assembler.assemble(
            product_snippets=snippets,
            history_snippets=snippets,
            current_turns=[],
        )

    def observe(self, turn: TurnEvent) -> dict:
        # MVP: no-op sink; production writes to Bok observe + personal signals.
        return {"observed": True, "turn_id": turn.turn_id}

    async def import_document(self, account_id: str, path: str, content: str) -> dict:
        safe_path = path.lstrip("/")
        doc_path = f"accounts/{account_id}/knowledge/{safe_path}"
        write_result = self.markdown.write(doc_path, content)
        chunk = {"id": _aid(), "text": content, "path": doc_path, "source": "import"}
        count = await self.vector.upsert([chunk], account_id)
        return {**write_result, "indexed": count}
