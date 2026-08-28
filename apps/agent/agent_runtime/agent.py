from __future__ import annotations

import asyncio
import os
from typing import Optional

from bok_voice_core.policies import ProviderRegistry, ProviderState, select_session_manifest
from bok_voice_core.types import CallMode

from .plugins.context import ContextInjector
from .plugins.knowledge import KnowledgePlugin
from .plugins.settlement import SettlementTrigger
from .providers.registry import build_provider_registry
from .control_plane import ControlPlaneClient


def build_dummy_manifest(*, session_id: str, account_id: str, object_id: str, persona_id: str, mode: str = "simulation") -> dict:
    manifest = select_session_manifest(
        session_id=session_id,
        account_id=account_id,
        object_id=object_id,
        persona_id=persona_id,
        mode=CallMode(mode),
        providers={"vad": "livekit", "asr": "sherpa", "llm": "ollama", "tts": "gpt_sovits"},
    )
    return manifest.__dict__


def _instructions(
    *,
    persona: dict | None,
    object_card: dict | None,
    snippets: list[dict],
    history: str = "",
) -> str:
    parts: list[str] = []
    if persona:
        name = persona.get("name") or "Bok Voice"
        company = persona.get("company") or ""
        tone = persona.get("tone") or ""
        style = f"说话风格：{tone}。" if tone else ""
        parts.append(f"你是{name}，代表{company}。{style}".replace("。。", "。"))
    if object_card:
        display = object_card.get("display_name") or ""
        role = object_card.get("role_template") or "客户"
        lang = object_card.get("language") or "中文"
        parts.append(f"当前对话对象：{display}（{role}，语言 {lang}）。")
    if snippets:
        parts.append("以下是产品资料：")
        for s in snippets[:5]:
            text = s.get("text", "")
            if text:
                parts.append(f"- {text}")
    if history:
        parts.append(f"上次聊到：{history}")
    return "\n".join(parts)


async def entrypoint(ctx):
    """LiveKit Agent job entrypoint (must be module-level for pickling)."""
    from livekit.agents import Agent, AgentSession, inference, stt
    from .providers.livekit_plugins import DeepSeekLLM, OllamaLLM

    room_name = ctx.room.name
    call_id = os.environ.get("AGENT_CALL_ID") or room_name
    cp_base = os.environ.get("CONTROL_PLANE_URL") or "http://control-plane:8000"
    cp = ControlPlaneClient(cp_base)

    # Resolve business context (call -> object -> persona -> knowledge) for injection.
    call: dict | None = None
    persona: dict | None = None
    object_card: dict | None = None
    snippets: list[dict] = []
    try:
        call = await cp.get_call(call_id)
        account_id = call.get("account_id", "acc-001")
        object_id = call.get("object_id", "")
        persona_id = call.get("persona_id", "")
        if object_id:
            object_card = await cp.get_object(object_id)
        if persona_id:
            persona = await cp.get_persona(persona_id)
        query = (object_card or {}).get("background") or "产品介绍"
        snippets = await cp.search_knowledge(query, account_id, 5)
    except Exception as e:
        print(f"[agent] context resolve failed ({room_name}): {e}", flush=True)

    instructions = _instructions(persona=persona, object_card=object_card, snippets=snippets)

    # Prefer the deterministic scripted LLM for CI, then a fast low-latency cloud LLM,
    # then Ollama offline, then a generic inference fallback.
    from .providers.livekit_plugins import ScriptedLLM

    llm_provider = None
    if os.environ.get("SCRIPTED_LLM") == "1":
        llm_provider = ScriptedLLM(
            expect_kw=os.environ.get("SCRIPTED_LLM_EXPECT_KW", ""),
            output=os.environ.get("SCRIPTED_LLM_OUTPUT", "（脚本回复）"),
        )
    elif os.environ.get("DEEPSEEK_API_KEY"):
        llm_provider = DeepSeekLLM(api_key=os.environ["DEEPSEEK_API_KEY"])
    elif os.environ.get("OLLAMA_BASE_URL"):
        llm_provider = OllamaLLM(
            base_url=os.environ["OLLAMA_BASE_URL"],
            model=os.environ.get("OLLAMA_MODEL", "huihui_ai/qwen3.5-abliterated:9b"),
        )
    else:
        llm_provider = inference.LLM("google/gemma-4-31b-it")

    use_fake = os.environ.get("USE_FAKE_MEDIA") == "1"
    if use_fake:
        from .providers.livekit_plugins import FakeLiveKitSTT, FakeLiveKitTTS, FakeLiveKitVAD

        vad_provider = FakeLiveKitVAD()
        stt_provider = FakeLiveKitSTT()
        tts_provider = FakeLiveKitTTS()
    else:
        from .providers.livekit_plugins import SherpaSenseVoiceSTT, VolcanoTTS

        vad_provider = inference.VAD()
        # SenseVoice is offline/batch: wrap it in StreamAdapter so LiveKit feeds real VAD
        # segments to `recognize()` instead of raw per-frame streaming (which it can't consume).
        stt_provider = stt.StreamAdapter(stt=SherpaSenseVoiceSTT(), vad=vad_provider)
        tts_provider = VolcanoTTS()

    session = AgentSession(
        vad=vad_provider,
        stt=stt_provider,
        llm=llm_provider,
        tts=tts_provider,
    )

    # Persist turns + auto-settle on hangup (idempotent server side).
    def _on_conversation_item(ev):
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            return
        text = getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or ""
        if not text:
            return
        asyncio.create_task(cp.add_turn(call_id, role, text))

    def _on_close(ev):
        asyncio.create_task(cp.settle(call_id))

    session.on("conversation_item_added", _on_conversation_item)
    session.on("close", _on_close)

    agent = Agent(instructions=instructions or "你是 Bok Voice 客服助手。")
    await session.start(agent=agent, room=ctx.room)
    await session.generate_reply(instructions="请问有什么可以帮您？")


def run_agent() -> None:
    """Start the LiveKit Agent worker (imports are lazy so tests need no livekit)."""
    from livekit.agents import WorkerOptions, cli

    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
