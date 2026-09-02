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

try:
    from bok_voice_obs.logging import configure_logging, get_logger
    from bok_voice_obs.context import set_correlation, Correlation

    configure_logging(level=os.environ.get("BOK_LOG_LEVEL", "INFO"))
    agent_log = get_logger("agent", component="agent", service="agent")
except Exception:  # pragma: no cover - observability must never break the agent
    agent_log = None


def _agent_log(event: str, **data):
    if agent_log:
        agent_log.info(event, extra={"event": event, "component": "agent", "data": data})


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


_LANG_LABELS = {"zh": "普通话/中文", "yue": "粤语", "en": "英语"}


def _normalize_lang(raw, default: str = "") -> str:
    """把对象/人设里的语言值归一为 zh/yue/en。vi 等未支持语言回落到 default。"""
    key = (raw or "").strip().lower()
    mapping = {
        "zh": "zh", "chinese": "zh", "mandarin": "zh", "普通话": "zh", "中文": "zh",
        "yue": "yue", "cantonese": "yue", "粤": "yue", "粤语": "yue", "广东话": "yue",
        "en": "en", "english": "en", "英语": "en",
        "vi": "", "vietnamese": "", "auto": "", "": "",
    }
    return mapping.get(key, key if key in {"zh", "yue", "en"} else default)


def _build_default_voice_map(tts_cfg: dict) -> dict:
    """组每语言兜底音色（persona 未绑定 reference_audio 时使用）。

    返回 {zh|yue|en: voice_id}；只包含已配置的语言。Qwen3TTSTTS 解析时
    当前语言缺省会回落到 zh，因此只配 speaker_zh 也能让粤语/英语轮次出声。
    """
    single = tts_cfg.get("speaker") or ""
    mapping: dict[str, str] = {}
    zh = tts_cfg.get("speaker_zh") or single
    if zh:
        mapping["zh"] = zh
    yue = tts_cfg.get("speaker_yue")
    if yue:
        mapping["yue"] = yue
    en = tts_cfg.get("speaker_en")
    if en:
        mapping["en"] = en
    return mapping


def _vad_float(cfg: dict, key: str, env_name: str, default: str) -> float:
    """VAD 时长参数：agent 侧显式环境变量优先（部署覆盖），否则用设置页值。"""
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        raw = cfg.get(key)
    if raw is None or raw == "":
        raw = default
    try:
        return float(raw)
    except (TypeError, ValueError):  # pragma: no cover - 配置错误按默认值兜底
        return float(default)


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
    snippets: list[dict] | None = None,
    history: str = "",
    template: dict | None = None,
) -> str:
    parts: list[str] = []
    if persona:
        name = persona.get("name") or "Bok Voice"
        company = persona.get("company") or ""
        # 人设语言 = AI 的母语基调：默认用该语言，但客户明确说其它语言时仍跟随客户。
        persona_lang = _normalize_lang(persona.get("language"))
        lang_note = f"你以{_LANG_LABELS.get(persona_lang, persona_lang)}为母语。" if persona_lang else ""
        # 对象绑定的话术模板可覆盖人设语气（tone_override 优先）。
        tone = (template or {}).get("tone_override") or persona.get("tone") or ""
        style = f"说话风格：{tone}。" if tone else ""
        parts.append(f"你是{name}，代表{company}。{lang_note}{style}".replace("。。", "。"))
    if object_card:
        display = object_card.get("display_name") or ""
        role = object_card.get("role_template") or "客户"
        lang = object_card.get("language") or "中文"
        parts.append(f"当前对话对象：{display}（{role}，语言 {lang}）。")
    if template:
        tpl_name = template.get("name") or ""
        tpl_lines: list[str] = []
        if template.get("opening"):
            tpl_lines.append(f"开场白：{template.get('opening')}")
        if template.get("core"):
            tpl_lines.append(f"核心话术：{template.get('core')}")
        if template.get("objection"):
            tpl_lines.append(f"异议应对：{template.get('objection')}")
        if template.get("closing"):
            tpl_lines.append(f"收尾话术：{template.get('closing')}")
        if tpl_lines:
            prefix = f"对话模板「{tpl_name}」：" if tpl_name else "对话模板："
            parts.append(prefix + "；".join(tpl_lines) + "。严格按模板组织回应。")
    if history:
        parts.append(f"上次聊到：{history}")
    parts.append(
        "回复语言必须与用户使用的语言严格一致："
        "用户说普通话就只用标准普通话回复（严禁使用任何粤语口语用字）；"
        "用户说粤语就用地道粤语口语回复（使用如：唔、冇、嘅、哋、佢、喺、嚟、啲、咁、係、唔該、"
        "倾偈、而家、睇嚟、啱啱）；用户说英语就用英语回复。"
        "在尚未确定客户语言（如开场）或客户语言不明时，使用你（客服）的母语。"
        "直接以该语言回应，不要解释你正在使用什么语言，不要添加任何注释或括号说明。"
    )
    return "\n".join(parts)


