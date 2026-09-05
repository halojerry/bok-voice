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

    async def get_template(self, template_id: str) -> dict:
        r = await self._client.get(f"/api/templates/{template_id}")
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

    async def get_settings(self) -> dict:
        r = await self._client.get("/api/settings", params={"internal": "1"})
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

    async def post_session_report(self, call_id: str, report: dict) -> None:
        """上報官方 SessionReport(真实逐模型 usage/权威 chat_history)。settle 前调。

        失败由 caller 打日志——报表缺真数据回退估算口径,唔阻结算。
        """
        r = await self._client.post(f"/api/calls/{call_id}/session-report", json=report)
        r.raise_for_status()

    async def end_call(self, call_id: str, disposition: str = "declined") -> dict:
        """AI 收尾后主动结束通话:置 ENDED 并断房。

        disposition=declined(客户拒绝,默认)| no_response(沉默心跳两次无回应)。
        失败(404 已结束/网络抖动)由 caller 打日志即可,结算另有 _on_close 幂等兜底。
        """
        r = await self._client.post(
            f"/api/supervisor/{call_id}/end",
            params={"disposition": disposition},
        )
        r.raise_for_status()
        return r.json()

    async def report_whatsapp(self, call_id: str, number: str = "") -> None:
        """上報偵測到客戶俾 WhatsApp。number 有值=captured,空=offered。fire-and-forget。

        raise_for_status 俾 caller 知失敗(清 key 等下次偵測補報)——server 幂等,
        重複 POST 唔會造成重複爆閃。
        """
        r = await self._client.post(
            f"/api/calls/{call_id}/whatsapp",
            json={"number": number},
        )
        r.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
