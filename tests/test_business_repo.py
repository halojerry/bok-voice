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
