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
