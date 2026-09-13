"""turns 分析账本（spec 2026-09-10 §6.1）：org_id/line/speaker/gen/template_step/
时间轴/perceived_ms 落库 + 旧调用零破坏（缺省值兜底）+ 幂等迁移补列。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # in-memory repo
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient

from control_plane.main import app

NEW_FIELDS = {
    "org_id": "org-demo",
    "line": "b",
    "speaker": "customer",
    "gen": "llm",
    "template_step": 2,
    "started_ms": 1500,
    "ended_ms": 4200,
    "perceived_ms": 1820,
}


def _make_call(client: TestClient) -> str:
    r = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "mode": "simulation", "direction": "webrtc", "language": "cantonese"},
    )
    assert r.status_code in (200, 201)
    return r.json()["id"]


def test_new_ledger_fields_persisted():
    with TestClient(app) as client:
        call_id = _make_call(client)
        r = client.post(
            f"/api/calls/{call_id}/turns",
            params={"role": "user", "transcript": "我個單號係三七七八九零", **NEW_FIELDS},
        )
        assert r.status_code in (200, 201)
        t = client.get(f"/api/calls/{call_id}/turns").json()[-1]
        for key, value in NEW_FIELDS.items():
            assert t[key] == value, f"{key} 未落库: {t}"


def test_old_callers_get_defaults():
    with TestClient(app) as client:
        call_id = _make_call(client)
        r = client.post(
            f"/api/calls/{call_id}/turns",
            params={"role": "assistant", "transcript": "你好"},
        )
        assert r.status_code in (200, 201)
        t = client.get(f"/api/calls/{call_id}/turns").json()[-1]
        assert t["org_id"] == "" and t["line"] == "a" and t["speaker"] == ""
        assert t["perceived_ms"] == 0


def test_migration_adds_ledger_columns(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    from control_plane.deps import build_engine

    build_engine()
    build_engine()  # 幂等：二启不报错
    import sqlite3

    cols = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(turns)")}
    assert {"org_id", "line", "speaker", "gen", "template_step", "started_ms", "ended_ms", "perceived_ms"} <= cols


def test_sql_mode_roundtrip_full_ledger(tmp_path, monkeypatch):
    """SQL 分支全链回归（终审 Important#1）：POST → sqlite 行 → SQL get_turns →
    TurnEvent → HTTP 序列化,8 个新列逐一精确回读;旧调用缺省值在 SQL 路径同样兜底。
    建模自 test_nodes_registry 的 SQL 模式姿势（真实 engine,非内存 repo）。"""
    db = tmp_path / "ledger-sql.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    with TestClient(app) as client:
        call_id = _make_call(client)
        r = client.post(
            f"/api/calls/{call_id}/turns",
            params={"role": "user", "transcript": "sql roundtrip", "org_id": "org-sql",
                    "line": "b", "speaker": "agent_human", "gen": "qa_fastpath",
                    "template_step": 3, "started_ms": 100, "ended_ms": 999, "perceived_ms": 1500},
        )
        assert r.status_code in (200, 201)
        r2 = client.post(
            f"/api/calls/{call_id}/turns",
            params={"role": "assistant", "transcript": "你好"},  # 旧调用方形状
        )
        assert r2.status_code in (200, 201)
        turns = client.get(f"/api/calls/{call_id}/turns").json()
    full = next(t for t in turns if t["role"] == "user")
    legacy = next(t for t in turns if t["role"] == "assistant")
    assert (full["org_id"], full["line"], full["speaker"], full["gen"]) == \
        ("org-sql", "b", "agent_human", "qa_fastpath"), full
    assert (full["template_step"], full["started_ms"], full["ended_ms"], full["perceived_ms"]) == \
        (3, 100, 999, 1500), full
    assert legacy["org_id"] == "" and legacy["line"] == "a" and legacy["speaker"] == "", legacy
    assert legacy["perceived_ms"] == 0 and legacy["template_step"] == 0, legacy
