import asyncio

from bok_voice_knowledge.knowledge import DefaultKnowledgeService
from bok_voice_knowledge.markdown_source import LocalMarkdownSource
from bok_voice_knowledge.vector_store import InMemoryVectorStore


def test_import_and_search_account_isolated(tmp_path):
    vault = tmp_path / "vault"
    service = DefaultKnowledgeService(
        markdown=LocalMarkdownSource(vault),
        vector=InMemoryVectorStore(),
    )
    asyncio.run(
        service.import_document(
            "acc-a",
            "product.md",
            "我们的产品支持越南语与粤语实时通话。",
        )
    )
    hits_a = asyncio.run(service.search("越南语", "acc-a"))
    hits_b = asyncio.run(service.search("越南语", "acc-b"))
    assert hits_a and hits_a[0]["account_id"] == "acc-a"
    assert hits_b == []
    assert (vault / "accounts" / "acc-a" / "knowledge" / "product.md").exists()
