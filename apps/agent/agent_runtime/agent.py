from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

from bok_voice_core.policies import ProviderRegistry, ProviderState, select_session_manifest
from bok_voice_core.types import CallMode

from .plugins.context import ContextInjector
from .plugins.knowledge import KnowledgePlugin
from .plugins.settlement import SettlementTrigger
from .providers.registry import build_provider_registry
from .control_plane import ControlPlaneClient

# 剥掉进 TTS 那一路的 <expr/> 标签（防被念出来）；转录那一路框架会自动剥离并发布 mood。
_EXPR_TAG_RE = re.compile(r"<expr\b[^>]*?/>|<[^>]+>")
_EXPR_PARTIAL_RE = re.compile(r"<expr\b[^>]*$")
# Sync cleaner for the *recorded* transcript path: strip the internal <expr/>
# emotion tag (open/closed/self-closing, including a dangling open fragment)
# so the persisted transcript is clean customer-facing copy.
_EXPR_SYNC_RE = re.compile(r"<expr\b[^>]*?/>|<expr\b[^>]*>|</expr>|<expr\b[^>]*$")


def _clean_transcript(text: str) -> str:
    return _EXPR_SYNC_RE.sub("", text).strip()


async def _strip_expr_markup(text):
    carry = ""
    async for chunk in text:
        combined = carry + chunk
        out = _EXPR_TAG_RE.sub("", combined)
        # A tag split across stream chunks has no closing ">" yet: hold the
        # trailing "<expr ..." fragment until the next chunk completes it.
        m = _EXPR_PARTIAL_RE.search(out)
        if m and out[m.start() :].startswith("<expr"):
            carry = out[m.start() :]
            out = out[: m.start()]
        else:
            carry = ""
        if out:
            yield out
    # Drop any dangling partial tag at the end of the stream.
    if carry:
        yield ""


def _parse_voice_map(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"zh": raw}
        except Exception:
            return {"zh": raw}
    return {"zh": str(raw or "")}


def _sidecar_base_url(cfg_base: str, env_name: str, default: str) -> str:
    """Prefer the container-facing env URL over a browser-local loopback config.

    The settings page stores 127.0.0.1 base URLs (correct for browser-side
    health checks), but the Agent worker runs inside Docker where 127.0.0.1 is
    the container itself. Any loopback value from the control plane is treated
    as a local-dev default and overridden by the environment variable.
    """
    if cfg_base and "127.0.0.1" not in cfg_base and "localhost" not in cfg_base:
        return cfg_base
    return os.environ.get(env_name, default)


