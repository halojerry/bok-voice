from bok_voice_core.types import CallMode

from bok_voice_business_db.repository import InMemoryBusinessRepository, SqlAlchemyBusinessRepository
from bok_voice_core.policies import select_session_manifest
from bok_voice_core.types import TurnEvent


def make_manifest(call_id="call-1", account="acc-a"):
    return select_session_manifest(
        session_id=call_id,
        account_id=account,
        object_id="obj-1",
        persona_id="p-1",
        mode=CallMode.LIVE,
    )


def test_inmemory_repo_call_and_turns_and_settlement():
    repo = InMemoryBusinessRepository()
    created = repo.create_call(make_manifest())
    assert created["id"] == "call-1"
    repo.create_turn(TurnEvent(trace_id="call-1", call_id="call-1", turn_id="t1", role="user", transcript="hello", emotion="neutral"))
    assert repo.get_turns("call-1")[0].transcript == "hello"
    assert repo.get_call("call-1")["account_id"] == "acc-a"
    assert repo.get_settlement("call-1") is None


def test_sqlalchemy_repo_roundtrip():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from bok_voice_business_db import models

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    repo = SqlAlchemyBusinessRepository(session)
    repo.create_call(make_manifest("call-2", "acc-b"))
    repo.create_turn(TurnEvent(trace_id="call-2", call_id="call-2", turn_id="t1", role="user", transcript="hi"))
    assert repo.get_call("call-2")["account_id"] == "acc-b"
    assert repo.get_turns("call-2")[0].transcript == "hi"


def test_mark_active_if_live_inmemory_semantics():
    """2026-09-14 call-c76832ac:token 端点的终态守卫原子化语义(内存替身)。
    非终态 → active;ended/failed/缺行 → 不动。"""
    repo = InMemoryBusinessRepository()
    repo.create_call(make_manifest("call-m"))
    assert repo.mark_active_if_live("call-m") is True
    assert repo.get_call("call-m")["status"] == "active"
    assert repo.mark_active_if_live("call-x") is False  # 缺行
    repo.update_call("call-m", status="ended")
    assert repo.mark_active_if_live("call-m") is False
    assert repo.get_call("call-m")["status"] == "ended"
    repo.update_call("call-m", status="failed")
    assert repo.mark_active_if_live("call-m") is False
    assert repo.get_call("call-m")["status"] == "failed"
    # paused 非终态:照旧翻 active(与旧读-写块语义等价)
    repo.update_call("call-m", status="paused")
    assert repo.mark_active_if_live("call-m") is True
    assert repo.get_call("call-m")["status"] == "active"


def test_sqlalchemy_mark_active_if_live_conditional_update():
    """SQL 路径并发现场复刻:hangup 已把 ended 落库,随后到达的 token 签发
    必须吃 DB 现状(单条条件 UPDATE,无读-写窗口),不得复活终态。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from bok_voice_business_db import models

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    repo = SqlAlchemyBusinessRepository(maker())
    repo.create_call(make_manifest("call-s"))
    assert repo.mark_active_if_live("call-s") is True

    # 另一并发请求(hangup)把 ended 落库
    other = maker()
    other.get(models.CallSession, "call-s").status = "ended"
    other.commit()
    other.close()

    # 本请求的签发:条件 UPDATE 必须按 DB 现状判定 → 不改、返回 False
    assert repo.mark_active_if_live("call-s") is False
    fresh = maker()
    assert fresh.get(models.CallSession, "call-s").status == "ended"
