"""Call local Ollama through the LiveKit LLM plugin to validate the model."""

from __future__ import annotations

import asyncio
import os

from agent_runtime.providers.livekit_plugins import OllamaLLM
from livekit.agents import llm


async def main() -> None:
    model = OllamaLLM(
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        model=os.environ.get("OLLAMA_MODEL", "huihui_ai/qwen3.5-abliterated:9b"),
    )
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content="用一句话介绍你们的实时语音客服产品。")
    stream = model.chat(chat_ctx=ctx)
    parts: list[str] = []
    async for chunk in stream:
        if chunk.delta and chunk.delta.content:
            parts.append(chunk.delta.content)
    reply = "".join(parts).strip()
    if not reply:
        raise SystemExit("empty reply from Ollama")
    print("OLLAMA_OK", len(reply), reply[:80])


if __name__ == "__main__":
    asyncio.run(main())