def build_dummy_manifest(*, session_id: str, account_id: str, object_id: str, persona_id: str, mode: str = "simulation") -> dict:
    manifest = select_session_manifest(
        session_id=session_id,
        account_id=account_id,
        object_id=object_id,
        persona_id=persona_id,
        mode=CallMode(mode),
        providers={"vad": "silero", "asr": "qwen3_asr", "llm": "mlx", "tts": "qwen3_tts"},
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
    parts.append(
        "回复语言必须与用户使用的语言严格一致："
        "用户说普通话就只用标准普通话回复（严禁使用任何粤语口语用字）；"
        "用户说粤语就用地道粤语口语回复（使用如：唔、冇、嘅、哋、佢、喺、嚟、啲、咁、係、唔該、"
        "倾偈、而家、睇嚟、啱啱）；用户说英语就用英语回复。"
    )
    return "\n".join(parts)


async def entrypoint(ctx):
    """LiveKit Agent job entrypoint (must be module-level for pickling)."""
    from livekit.agents import Agent, AgentSession, TurnHandlingOptions, inference, stt
    from .providers.livekit_plugins import DeepSeekLLM, ExprAwareLLM, OllamaLLM

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

    # 全局 Provider 设置优先；读取失败或未配置时回退到环境变量。
    settings: dict = {}
    try:
        settings = await cp.get_settings()
    except Exception as e:
        print(f"[agent] settings resolve failed ({room_name}): {e}", flush=True)
    llm_cfg = settings.get("llm", {})
    asr_cfg = settings.get("asr", {})
    tts_cfg = settings.get("tts", {})
    vad_cfg = settings.get("vad", {})

    from .providers.livekit_plugins import (
        DeepSeekLLM,
        FakeLiveKitSTT,
        FakeLiveKitTTS,
        FakeLiveKitVAD,
        LanguageState,
        OllamaLLM,
        Qwen3ASRSTT,
        Qwen3TTSTTS,
        ScriptedLLM,
        SherpaSenseVoiceSTT,
        VolcanoTTS,
    )

    language_state = LanguageState()
    use_fake = os.environ.get("USE_FAKE_MEDIA") == "1"

    if use_fake:
        vad_provider = FakeLiveKitVAD()
        stt_provider = FakeLiveKitSTT()
        tts_provider = FakeLiveKitTTS()
    else:
        vad_provider = inference.VAD(
            # 默认 60s 的 max_buffered_speech 会在无静音假音频/连续说话时
            # 一直缓冲不切句，最终把 worker 拖到「process is unresponsive」。
            max_buffered_speech=float(os.environ.get("VAD_MAX_BUFFERED_SPEECH", "15")),
            min_speech_duration=float(os.environ.get("VAD_MIN_SPEECH_DURATION", "0.15")),
            min_silence_duration=float(os.environ.get("VAD_MIN_SILENCE_DURATION", "0.35")),
        )
        asr_provider_name = asr_cfg.get("provider") or "qwen3_asr"
        if asr_provider_name == "qwen3_asr":
            stt_provider = stt.StreamAdapter(
                stt=Qwen3ASRSTT(
                    base_url=_sidecar_base_url(
                        asr_cfg.get("base_url") or "",
                        "QWEN3_ASR_BASE_URL",
                        "http://127.0.0.1:8787",
                    ),
                    language_state=language_state,
                ),
                vad=vad_provider,
            )
        else:
            stt_provider = stt.StreamAdapter(
                stt=SherpaSenseVoiceSTT(language_state=language_state),
                vad=vad_provider,
            )

        tts_provider_name = tts_cfg.get("provider") or "qwen3_tts"
        if tts_provider_name == "qwen3_tts":
            voice_map = _parse_voice_map(
                (persona or {}).get("reference_audio")
                or tts_cfg.get("speaker")
                or tts_cfg.get("speaker_zh")
            )
            tts_provider = Qwen3TTSTTS(
                base_url=_sidecar_base_url(
                    tts_cfg.get("base_url") or "",
                    "QWEN3_TTS_BASE_URL",
                    "http://127.0.0.1:8788",
                ),
                voice=voice_map,
                language_state=language_state,
                instruct=tts_cfg.get("instruct") or "",
                sample_rate=int(tts_cfg.get("sample_rate") or 24000),
            )
        else:
            tts_provider = VolcanoTTS(
                sample_rate=int(tts_cfg.get("sample_rate") or 24000),
            )

    llm_provider_name = llm_cfg.get("provider") or "ollama"
    if os.environ.get("SCRIPTED_LLM") == "1":
        llm_provider = ScriptedLLM(
            expect_kw=os.environ.get("SCRIPTED_LLM_EXPECT_KW", ""),
            output=os.environ.get("SCRIPTED_LLM_OUTPUT", "（脚本回复）"),
        )
    elif llm_provider_name == "deepseek":
        api_key = llm_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
        if api_key:
            llm_provider = DeepSeekLLM(
                api_key=api_key,
                model=llm_cfg.get("model") or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                base_url=llm_cfg.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            )
        else:
            llm_provider = OllamaLLM(
                base_url=os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1"),
                model=os.environ.get("OLLAMA_MODEL", "huihui_ai/qwen3.5-abliterated:9b"),
            )
    elif llm_provider_name in ("mlx", "local_openai", "lmstudio"):
        from .providers.livekit_plugins import MlxLlmLLM

        llm_provider = MlxLlmLLM(
            base_url=llm_cfg.get("base_url")
            or os.environ.get("MLX_LLM_BASE_URL", "http://host.docker.internal:1235/v1"),
            model=llm_cfg.get("model") or os.environ.get("MLX_LLM_MODEL", None),
        )
    elif llm_provider_name == "ollama":
        llm_provider = OllamaLLM(
            base_url=llm_cfg.get("base_url") or os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1"),
            model=llm_cfg.get("model") or os.environ.get("OLLAMA_MODEL", "huihui_ai/qwen3.5-abliterated:9b"),
            api_key=llm_cfg.get("api_key") or "ollama",
        )
    elif llm_provider_name == "fake":
        from .providers.livekit_plugins import ScriptedLLM

        llm_provider = ScriptedLLM(output=os.environ.get("SCRIPTED_LLM_OUTPUT", "（脚本回复）"))
    else:
        llm_provider = inference.LLM("google/gemma-4-31b-it")

    # 确定性 mood 通道：无论模型是否遵守「吐 <expr> 标签」的指令，
    # ExprAwareLLM 都会在每次回复前强制前置标签，保证转录发布 lk.expression。
    llm_provider = ExprAwareLLM(llm_provider)

    session = AgentSession(
        vad=vad_provider,
        stt=stt_provider,
        llm=llm_provider,
        tts=tts_provider,
        # 官方低延迟调参（docs.livekit.io/agents/logic/turns/tuning）：
        # - dynamic endpointing：按会话停顿统计自适应，min 0.35s 加速切句
        # - preemptive_tts：在轮次确认前就开跑 LLM->TTS，代价是打断时浪费算力
        # - interruption 保持自适应（无模型时自动回退 VAD），min_duration 收紧到 0.35s
        turn_handling=TurnHandlingOptions(
            endpointing={
                "mode": "dynamic",
                "min_delay": float(os.environ.get("ENDPOINT_MIN_DELAY", "0.35")),
                "max_delay": float(os.environ.get("ENDPOINT_MAX_DELAY", "2.0")),
            },
            preemptive_generation={
                "enabled": True,
                "preemptive_tts": os.environ.get("PREEMPTIVE_TTS", "1") == "1",
                "max_speech_duration": 10.0,
                "max_retries": 3,
            },
            interruption={
                "enabled": True,
                "min_duration": 0.35,
                "min_words": 0,
            },
        ),
        # 默认 ["filter_markdown","filter_emoji"] 会被整体替换，故带上内置两项；
        # 追加的自定义 transform 把 <expr/> 从进 TTS 的文本里剥掉（转录路径保留，框架发布 mood）。
        tts_text_transforms=["filter_markdown", "filter_emoji", _strip_expr_markup],
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
        asyncio.create_task(cp.add_turn(call_id, role, _clean_transcript(text)))

    def _on_close(ev):
        asyncio.create_task(cp.settle(call_id))

    session.on("conversation_item_added", _on_conversation_item)
    session.on("close", _on_close)

    agent = Agent(instructions=instructions or "你是 Bok Voice 客服助手。")
    # AgentSession 内部已注册 job shutdown callback（自动 aclose），
    # 这里不能提前 close，否则会话在接通后立刻被销毁。
    await session.start(agent=agent, room=ctx.room)

    await session.generate_reply(instructions="请问有什么可以帮您？")


def run_agent() -> None:
    """Start the LiveKit Agent worker (imports are lazy so tests need no livekit)."""
    from livekit.agents import WorkerOptions, cli

    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
