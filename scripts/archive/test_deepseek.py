"""Call DeepSeek through the LiveKit LLM plugin to validate the key."""

from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import asyncio
import os

from agent_runtime.providers.livekit_plugins import DeepSeekLLM
from livekit.agents import llm


async def main() -> None:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY missing")
    model = DeepSeekLLM(api_key=key, model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content="用一句话介绍你们的实时语音客服产品。")
    stream = model.chat(chat_ctx=ctx)
    parts: list[str] = []
    async for chunk in stream:
        if chunk.delta and chunk.delta.content:
            parts.append(chunk.delta.content)
    reply = "".join(parts).strip()
    if not reply:
        raise SystemExit("empty reply from DeepSeek")
    print("DEEPSEEK_OK", len(reply), reply[:80])


if __name__ == "__main__":
    asyncio.run(main())
