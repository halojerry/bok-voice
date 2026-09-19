"""罐头状态面:spawn 失败降级 available=False / 状态透传 / 缓存音频回放 WAV。"""
from __future__ import annotations

import io
import os
import wave

os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("BOK_JWT_SECRET", "test-secret-for-canned")

from agent_runtime.tts_cache import TtsAudioCache  # noqa: E402

import pytest  # noqa: E402
from control_plane import pregen as pregen_mod  # noqa: E402

FIXTURE = {
    "qa_status": {"qa:x": {"state": "ok", "voice": "v1", "key": "a" * 40}},
}


@pytest.fixture(autouse=True)
def _reset_status_cache():
    """模块级 TTL 缓存跨测试隔离:不重置则上一条测试的缓存结果污染下一条
    (passthrough 的 calls 计数 / wav 回放的 statuses 查找),与端点语义无关。"""
    pregen_mod._status_cache = (0.0, None)
    yield
    pregen_mod._status_cache = (0.0, None)


def _client_and_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from control_plane.main import app
    from bok_voice_business_db.repository import InMemoryBusinessRepository

    repo = InMemoryBusinessRepository()
    monkeypatch.setattr("control_plane.main._repo", lambda: repo)
    client = TestClient(app).__enter__()
    return client, repo


def test_canned_status_spawn_failure_degrades(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)

    def boom(base_url: str) -> dict:
        raise RuntimeError("spawn fail")

    monkeypatch.setattr("control_plane.pregen.qa_status_json", boom)
    r = client.get("/api/qa/canned-status")
    assert r.status_code == 200 and r.json()["available"] is False


def test_canned_status_passthrough_and_ttl(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    calls = {"n": 0}

    def fake(base_url: str) -> dict:
        calls["n"] += 1
        return FIXTURE

    monkeypatch.setattr("control_plane.pregen.qa_status_json", fake)
    monkeypatch.setattr("control_plane.pregen._STATUS_TTL_S", 60.0, raising=False)
    assert client.get("/api/qa/canned-status").json()["statuses"] == FIXTURE["qa_status"]
    assert client.get("/api/qa/canned-status").json()["statuses"] == FIXTURE["qa_status"]
    assert calls["n"] == 1  # TTL 内只 spawn 一次


def test_canned_audio_wav_replay(monkeypatch, tmp_path):
    client, repo = _client_and_repo(monkeypatch)
    entry = repo.create_qa_entry({"question_text": "q", "answer_text": "您好", "account_id": "acc-001"})
    cache = TtsAudioCache(tmp_path)
    cache.store("b" * 40, b"\x01\x02" * 240, text="您好", voice="v", model="m", pin=True)
    monkeypatch.setattr(
        "control_plane.pregen.qa_status_json",
        lambda base_url: {"qa_status": {entry["id"]: {"state": "ok", "voice": "v", "key": "b" * 40}}},
    )
    monkeypatch.setattr("control_plane.pregen.cache_root", lambda: tmp_path)
    r = client.get(f"/api/qa/{entry['id']}/canned-audio")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1


def test_pregen_trigger_single_flight(monkeypatch):
    client, repo = _client_and_repo(monkeypatch)
    seen: dict = {}

    def fake_spawn(base_url: str, entry_ids: list[str]) -> dict:
        seen["ids"] = entry_ids
        return {"status": "queued"}

    monkeypatch.setattr("control_plane.pregen.qa_pregen_spawn", fake_spawn)
    r = client.post("/api/qa/pregen", json={"ids": ["qa:1"]})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert seen["ids"] == ["qa:1"]
