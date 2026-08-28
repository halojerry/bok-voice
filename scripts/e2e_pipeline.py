"""Deterministic end-to-end pipeline test against the running Docker stack.

Verifies the business loop without a browser mic:
  1) knowledge import persists Markdown + pgvector chunk,
  2) knowledge search honours account isolation,
  3) object/persona are fetched and injected into the agent instructions,
  4) the scripted LLM replies per the specified 话术 when the injected context
     contains the expected knowledge token,
  5) turns persist and settlement runs.

Run:  python scripts/e2e_pipeline.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for part in ("packages/core", "apps/agent"):
    p = ROOT / part
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

BASE = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")

from agent_runtime.agent import _instructions  # noqa: E402
from agent_runtime.providers.livekit_plugins import ScriptedLLM  # noqa: E402
from livekit.agents.llm import ChatContext  # noqa: E402

ACCOUNT = "acc-001"
OTHER = "acc-002"
KEYWORD = "MT3000"
SCRIPTED = "我们支持MT3000越南语离线通话，欢迎联系。"


def assert_step(label: str, cond: bool, data=None) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}", data or "")
    if not cond:
        raise SystemExit(f"FAILED: {label}")


async def run(client: httpx.AsyncClient) -> None:
    # 1 + 2) knowledge import + persistence + isolation
    imported = (
        await client.post(
            "/api/knowledge/import",
            json={
                "account_id": ACCOUNT,
                "path": "product-md.md",
                "content": "本公司 MT3000 型号支持越南语与粤语实时离线通话。",
            },
        )
    ).json()
    assert_step("knowledge import indexed", imported.get("indexed", 0) >= 1, imported)

    hits = (
        await client.get("/api/knowledge/search", params={"query": KEYWORD, "account_id": ACCOUNT})
    ).json()
    assert_step("knowledge search found (acc-001)", hits and hits[0]["account_id"] == ACCOUNT, hits[:1])
    other = (
        await client.get("/api/knowledge/search", params={"query": KEYWORD, "account_id": OTHER})
    ).json()
    assert_step("account isolation (acc-002 empty)", other == [], other)

    # 3) object + persona + call
    obj = (
        await client.post(
            "/api/objects",
            params={"account_id": ACCOUNT},
            json={"display_name": "越南采购商 Nguyen", "role_template": "buyer", "language": "vi", "background": "关注 MT3000"},
        )
    ).json()
    persona = (
        await client.put(
            "/api/personas",
            json={"account_id": ACCOUNT, "name": "小博", "company": "Bok 建材", "tone": "专业温和"},
        )
    ).json()
    call = (
        await client.post(
            "/api/calls",
            json={"account_id": ACCOUNT, "object_id": obj["id"], "persona_id": persona["id"], "mode": "simulation", "language": "vi"},
        )
    ).json()
    assert_step("call created", call.get("id") and call["object_id"] == obj["id"], call)

    fetched_obj = (await client.get(f"/api/objects/{obj['id']}")).json()
    fetched_persona = (await client.get(f"/api/personas/{persona['id']}")).json()
    assert_step("get_object", fetched_obj["display_name"] == "越南采购商 Nguyen", fetched_obj["display_name"])
    assert_step("get_persona", fetched_persona["name"] == "小博", fetched_persona["name"])

    # 4) inject into instructions -> scripted LLM replies per specified 话术
    snippets = [
        s for s in hits if s["account_id"] == ACCOUNT
    ]
    instructions = _instructions(persona=fetched_persona, object_card=fetched_obj, snippets=snippets)
    assert_step("instructions contain knowledge", KEYWORD in instructions, KEYWORD)
    ctx = ChatContext()
    ctx.add_message(role="system", content=instructions)
    ctx.add_message(role="user", content=f"你们这款支持{KEYWORD}吗？")
    llm = ScriptedLLM(expect_kw=KEYWORD, output=SCRIPTED)

    async def collect() -> str:
        out = ""
        async for chunk in llm.chat(chat_ctx=ctx):
            delta = getattr(chunk, "delta", None)
            if delta and getattr(delta, "content", None):
                out += delta.content
        return out

    output = await collect()
    assert_step("scripted LLM replies per specified 话术", output == SCRIPTED, output)

    # 5) turns + settlement
    await client.post(
        f"/api/calls/{call['id']}/turns",
        params={"role": "user", "transcript": "嗯 然后 MT3000 优惠多少", "emotion": "neutral"},
    )
    turns = (await client.get(f"/api/calls/{call['id']}/turns")).json()
    assert_step("turn persisted", turns and turns[0]["transcript"].endswith("优惠多少"), turns)
    settled = (await client.post(f"/api/calls/{call['id']}/settle")).json()
    settlement = (await client.get(f"/api/calls/{call['id']}/settlement")).json()
    assert_step("settlement done", settled["status"] == "done" and settlement["status"] == "done", settlement["status"])
    assert_step("settlement doc path scoped to account", settlement["transcript_doc_path"].startswith(f"accounts/{ACCOUNT}/"), settlement["transcript_doc_path"])

    print("\nPIPELINE_E2E_PASSED")


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as client:
        await run(client)


if __name__ == "__main__":
    asyncio.run(main())
