"""cluster_head_id 数据层：迁移补列 / patch 透传 / 删除级联清引用（spec §4.1）。"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-owner")


def _sql_repo(tmpdir: str):
    """tmp sqlite + build_engine 迁移后建 SQL 仓库（幂等二跑=幂等性回归）。

    注：仓库构造契约是 Session（test_owner_scope.py/test_filler_canned.py 同款
    sessionmaker 姿势），brief 测试骨架写的 Engine 传入与 .engine 属性在此适配——
    引擎挂到 repo.engine 仅供迁移列断言，断言体保持 brief 原样。
    """
    os.environ["DATABASE_URL"] = f"sqlite:///{tmpdir}/qa.db"
    from sqlalchemy.orm import sessionmaker

    from control_plane import deps
    from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

    engine = deps.build_engine()
    assert engine is not None
    assert deps.build_engine() is not None  # 二跑不炸=幂等
    repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)())
    repo.engine = engine  # type: ignore[attr-defined]  # 迁移断言用
    return repo


def test_migration_adds_cluster_head_id(tmp_path):
    repo = _sql_repo(str(tmp_path))
    import sqlalchemy as sa

    with repo.engine.connect() as conn:  # type: ignore[attr-defined]
        cols = {c["name"] for c in sa.inspect(conn).get_columns("qa_entries")}
    assert "cluster_head_id" in cols


def test_create_patch_dict_roundtrip(tmp_path):
    repo = _sql_repo(str(tmp_path))
    head = repo.create_qa_entry({"question_text": "q1", "answer_text": "a", "account_id": "acc-001"})
    row = repo.create_qa_entry(
        {"question_text": "q2", "answer_text": "a", "account_id": "acc-001",
         "cluster_head_id": head["id"]}
    )
    assert row["cluster_head_id"] == head["id"]
    assert repo.list_qa_entries("acc-001")[0]["cluster_head_id"] == ""  # dict 序列化含键
    patched = repo.update_qa_entry(row["id"], {"cluster_head_id": ""})  # 断簇=写空串
    assert patched is not None and patched["cluster_head_id"] == ""


def test_delete_head_cascades_children(tmp_path):
    repo = _sql_repo(str(tmp_path))
    head = repo.create_qa_entry({"question_text": "h", "answer_text": "a", "account_id": "acc-001"})
    child = repo.create_qa_entry(
        {"question_text": "c", "answer_text": "a", "account_id": "acc-001",
         "cluster_head_id": head["id"]}
    )
    assert repo.delete_qa_entry(head["id"]) is True
    assert repo.get_qa_entry(child["id"])["cluster_head_id"] == ""


def test_in_memory_repo_parity(monkeypatch):
    os.environ["DATABASE_URL"] = ""  # 强制内存仓库
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    head = repo.create_qa_entry({"question_text": "h", "answer_text": "a", "account_id": "acc-001"})
    child = repo.create_qa_entry(
        {"question_text": "c", "answer_text": "a", "account_id": "acc-001",
         "cluster_head_id": head["id"]}
    )
    assert child["cluster_head_id"] == head["id"]
    assert repo.delete_qa_entry(head["id"]) is True
    assert repo.get_qa_entry(child["id"])["cluster_head_id"] == ""
