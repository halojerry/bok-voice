"""priority 数据层:迁移补列 / create+update 透传 / 默认 10(spec 2026-09-18 flow-graph-phase3 §1)。"""
from __future__ import annotations

import os

os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-priority")


def test_migration_adds_priority(tmp_path):
    saved = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/prio.db"
        from sqlalchemy.orm import sessionmaker

        from control_plane import deps
        from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

        engine = deps.build_engine()
        assert deps.build_engine() is not None  # 幂等二跑
        repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)())
        import sqlalchemy as sa

        with engine.connect() as conn:
            cols = {c["name"] for c in sa.inspect(conn).get_columns("qa_entries")}
        assert "priority" in cols
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved


def test_create_update_roundtrip_and_default():
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    e = repo.create_qa_entry({"account_id": "acc-001", "question_text": "q", "answer_text": "a"})
    assert e["priority"] == 10  # 默认
    repo.update_qa_entry(e["id"], {"priority": 1})
    assert repo.get_qa_entry(e["id"])["priority"] == 1
