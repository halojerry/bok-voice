"""graph_json 数据层:迁移补列 / create+update 透传 / 默认空串(spec §5)。"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-owner")

_DOC = '{"version":1,"intents":[{"id":"int_1a2b3c4d","label":"投诉","keywords":["投诉"],"steps":[],"enabled":true}],"bindings":[]}'


def _sql_repo(tmpdir: str):
    # env 复原:本夹具写的 DATABASE_URL 不得泄漏给同进程后续用例——全量跑时
    # CP 的 TestClient 用例(build_engine 读 env)会误连本用例的临时库,
    # usage 聚合读到跨用例脏行(test_wave_b_llm_asr_usage 实测 1250→1257)。
    # engine 建好后 repo 自带 session,DATABASE_URL 复原不影响断言。
    prev_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{tmpdir}/graph.db"
    try:
        from sqlalchemy.orm import sessionmaker

        from control_plane import deps
        from bok_voice_business_db.repository import SqlAlchemyBusinessRepository

        engine = deps.build_engine()
        assert engine is not None
        assert deps.build_engine() is not None  # 二跑不炸=幂等
        repo = SqlAlchemyBusinessRepository(sessionmaker(bind=engine, expire_on_commit=False)())
        repo.engine = engine  # type: ignore[attr-defined]
        return repo
    finally:
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url


def test_migration_adds_graph_json(tmp_path):
    repo = _sql_repo(str(tmp_path))
    import sqlalchemy as sa

    with repo.engine.connect() as conn:  # type: ignore[attr-defined]
        cols = {c["name"] for c in sa.inspect(conn).get_columns("conversation_templates")}
    assert "graph_json" in cols


def test_create_update_roundtrip(tmp_path):
    repo = _sql_repo(str(tmp_path))
    tpl = repo.create_template({"account_id": "acc-001", "name": "t", "graph_json": _DOC})
    assert repo.get_template(tpl["id"])["graph_json"] == _DOC
    # update 白名单放行;空串=清空
    repo.update_template(tpl["id"], {"graph_json": ""})
    assert repo.get_template(tpl["id"])["graph_json"] == ""
    repo.update_template(tpl["id"], {"graph_json": _DOC})
    assert repo.get_template(tpl["id"])["graph_json"] == _DOC


def test_default_empty_and_inmemory_parity():
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    tpl = repo.create_template({"account_id": "acc-001", "name": "m"})
    assert tpl["graph_json"] == ""
    repo.update_template(tpl["id"], {"graph_json": _DOC})
    assert repo.get_template(tpl["id"])["graph_json"] == _DOC
