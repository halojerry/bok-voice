"""审计闭环（2026-09-07）：turns 落库补全 + 轮级延迟档案 + metrics 端点。

此前：CP 把 agent 发来的 provider/latency_ms 静默丢弃（恒 0）、GET turns 不出
created_at、无每通延迟查询面——审计面「日志 stdout 一把抓、库里无档案」。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")  # in-memory repo
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from fastapi.testclient import TestClient

from control_plane.main import app


def _make_call(client: TestClient) -> str:
    r = client.post(
        "/api/calls",
        json={"account_id": "acc-001", "mode": "simulation", "direction": "webrtc", "language": "cantonese"},
    )
    assert r.status_code in (200, 201)
    return r.json()["id"]


def test_turn_fields_persisted_and_returned():
    with TestClient(app) as client:
        call_id = _make_call(client)
        r = client.post(
            f"/api/calls/{call_id}/turns",
            params={"role": "assistant", "transcript": "你好", "provider": "minimax",
                    "latency_ms": 1234, "language": "cantonese"},
        )
        assert r.status_code in (200, 201)
        turns = client.get(f"/api/calls/{call_id}/turns").json()
        t = turns[-1]
        assert t["provider"] == "minimax", f"provider 被丢弃: {t}"
        assert t["latency_ms"] == 1234, f"latency_ms 被丢弃: {t}"
        assert t["language"] == "cantonese"
        assert t.get("created_at"), "created_at 未返回"


def test_call_metrics_endpoint_aggregates_latency():
    with TestClient(app) as client:
        call_id = _make_call(client)
        for ms in (100, 200, 1000, 2000):
            client.post(
                f"/api/calls/{call_id}/turns",
                params={"role": "assistant", "transcript": "x", "latency_ms": ms},
            )
        m = client.get(f"/api/calls/{call_id}/metrics").json()
        assert m["call_id"] == call_id
        assert m["turns"] == 4
        assert m["latency_ms"]["max"] == 2000
        assert m["latency_ms"]["p50"] == 200
        assert m["latency_ms"]["p95"] == 2000
        assert m["latency_ms"]["n"] == 4


def test_turn_language_migration_column_exists(tmp_path, monkeypatch):
    """旧库迁移补 turns.language 列。"""
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE call_sessions (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64), object_id VARCHAR(64), persona_id VARCHAR(64), mode VARCHAR(32), status VARCHAR(32));"
        "CREATE TABLE object_profiles (id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64), display_name VARCHAR(255));"
        "CREATE TABLE settlements (id VARCHAR(64) PRIMARY KEY, call_id VARCHAR(64));"
    )
    conn.close()
    from control_plane.deps import build_engine

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    build_engine()
    conn = sqlite3.connect(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(turns)")}
    conn.close()
    assert "language" in cols, f"turns.language 列缺失: {cols}"


def test_bok_log_rotation(tmp_path):
    """stdout 日志 >50MB 归档 .1，小文件不动。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bok", Path(__file__).resolve().parents[1] / "tools" / "bok.py"
    )
    bok = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bok)

    small = tmp_path / "small.log"
    small.write_bytes(b"x" * 100)
    bok._rotate_log(small)
    assert small.read_bytes() == b"x" * 100  # 小文件不动

    big = tmp_path / "big.log"
    big.write_bytes(b"y" * (51 * 1024 * 1024))
    bok._rotate_log(big)
    assert big.stat().st_size == 0
    assert (tmp_path / "big.log.1").stat().st_size == 51 * 1024 * 1024
