"""End-to-end HTTP test against a running Control Plane on port 8000.

Run after starting docker services and `uvicorn control_plane.main:app`.
"""

from __future__ import annotations

import httpx


BASE = "http://127.0.0.1:8000"


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=20) as client:
        health = client.get("/health").json()
        assert health["ok"] is True, health
        print("health ok")

        obj = client.post(
            "/api/objects",
            params={"account_id": "acc-001"},
            json={"display_name": "越南采购商", "role_template": "buyer", "language": "vi", "background": "关注 MOQ 与账期"},
        ).json()
        obj_id = obj["id"]
        print("object created:", obj_id)

        imported = client.post(
            "/api/knowledge/import",
            json={"account_id": "acc-001", "path": "product.md", "content": "我们的产品支持越南语与粤语实时通话，支持离线 ASR。"},
        ).json()
        assert imported.get("indexed", 0) >= 1, imported
        print("knowledge import ok:", imported)

        hits = client.get("/api/knowledge/search", params={"query": "越南语", "account_id": "acc-001"}).json()
        assert hits and hits[0]["account_id"] == "acc-001", hits
        hits_b = client.get("/api/knowledge/search", params={"query": "越南语", "account_id": "acc-002"}).json()
        assert hits_b == [], hits_b
        print("knowledge search + isolation ok")

        call = client.post(
            "/api/calls",
            json={"account_id": "acc-001", "object_id": obj_id, "persona_id": "p-1", "mode": "simulation"},
        ).json()
        call_id = call["id"]
        print("call created:", call_id)

        client.post(f"/api/calls/{call_id}/turns", params={"role": "user", "transcript": "嗯 然后 优惠 这个价格", "emotion": "neutral"})
        client.post(f"/api/calls/{call_id}/turns", params={"role": "assistant", "transcript": "我们支持账期优惠", "emotion": "friendly"})
        detail = client.get(f"/api/calls/{call_id}").json()
        assert detail["id"] == call_id, detail
        print("call detail ok")

        settled = client.post(f"/api/calls/{call_id}/settle").json()
        assert settled["status"] == "done", settled
        settlement = client.get(f"/api/calls/{call_id}/settlement").json()
        assert settlement["call_id"] == call_id, settlement
        assert settlement["transcript_doc_path"].startswith("accounts/acc-001/")
        print("settlement ok:", settlement["status"], settlement["metrics"])

        join = client.post(f"/api/supervisor/{call_id}/join").json()
        assert join["role"] == "supervisor", join
        assert client.post(f"/api/supervisor/{call_id}/pause-agent").json()["status"] == "paused"
        assert client.post(f"/api/supervisor/{call_id}/takeover").json()["status"] == "paused"
        assert client.post(f"/api/supervisor/{call_id}/transfer").json()["status"] == "ended"
        print("supervisor commands ok")

        active = client.get("/api/supervisor/active-calls").json()
        assert isinstance(active, list), active
        print("active-calls list ok:", len(active))

    print("\nE2E HTTP PASSED")


if __name__ == "__main__":
    main()