async def entrypoint(ctx):
    """LiveKit Agent job entrypoint (must be module-level for pickling)."""
    from livekit.agents import Agent, AgentSession, StopResponse, TurnHandlingOptions, inference, stt
    from .providers.livekit_plugins import ContextState, DeepSeekLLM, ExprAwareLLM

    room_name = ctx.room.name
    call_id = os.environ.get("AGENT_CALL_ID") or room_name
    cp_base = os.environ.get("CONTROL_PLANE_URL") or "http://127.0.0.1:8000"
    cp = ControlPlaneClient(cp_base)

    # Resolve business context (call -> object -> persona -> knowledge) for injection.
    call: dict | None = None
    persona: dict | None = None
    object_card: dict | None = None
    template: dict | None = None
    snippets: list[dict] = []
    context_state = ContextState(account_id="acc-001")
    try:
        call = await cp.get_call(call_id)
        account_id = call.get("account_id", "acc-001")
        if agent_log:
            try:
                set_correlation(Correlation(request_id=call_id, call_id=call_id, account_id=account_id))
            except Exception:
                pass
            _agent_log("agent.call.context", call_id=call_id, account_id=account_id)
        context_state = ContextState(account_id=account_id)
        object_id = call.get("object_id", "")
        persona_id = call.get("persona_id", "")
        if object_id:
            object_card = await cp.get_object(object_id)
            template_id = (object_card or {}).get("template_id", "")
            if template_id:
                template = await cp.get_template(template_id)
        if persona_id:
            persona = await cp.get_persona(persona_id)
        query = (object_card or {}).get("background") or "产品介绍"
        snippets = await cp.search_knowledge(query, account_id, 5)
        # 初始上下文：接通时按对象背景预载少量知识；后续每轮按用户问题实时覆盖。
        context_state.set_knowledge(snippets)
    except Exception as e:
        print(f"[agent] context resolve failed ({room_name}): {e}", flush=True)

    instructions = _instructions(
        persona=persona,
        object_card=object_card,
        template=template,
    )

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
        MlxLlmLLM,
        Qwen3ASRSTT,
        Qwen3TTSTTS,
        ContextAwareLLM,
        ContextState,
        ScriptedLLM,
        SherpaSenseVoiceSTT,
        VolcanoTTS,
    )

    language_state = LanguageState()
    # 开场语言 = 人设(AI)语言优先（用户在人设里选了普通话/粤语/英文，就是期望 AI 用它说话）；
    # 未设置时回落到对象(客户)语言；再退回普通话。之后每轮由 ASR 检出的客户语言覆盖。
    greet_lang = _normalize_lang((persona or {}).get("language")) or _normalize_lang((object_card or {}).get("language")) or "zh"
    language_state.lang = greet_lang
    from .plugins.emotion import EmotionState

    emotion_state = EmotionState()
    use_fake = os.environ.get("USE_FAKE_MEDIA") == "1"

    # ---- VAD：设置页 vad.provider / 时长 / 打断开关；环境变量仅作部署覆盖 ----
    vad_provider_name = (vad_cfg.get("provider") or "silero").lower()
    if use_fake or vad_provider_name in ("fake", "fake_vad"):
        vad_provider = FakeLiveKitVAD()
    else:
        vad_provider = inference.VAD(
            max_buffered_speech=_vad_float(vad_cfg, "max_buffered_speech", "VAD_MAX_BUFFERED_SPEECH", "15"),
            min_speech_duration=_vad_float(vad_cfg, "min_speech_duration", "VAD_MIN_SPEECH_DURATION", "0.15"),
            min_silence_duration=_vad_float(vad_cfg, "min_silence_duration", "VAD_MIN_SILENCE_DURATION", "0.35"),
        )
    interruption_enabled = bool(vad_cfg.get("interruption", True)) if vad_provider_name != "fake" else True

    # ---- ASR：设置页 asr.provider（qwen3_asr / sherpa_sensevoice / fake）----
    asr_provider_name = (asr_cfg.get("provider") or "qwen3_asr").lower()
    if use_fake or asr_provider_name in ("fake", "fake_stt"):
        stt_provider = FakeLiveKitSTT()
    else:
        # 只有显式选择 sherpa 才走 sherpa-onnx；未知/缺失/历史值一律回退 sidecar
        # （Qwen3-ASR），避免配置写错导致 agent 崩溃（sherpa 模型不再随包）。
        use_sherpa = asr_provider_name in {"sherpa", "sherpa_sensevoice"}
        stt_provider = stt.StreamAdapter(
            stt=(
                SherpaSenseVoiceSTT(language_state=language_state)
                if use_sherpa
                else Qwen3ASRSTT(
                    base_url=_sidecar_base_url(
                        asr_cfg.get("base_url") or "",
                        "QWEN3_ASR_BASE_URL",
                        "http://127.0.0.1:8787",
                    ),
                    language_state=language_state,
                )
            ),
            vad=vad_provider,
        )

    # ---- TTS：设置页 tts.provider（qwen3_tts / volcano_streaming / fake）----
    tts_provider_name = (tts_cfg.get("provider") or "qwen3_tts").lower()
    if use_fake or tts_provider_name in ("fake", "fake_tts"):
        # fake = 静音测试音（FakeLiveKitTTS）；绝不落入 Volcano 的 beep 分支。
        tts_provider = FakeLiveKitTTS()
    elif tts_provider_name in ("volcano", "volcano_streaming"):
        tts_provider = VolcanoTTS(
            sample_rate=int(tts_cfg.get("sample_rate") or 24000),
        )
    else:
        if tts_provider_name not in ("", "qwen3_tts"):
            print(f"[agent] unknown tts provider {tts_provider_name!r}, fallback qwen3_tts", flush=True)
        persona_voice = (persona or {}).get("reference_audio") or ""
        if persona_voice:
            # persona 绑定的分语言音色优先（老格式单字符串 → {zh: ...}）。
            voice_map = _parse_voice_map(persona_voice)
        else:
            # 兜底：设置页 speaker_zh/yue/en 组每语言音色。
            voice_map = _build_default_voice_map(tts_cfg)
        tts_provider = Qwen3TTSTTS(
            base_url=_sidecar_base_url(
                tts_cfg.get("base_url") or "",
                "QWEN3_TTS_BASE_URL",
                "http://127.0.0.1:8788",
            ),
            voice=voice_map,
            language_state=language_state,
            instruct=tts_cfg.get("instruct") or "",
            emotion_state=emotion_state,
            sample_rate=int(tts_cfg.get("sample_rate") or 24000),
        )

    llm_provider_name = llm_cfg.get("provider") or "local_openai"
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
            # 云端 provider 缺 key：显式告警并回退本地，不再静默发生。
            _agent_log("llm.deepseek.no_api_key", fallback="mlx")
            print("[agent] deepseek selected but DEEPSEEK_API_KEY missing — falling back to local MLX LLM", flush=True)
            llm_provider = MlxLlmLLM(
                base_url=os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
                model=os.environ.get("MLX_LLM_MODEL", "local"),
            )
    elif llm_provider_name in ("mlx", "local_openai", "lmstudio"):
        llm_provider = MlxLlmLLM(
            base_url=llm_cfg.get("base_url")
            or os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
            model=llm_cfg.get("model") or os.environ.get("MLX_LLM_MODEL", ""),
        )
    elif llm_provider_name == "fake":
        from .providers.livekit_plugins import ScriptedLLM

        llm_provider = ScriptedLLM(output=os.environ.get("SCRIPTED_LLM_OUTPUT", "（脚本回复）"))
    else:
        llm_provider = inference.LLM("google/gemma-4-31b-it")

    # 确定性 mood 通道：无论模型是否遵守「吐 <expr> 标签」的指令，
    # ExprAwareLLM 都会在每次回复前强制前置标签，保证转录发布 lk.expression。
    llm_provider = ContextAwareLLM(
        ExprAwareLLM(llm_provider, emotion_state=emotion_state),
        context_state=context_state,
    )

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
                # 默认关闭：preemptive 会在用户句确认前抢先生成，导致回复与上下文
                # 错位（观察：粤语轮偶发回普通话问候）。关闭只损失少量首包延迟，
                # 换取每轮回复严格基于已确认的用户输入（可用 PREEMPTIVE_GENERATION=1 开启）。
                "enabled": os.environ.get("PREEMPTIVE_GENERATION", "0") == "1",
                "preemptive_tts": os.environ.get("PREEMPTIVE_TTS", "1") == "1",
                "max_speech_duration": 10.0,
                "max_retries": 3,
            },
            interruption={
                "enabled": interruption_enabled,
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

    async def _async_update_context(role, text):
        # 渐进披露：每轮按用户当前问题实时检索，覆盖初始知识，并维护整场对话摘要。
        try:
            if role == "user":
                hits = await cp.search_knowledge(text, context_state.account_id, 5)
                context_state.set_knowledge(hits)
            context_state.add_summary(role, _clean_transcript(text))
        except Exception as exc:  # pragma: no cover - context must not break turns
            print(f"[agent] context update failed: {exc!r}", flush=True)

    def _on_item_for_context(ev):
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            return
        text = getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or ""
        if text:
            if role == "user":
                # 同步注入：用 ASR 检测到的用户语言约束本轮回回复语言。
                # 必须在异步检索之前设置，否则 LLM 请求可能先于指令注入发出。
                context_state.set_user_language(language_state.lang)
            asyncio.create_task(_async_update_context(role, text))

    def _on_close(ev):
        asyncio.create_task(cp.settle(call_id))

    session.on("conversation_item_added", _on_conversation_item)
    session.on("conversation_item_added", _on_item_for_context)
    session.on("close", _on_close)

    agent = Agent(instructions=instructions or "你是 Bok Voice 客服助手。")
    # AgentSession 内部已注册 job shutdown callback（自动 aclose），
    # 这里不能提前 close，否则会话在接通后立刻被销毁。
    await session.start(agent=agent, room=ctx.room)

    await session.generate_reply(instructions="请问有什么可以帮您？")


def run_agent() -> None:
    """Start the LiveKit Agent worker (imports are lazy so tests need no livekit)."""
    import sys

    from livekit.agents import WorkerOptions, cli

    # livekit-agents 1.7.x 的 cli.run_app 需要显式子命令（start）。
    if len(sys.argv) == 1:
        sys.argv.append("start")
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
