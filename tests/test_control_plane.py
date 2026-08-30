import os

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")

from fastapi.testclient import TestClient

from control_plane.main import app


def test_control_plane_flow():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True, "service": "bok-voice-control-plane"}
        token = client.post("/api/token", json={"account_id": "acc-001"}).json()
        assert token["roomName"]
        import jwt

        claims = jwt.decode(token["token"], options={"verify_signature": False})
        assert claims["video"]["room"] == token["roomName"]
        assert claims["video"]["roomJoin"] is True
        assert token["url"] == "ws://localhost:7880"
        created = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": "obj-1", "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = created["id"]
        client.post(f"/api/calls/{call_id}/turns", params={"role": "user", "transcript": "嗯 然后 优惠", "emotion": "neutral"})
        settled = client.post(f"/api/calls/{call_id}/settle").json()
        assert settled["status"] == "done"


def test_settings_object_persona_knowledge_and_reports():
    with TestClient(app) as client:
        settings = client.get("/api/settings").json()
        assert settings["policy"] == "offline_first"
        saved = client.put(
            "/api/settings",
            json={
                "asr": {"provider": "sherpa_sensevoice", "model": "sensevoice"},
                "llm": {"provider": "ollama", "model": "qwen", "api_key": "ollama"},
                "tts": {"provider": "volcano_streaming", "access_token": "secret"},
                "vad": {"provider": "silero"},
                "policy": "offline_first",
            },
        ).json()
        assert saved["llm"]["has_api_key"] is True

        obj = client.post(
            "/api/objects",
            params={"account_id": "acc-001"},
            json={"display_name": "Nguyen", "role_template": "buyer", "language": "vi", "background": "test"},
        ).json()
        obj_id = obj["id"]
        updated = client.patch(f"/api/objects/{obj_id}", json={"display_name": "Nguyen V2"}).json()
        assert updated["display_name"] == "Nguyen V2"

        persona = client.post("/api/personas", json={"account_id": "acc-001", "name": "小博"}).json()
        persona_id = persona["id"]
        assert client.put(f"/api/personas/{persona_id}", json={"account_id": "acc-001", "name": "小博2"}).json()["name"] == "小博2"

        imported = client.post(
            "/api/knowledge/import",
            json={"account_id": "acc-001", "path": "p.md", "content": "产品知识"},
        ).json()
        assert imported["indexed"] >= 1
        docs = client.get("/api/knowledge", params={"account_id": "acc-001"}).json()
        assert any(d["path"].endswith("p.md") for d in docs)

        report = client.get("/api/reports/summary", params={"account_id": "acc-001"}).json()
        assert isinstance(report["total_calls"], int)


def test_sidecar_health_routes_exist():
    with TestClient(app) as client:
        asr = client.get("/api/asr/health")
        tts = client.get("/api/tts/health")
        assert asr.status_code in {200, 503}
        assert tts.status_code in {200, 503}
