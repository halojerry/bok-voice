# KB 增量索引改造实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 知识重导入从「整篇删除重建+全量重嵌入」改为「按 chunk 内容哈希对账，只嵌入新增/变更段」；InMemory 路径的 search 不再每次全量重算向量；同文档内容未变时重导入零嵌入零写库。

**Architecture:** 三层改动：①`knowledge_chunks` 表补 `content_hash` 列（deps.py 幂等迁移+回填）；②`DefaultKnowledgeService.import_document` 改哈希集合对账（保留未变 chunk 及其 id/向量）；③`InMemoryVectorStore` 在 upsert 时缓存 chunk 向量、search 直接复用（消除每查询 O(全部 chunk) 的重嵌入）。生产 SQLite 路径（InMemory+vault 重建）与 Postgres 路径（SqlVectorStore）行为对齐。

**Tech Stack:** Python 3.12、SQLAlchemy、hashlib.sha256；测试 pytest。

## Global Constraints

- DB 迁移只写在 `apps/control-plane/control_plane/deps.py` `build_engine()` 幂等段（补列模式照 `deps.py:27-47` 既有写法：先查列存在再 `ALTER TABLE ... ADD COLUMN`，SQLite 无 `ADD COLUMN IF NOT EXISTS`）。
- 不改 `MarkdownSource` vault 文件格式与 `/api/knowledge` 端点契约。
- chunk id 保持生成规则不变（`{uuid12}:{idx}`）；未变 chunk 的 id 必须跨重导入保持稳定（这是本改造的可观测目标之一）。
- `SqlVectorStore` 的 `search` 不动（已持久化向量）；`upsert` 接受并落库 `content_hash`。
- 术语门禁：新增文件避免 `yue` 字面量（测试会扫）。

---

### Task 1: content_hash 列迁移

**Files:**
- Modify: `packages/business-db/bok_voice_business_db/vector_models.py:18-27`（KnowledgeChunk 补列声明）
- Modify: `apps/control-plane/control_plane/deps.py`（build_engine 幂等段补列+回填）

**Interfaces:**
- Produces: `KnowledgeChunk.content_hash: Mapped[str]`（String(64)，默认 `""`）；存量行回填为 `sha256(text)[:32]`。

- [ ] **Step 1: 模型补列**

```python
class KnowledgeChunk(VectorBase):
    ...
    source: Mapped[str] = mapped_column(String(64), default="import")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    embedding: Mapped[list[float]] = mapped_column(Vector(384))
```

- [ ] **Step 2: deps.py 幂等迁移**（照既有 `_has_column` 探测风格，插在 knowledge_chunks create_all 之后）

```python
        # KB 增量索引(2026-09-10): knowledge_chunks 补 content_hash + 回填存量。
        insp = sa.inspect(conn)
        if "knowledge_chunks" in insp.get_table_names() and not _has_column(insp, "knowledge_chunks", "content_hash"):
            conn.execute(sa.text("ALTER TABLE knowledge_chunks ADD COLUMN content_hash VARCHAR(64) DEFAULT ''"))
            conn.execute(sa.text(
                "UPDATE knowledge_chunks SET content_hash = substr(sha256_hex(text), 1, 32) WHERE content_hash = ''"
            ))
```

注意：SQLite 无内置 `sha256`——回填改在 Python 侧进行：`select` 全行、逐行算 `hashlib.sha256(row.text.encode()).hexdigest()[:32]` 后批量 UPDATE（知识库量级 ≤数千行，一次性成本可忽略）。Postgres 路径同一段代码走 Python 回填即可，不写方言 SQL。

- [ ] **Step 3: 验证**：临时 DB 起 `build_engine()` 两次（第二次为幂等回归），`python -m compileall -q apps packages`。

- [ ] **Step 4: Commit** — `feat(cp): knowledge_chunks 补 content_hash 列+存量回填（KB 增量索引 1/3）`

### Task 2: import_document 哈希对账

**Files:**
- Modify: `packages/knowledge/bok_voice_knowledge/knowledge.py:101-117`

