from __future__ import annotations

import httpx


class ControlPlaneClient:
    """Thin HTTP client from the Agent worker to the Control Plane REST API."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=15)

    async def get_call(self, call_id: str) -> dict:
        r = await self._client.get(f"/api/calls/{call_id}")
        r.raise_for_status()
        return r.json()

    async def get_object(self, object_id: str) -> dict:
        r = await self._client.get(f"/api/objects/{object_id}")
        r.raise_for_status()
        return r.json()

    async def get_persona(self, persona_id: str) -> dict:
        r = await self._client.get(f"/api/personas/{persona_id}")
        r.raise_for_status()
        return r.json()

    async def search_knowledge(self, query: str, account_id: str, limit: int = 5) -> list[dict]:
        r = await self._client.get(
            "/api/knowledge/search",
            params={"query": query, "account_id": account_id, "limit": limit},
        )
        r.raise_for_status()
        return r.json()

    async def add_turn(
        self,
        call_id: str,
        role: str,
        transcript: str,
        emotion: str = "",
        provider: str = "",
        latency_ms: int = 0,
    ) -> None:
        await self._client.post(
            f"/api/calls/{call_id}/turns",
            params={
                "role": role,
                "transcript": transcript,
                "emotion": emotion,
                "provider": provider,
                "latency_ms": latency_ms,
            },
        )

    async def settle(self, call_id: str) -> dict:
        r = await self._client.post(f"/api/calls/{call_id}/settle")
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        await self._client.aclose()
