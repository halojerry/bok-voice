"""KB 增量索引行为测试(2026-09-10):重导入按 chunk 哈希对账。

- 内容未变的重导入 = 零删除零嵌入(changed=False)
- 内容变更 = 只嵌入新增文本,未变 chunk 的 id 跨重导入保持稳定
"""
from __future__ import annotations

import asyncio

from bok_voice_core.embeddings import CharHashEmbedding
from bok_voice_knowledge.knowledge import DefaultKnowledgeService
from bok_voice_knowledge.vector_store import InMemoryVectorStore


class FakeMarkdown:
    """MarkdownSource 结构化替身:只记文件,不落盘。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def write(self, path: str, content: str) -> dict:
        self.files[path] = content
        return {"path": path, "bytes": len(content)}

    def forget(self, path: str) -> dict:
        return {"path": self.files.pop(path, "")}


class CountingEmbedder(CharHashEmbedding):
    def __init__(self, dim: int = 64) -> None:
        super().__init__(dim)
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return super().embed(texts)


def _service(emb: CountingEmbedder) -> DefaultKnowledgeService:
    return DefaultKnowledgeService(markdown=FakeMarkdown(), vector=InMemoryVectorStore(emb))


def test_reimport_unchanged_doc_skips_reindex() -> None:
    emb = CountingEmbedder()
    svc = _service(emb)
    content = "# 标题\n\n第一段内容。\n\n第二段内容。"
    r1 = asyncio.run(svc.import_document("acc-1", "a.md", content))
    assert r1["changed"] is True and r1["indexed"] > 0
    first_calls = emb.calls

    r2 = asyncio.run(svc.import_document("acc-1", "a.md", content))
    assert r2["changed"] is False
    assert r2["indexed"] == 0
    assert emb.calls == first_calls  # 零重嵌入


def test_reimport_changed_doc_keeps_unchanged_ids() -> None:
    emb = CountingEmbedder()
    svc = _service(emb)
    # 每段 300 字符:两段无法被 _chunk_content 合并(>500 上限),才能形成独立 chunk
    para_a = "段落甲。" + "甲" * 292
    para_b = "段落乙。" + "乙" * 292
    para_c = "段落丙。" + "丙" * 292
    asyncio.run(svc.import_document("acc-1", "a.md", f"{para_a}\n\n{para_b}"))
    before = {it["id"] for it in asyncio.run(svc.vector.list("acc-1"))}
    assert len(before) == 2  # 前置:确实是两个独立 chunk

    asyncio.run(svc.import_document("acc-1", "a.md", f"{para_a}\n\n{para_c}"))
    after = asyncio.run(svc.vector.list("acc-1"))
    texts = {it["text"] for it in after}
    kept = {it["id"] for it in after if para_a in it["text"]}

    assert kept and kept.issubset(before)  # 未变文本 id 稳定
    assert not any(para_b in t for t in texts)  # 陈旧 chunk 被清
    assert any(para_c in t for t in texts)  # 新内容已入索引


def test_reimport_chunks_gain_content_hash() -> None:
    emb = CountingEmbedder()
    svc = _service(emb)
    asyncio.run(svc.import_document("acc-1", "a.md", "唯一段落。"))
    items = asyncio.run(svc.vector.list("acc-1"))
    assert items and all(it.get("content_hash") for it in items)
