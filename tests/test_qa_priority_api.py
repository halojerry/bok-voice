"""priority API:透传 / 钳制 [0,1000] / 缺省 10(spec 2026-09-18 flow-graph-phase3 §1)。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-prio-api")


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    return TestClient(app).__enter__(), repo


def test_create_clamps_and_defaults(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    ok = client.post("/api/qa-entries", json={"question_text": "q", "answer_text": "a"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["priority"] == 10
    hi = client.post("/api/qa-entries", json={"question_text": "q2", "answer_text": "a", "priority": 9999})
    assert hi.status_code == 200 and hi.json()["priority"] == 1000
    lo = client.post("/api/qa-entries", json={"question_text": "q3", "answer_text": "a", "priority": -5})
    assert lo.status_code == 200 and lo.json()["priority"] == 0
    # 0 是合法值不钳成 10
    zero = client.post("/api/qa-entries", json={"question_text": "q4", "answer_text": "a", "priority": 0})
    assert zero.json()["priority"] == 0
    pid = ok.json()["id"]
    patch = client.patch(f"/api/qa-entries/{pid}", json={"priority": 1})
    assert patch.status_code == 200 and patch.json()["priority"] == 1
    # patch 越界也钳
    patch2 = client.patch(f"/api/qa-entries/{pid}", json={"priority": 2000})
    assert patch2.json()["priority"] == 1000
