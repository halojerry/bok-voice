from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def make_engine(url: str | None = None, *, in_memory: bool = False) -> Engine:
    if in_memory or (url and url.startswith("sqlite://")):
        return create_engine(
            url or "sqlite:///./data/bok_voice.db",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
    return create_engine(url or os.environ.get("DATABASE_URL", "postgresql+psycopg://bok:bok@localhost:5432/bok_voice"), future=True)


def make_session_factory(engine: Engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session(engine: Engine) -> Session:
    return make_session_factory(engine)()
