"""知识库分块导入（2026-09-07 KB 复盘 P0-2）+ settle 轮次回填与空蒸馏事件。"""

from __future__ import annotations

import asyncio

from bok_voice_knowledge.knowledge import DefaultKnowledgeService, _chunk_content
from bok_voice_knowledge.markdown_source import LocalMarkdownSource
from bok_voice_knowledge.vector_store import InMemoryVectorStore


def _service(tmp_path) -> DefaultKnowledgeService:
    return DefaultKnowledgeService(
        markdown=LocalMarkdownSource(tmp_path / "vault"),
        vector=InMemoryVectorStore(),
    )


def test_chunk_content_paragraph_merge_and_hard_split():
    # 多段落合并进同一 chunk（≤500）
    doc = "\n\n".join("段" + str(i) * 50 for i in range(3))
    chunks = _chunk_content(doc, max_chars=500)
    assert len(chunks) == 1
    # 超长段落硬切
    long_para = "字" * 1200
    chunks = _chunk_content(long_para, max_chars=500)
    assert len(chunks) == 3
    assert all(len(c) <= 500 for c in chunks)


def test_chunked_import_mid_doc_keyword_searchable(tmp_path):
    """整文档单 chunk 的旧行为下,截断 150 字后后段关键词检索不到;分块后命中。"""
    service = _service(tmp_path)
    doc = "\n\n".join(
        [
            "产品支持越南语与粤语实时通话。",
            "MT3000 型号支持离线翻译与运费理赔查询。",
            "售后政策：签收后七天内可申请退换。",
        ]
    )
    asyncio.run(service.import_document("acc", "kb/doc.md", doc))
    hits = asyncio.run(service.search("退换", "acc", limit=5))
    assert any("退换" in it.get("text", "") for it in hits), "分块后尾段关键词必须可检索"
    # 确定性:同文档重导入不产生重复 chunk
    asyncio.run(service.import_document("acc", "kb/doc.md", doc))
    items = asyncio.run(service.vector.list("acc"))
    doc_items = [it for it in items if it.get("path", "").endswith("kb/doc.md")]
    assert len(doc_items) == len(_chunk_content(doc))


def test_reimport_replaces_stale_chunks(tmp_path):
    """重导入内容变更后,旧 chunk 唔得残留（防 stale 内容继续被检索）。"""
    service = _service(tmp_path)
    asyncio.run(service.import_document("acc", "kb/doc.md", "旧政策：九十天退货。"))
    asyncio.run(service.import_document("acc", "kb/doc.md", "新政策：三十天退货。"))
    hits = asyncio.run(service.search("九十天", "acc", limit=5))
    assert all("九十天" not in it.get("text", "") for it in hits), "旧政策 chunk 必须被清掉"


def test_settle_backfills_missing_turns_from_session_report(tmp_path, monkeypatch):
    """打断/强挂通话 turns 缺失 → settle 从 SessionReport.chat_history 回填。"""
    import os
    import sys

    sys.path.insert(0, str(tmp_path))
    os.environ.setdefault("DATABASE_URL", "")
    os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
    os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
    os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

    from fastapi.testclient import TestClient

    from control_plane.main import app

    with TestClient(app) as client:
        call = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "mode": "simulation", "direction": "webrtc", "language": "cantonese"},
        ).json()
        call_id = call["id"]
        # 模拟打断场景:无任何 turns 落库,直接上报 SessionReport（chat_history 快照）
        report = {
            "chat_history": {
                "items": [
                    {"type": "message", "role": "assistant", "content": ["您好，请问是林先生吗？"]},
                    {"type": "message", "role": "user", "content": ["係我。"]},
                    {"type": "message", "role": "assistant", "content": ["好的，帮您处理。"]},
                ]
            }
        }
        import json as jsonlib

        # 直接把 session_report 塞进 call（正常路径由 agent shutdown 上报）
        client.post(f"/api/calls/{call_id}/session-report", json=jsonlib.loads(jsonlib.dumps(report)))
        r = client.post(f"/api/calls/{call_id}/settle")
        assert r.status_code == 200
        turns = client.get(f"/api/calls/{call_id}/turns").json()
        assert len(turns) >= 3, f"回填后轮次应 ≥3: {len(turns)}"
        assert any(t["role"] == "user" and "係我" in t["transcript"] for t in turns)
