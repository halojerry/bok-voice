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

    async def list(self, account_id: str) -> list[dict]:
        return await self.vector.list(account_id)

    async def delete(self, account_id: str, ids: list[str]) -> int:
        # 删除必须同时移除 vault 里的源文件，否则 SQLite/内存路径在进程重启时会
        # 从 vault 重建索引，导致「已删除的知识复活」。只删 accounts/{acct}/knowledge/
        # 前缀的文档；结算/转写文档（objects/.../calls）不属于知识库，不受影响。
        all_items = await self.vector.list(account_id)
        by_id = {str(it.get("id")): it for it in all_items}
        targets: dict[str, dict] = {}
        for kid in ids:
            item = by_id.get(kid)
            if item is None:
                continue
            targets[kid] = item
            # 同一文档可能存在两种 id（导入时的随机 uuid / 重启重建的 md:rel），
            # 按 path 一并找出，确保两边都清掉。
            for other in all_items:
                if other is not item and str(other.get("path", "")) == str(item.get("path", "")):
                    targets[str(other.get("id"))] = other
        prefix = f"accounts/{account_id}/knowledge/"
        for item in targets.values():
            path = str(item.get("path") or "")
            if path.startswith(prefix):
                try:
                    self.markdown.forget(path)
                except Exception:  # pragma: no cover - 文件不存在视为已删
                    pass
        if not targets:
            return 0
        return await self.vector.delete(account_id, list(targets.keys()))

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