**Interfaces:**
- Consumes: `vector.list()` 条目中的 `content_hash` 字段（Task 1/3 产出）。
- Produces: `import_document()` 返回值新增 `changed: bool` 与 `reindexed: int`（新增嵌入数）；未变 chunk 的 id 不变。

- [ ] **Step 1: 先写失败测试** `tests/test_knowledge_incremental.py`

```python
from __future__ import annotations

import pytest

from bok_voice_core.embeddings import CharHashEmbedding
from bok_voice_knowledge.knowledge import DefaultKnowledgeService
from bok_voice_knowledge.vector_store import InMemoryVectorStore
from bok_voice_core.providers import MarkdownSource


class FakeMarkdown(MarkdownSource):
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def write(self, path: str, content: str) -> dict:
        self.files[path] = content
        return {"path": path, "bytes": len(content)}

    def forget(self, path: str) -> None:
        self.files.pop(path, None)


class CountingEmbedder(CharHashEmbedding):
    def __init__(self, dim: int) -> None:
        super().__init__(dim)
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return super().embed(texts)


@pytest.mark.asyncio
async def test_reimport_unchanged_doc_skips_reindex():
    emb = CountingEmbedder(64)
    svc = DefaultKnowledgeService(markdown=FakeMarkdown(), vector=InMemoryVectorStore(emb))
    r1 = await svc.import_document("acc-1", "a.md", "# 标题\n\n第一段内容。\n\n第二段内容。")
    assert r1["changed"] is True
    first_calls = emb.calls
    r2 = await svc.import_document("acc-1", "a.md", "# 标题\n\n第一段内容。\n\n第二段内容。")
    assert r2["changed"] is False and r2["indexed"] == 0
    assert emb.calls == first_calls  # 零重嵌入


@pytest.mark.asyncio
async def test_reimport_changed_doc_keeps_unchunk_ids():
    emb = CountingEmbedder(64)
    svc = DefaultKnowledgeService(markdown=FakeMarkdown(), vector=InMemoryVectorStore(emb))
    await svc.import_document("acc-1", "a.md", "段落甲。\n\n段落乙。")
    before = {it["id"] for it in await svc.vector.list("acc-1")}
    await svc.import_document("acc-1", "a.md", "段落甲。\n\n段落丙。")
    after = await svc.vector.list("acc-1")
    kept = {it["id"] for it in after if it["text"] == "段落甲。"}
    assert kept and kept.issubset(before)  # 未变文本 id 稳定
    assert not any(it["text"] == "段落乙。" for it in after)
```

（`MarkdownSource`/`CharHashEmbedding` 的真实构造签名以 `packages/core/bok_voice_core/providers.py` 与 `embeddings.py` 为准——写测试前先读这两个文件对齐，若 FakeMarkdown 继承不便则改用 monkeypatch stub 同名方法。）

- [ ] **Step 2: 跑测试确认失败** `pytest tests/test_knowledge_incremental.py -v`（现状会全量重建、无 changed 字段 → FAIL）。

- [ ] **Step 3: 实现**

```python
def _chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


async def import_document(self, account_id: str, path: str, content: str) -> dict:
    safe_path = path.lstrip("/")
    doc_path = f"accounts/{account_id}/knowledge/{safe_path}"
    write_result = self.markdown.write(doc_path, content)
    # 增量对账(2026-09-10):按 chunk 文本哈希集合比对——同 path 既有 chunk 中
    # 哈希仍在集合内的原样保留(id/向量不动),其余删除;只为新哈希做嵌入。
    # 内容完全未变的重导入 = 零删除零嵌入,重启重建路径同样受益。
    new_chunks = _chunk_content(content)
    new_hashes = {_chunk_hash(c) for c in new_chunks}
    existing = await self.vector.list(account_id)
    same_path = [it for it in existing if str(it.get("path", "")) == doc_path]
    stale_ids = [
        str(it.get("id"))
        for it in same_path
        if str(it.get("content_hash") or "") not in new_hashes
    ]
    if same_path and not stale_ids and len(same_path) == len(new_chunks):
        return {**write_result, "indexed": 0, "reindexed": 0, "changed": False}
    if stale_ids:
        await self.vector.delete(account_id, stale_ids)
    kept_hashes = {
        str(it.get("content_hash") or "")
        for it in same_path
        if str(it.get("content_hash") or "") in new_hashes
    }
    chunks = [
        {"id": f"{_aid()}:{i}", "text": c, "path": doc_path, "source": "import",
         "content_hash": _chunk_hash(c)}
        for i, c in enumerate(new_chunks)
        if _chunk_hash(c) not in kept_hashes
    ]
    count = await self.vector.upsert(chunks, account_id) if chunks else 0
    return {**write_result, "indexed": count, "reindexed": len(chunks), "changed": True}
```

（`knowledge.py` 顶部补 `import hashlib`。）

- [ ] **Step 4: 跑测试通过** + 既有 KB 相关测试全量 `pytest tests/ -k "knowledge or vector" -q`。

- [ ] **Step 5: Commit** — `feat(kb): 重导入按 chunk 哈希对账,未变内容零重嵌入（KB 增量索引 2/3）`

### Task 3: 两个 store 持久化 content_hash + InMemory 向量缓存

**Files:**
- Modify: `packages/knowledge/bok_voice_knowledge/vector_store.py`（InMemoryVectorStore）
- Modify: `packages/business-db/bok_voice_business_db/vector_store.py:36-50`（SqlVectorStore.upsert）

**Interfaces:**
- Produces: 两 store 的 `upsert()` 原样接受并落库 item 里可选的 `content_hash`；`list()` 返回条目含 `content_hash`；InMemory search 复用 upsert 时缓存的向量。

- [ ] **Step 1: InMemoryVectorStore 改造**

```python
class InMemoryVectorStore:
    def __init__(self, embedder=None) -> None:
        self._items: dict[str, dict] = {}
        self._vecs: dict[str, list[float]] = {}  # pk -> 向量缓存
        self._embedder = embedder

    async def upsert(self, items: list[dict], account_id: str) -> int:
        texts = [it.get("text", "") for it in items]
        vecs = None
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

    async def search(self, query, account_id, limit=5):
        # ...同现役混合打分,但 doc 向量改查 self._vecs,不再对全量 text 重嵌入;
        # 缓存缺失的条目(旧数据/嵌入失败)维持现行为兜底重算。
```

`delete()` 同步清 `self._vecs`。

- [ ] **Step 2: SqlVectorStore.upsert 落 hash**

```python
                vector_models.KnowledgeChunk(
                    ...,
                    source=item.get("source", "import"),
                    content_hash=item.get("content_hash", ""),
                    embedding=vec,
                )
```

`_to_dict` 补 `"content_hash": row.content_hash`；InMemory upsert 已把 item 原样并入 dict，天然透出。

- [ ] **Step 3: 测试通过 + 全量回归** `pytest tests/test_knowledge_incremental.py tests/ -k "knowledge" -q`；`python -m compileall -q apps packages`。

- [ ] **Step 4: Commit** — `perf(kb): InMemory 向量缓存+两 store 落 content_hash（KB 增量索引 3/3）`

### Task 4: 文档与收尾

- [ ] **Step 1:** `docs/REPO_MAP.md` 若有 KB 数据流描述则同步；FINDINGS 记录前后对比（重导入同文档 embed 次数 2N→0）。
- [ ] **Step 2:** PR 描述附测试证据（pytest 输出）。

## Phase 2（后续单独立计划，不在本计划实施）

- SQLite 路径持久化向量索引（自建 `knowledge_chunk_vectors` 表存 JSON/bytes 向量，摆脱 pgvector 依赖）→ 消灭重启 vault 全量重建；
- ChildChunk 父子分块 + 检索漏斗（粗排 1024→rerank→top_n，参数基线抄 RAGFlow 0.7/0.3、阈值 0.2）。
