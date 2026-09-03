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

# 4B/Qwen3 偶发把对话模板收尾 token <|im_end|> 当文字输出——转录同 TTS 都唔可以留低。
# _EXPR_TAG_RE 只剥「有 > 收尾」嘅 tag,<|im_end|> 冇 >,会漏,所以专门补剥。
_EOS_TOKEN_RE = re.compile(r"<\|/?im_(?:start|end)\|>|<\|endoftext\|>", re.IGNORECASE)
_EOS_TOKENS = ("<|im_end|>", "<|im_start|>", "<|endoftext|>")


def _strip_eos_tokens(text: str) -> str:
    return _EOS_TOKEN_RE.sub("", text or "")


def _trailing_eos_partial(text: str) -> str:
    """text 尾部若系某个 EOS token 被 stream 切开嘅真前缀,留低等下个 chunk 凑齐再剥。"""
    low = (text or "").lower()
    best = ""
    for tok in _EOS_TOKENS:
        t = tok.lower()
        for cut in range(1, len(t)):
            if cut > len(best) and low.endswith(t[:cut]):
                best = tok[:cut]
    return best

# MiniMax 支持的拟声标签(2.8-hd/turbo):这些是让它发声效的,不能剥。
# 其余舞台/动作括号提示(（稍作聽筒聲）（笑）(sigh) (pause) 等)会被 TTS 照念,剥掉。
_MINIMAX_VOCAL_TAGS = {"laughs", "chuckle", "coughs", "breath", "sighs", "humming"}
_STAGE_HALFWIDTH_WORDS = {"声", "笑", "停顿", "pause", "sigh", "cough", "清嗓", "动作", "背景", "沉默", "静默"}
_STAGE_FULLWIDTH_RE = re.compile(r"（[^（）]*?(?:声|音|笑|停顿|静默|沉默|稍等|清嗓|动作|背景|聽筒|铃)[^（）]*?）")


def _strip_stage_dirs(text: str) -> str:
    """剥掉会照念的舞台/动作括号;放行 MiniMax 拟声标签(如 (sighs)(laughs))。

    半角括号只有在「内容是舞台/动作词」时才剥,普通括注(如 (例如…))保留。
    """
    if not text:
        return text
    text = _STAGE_FULLWIDTH_RE.sub("", text)

    def _repl(m: "re.Match[str]") -> str:
        inner = m.group(1).strip().lower()
        if inner in _MINIMAX_VOCAL_TAGS:
            return m.group(0)  # MiniMax 会转成声效,保留
        if any(w in inner for w in _STAGE_HALFWIDTH_WORDS):
            return ""  # 半角舞台词剥掉,防照念
        return m.group(0)  # 普通括注保留

    return re.sub(r"\(([^()]*?)\)", _repl, text)


def _clean_transcript(text: str) -> str:
    text = _strip_eos_tokens(text)
    return _EXPR_SYNC_RE.sub("", text).strip()


async def _strip_expr_markup(text):
    carry = ""
    async for chunk in text:
        combined = carry + chunk
        out = _EXPR_TAG_RE.sub("", combined)
        # 舞台/动作括号提示（如（稍作聽筒聲））也会被 TTS 念出来，一并剥掉；
        # 但放行 MiniMax 拟声标签 (sighs)/(laughs) 等，让它可以转成声效。
        out = _strip_stage_dirs(out)
        # Qwen3 偶发把 <|im_end|> 当文字输出,剥走以免被 TTS 念出来。
        out = _strip_eos_tokens(out)
        # A tag split across stream chunks has no closing ">" yet: hold the
        # trailing "<expr ..." fragment (or half an <|im_end|>) until the next
        # chunk completes it.
        m = _EXPR_PARTIAL_RE.search(out)
        held = ""
        if m and out[m.start() :].startswith("<expr"):
            held = out[m.start() :]
            out = out[: m.start()]
        else:
            frag = _trailing_eos_partial(out)
            if frag:
                held = frag
                out = out[: -len(frag)]
        if out:
            yield out
        carry = held
    # Drop any dangling partial tag at the end of the stream.
    if carry:
        yield ""


async def _llm_judge(base_url: str, model: str, messages: list) -> str:
    """流程推进判定器:对本地 MLX LLM 发一个 max_tokens 极短请求,取回一个字。失败返空(唔推进)。"""
    if not base_url or not model:
        return ""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": 8,
                    "temperature": 0,
                    # 本地 MLX 對話模板會 append <|im_end|>,停喺呢度,回應淨係 verdict 字。
                    "stop": ["<|im_end|>", "<|im_start|>", "<|endoftext|>"],
                },
            )
            if r.status_code == 200:
                data = r.json()
                return str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
    except Exception as exc:  # pragma: no cover - 判定失败唔推进,唔阻断通话
        print(f"[flow] llm judge failed: {exc!r}", flush=True)
    return ""


def _parse_voice_map(raw) -> dict:
    # 旧数据 map 键 yue 由 _collapse_voice_map 归一;新写入一律 cantonese。
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"zh": raw}
        except Exception:
            return {"zh": raw}
    return {"zh": str(raw or "")}


def _collapse_voice_map(raw_map: dict, persona_lang: str) -> dict:
    """整场同声：把 persona 的旧 {zh,cantonese,en} 分语言 map 收敛成单一主音色。

    主音色取人设主语言（persona.language）对应键，缺则按 zh→cantonese→en→首个非空
    取；最终统一放进 zh 键（MiniMax/Qwen3 的 _resolve_voice 语言缺省都回落 zh），
    使整场无论客户讲粤/普/英都用同一把声。回退点：若想恢复「按语言分音色」，
    删掉本函数调用、直接传 raw_map 即可。旧数据 map 键 yue 作只读别名仍兼容。
    """
    if not raw_map:
        return {}
    # 旧数据只读别名:键 yue → 统一按 cantonese 读(新写永远用 cantonese)。
    norm_map = {}
    for k, v in raw_map.items():
        nk = "cantonese" if str(k).strip().lower() in {"yue", "cantonese"} else str(k)
        if nk not in norm_map:
            norm_map[nk] = v
    lang = (persona_lang or "").strip().lower()
    if lang == "yue":  # 旧数据只读别名
        lang = "cantonese"
    if lang not in {"zh", "cantonese", "en"}:
        lang = ""
    picked = ""
    for key in ([lang] if lang else []) + ["zh", "cantonese", "en", "yue"]:
        if norm_map.get(key):
            picked = str(norm_map[key])
            break
    if not picked:
        picked = str(next((v for v in norm_map.values() if v), ""))
    return {"zh": picked} if picked else {}


_LANG_LABELS = {"zh": "普通话/中文", "cantonese": "粤语", "en": "英语"}


def _sticky_reply_language(anchor: str, asr_lang: str, cur_sticky: str, cur_streak: int, threshold: int = 2) -> tuple[str, str, int]:
    """粤语客服「始终讲粤语」的语言锚定规则。

    - anchor：客服锚定语言（人设/对象语言，如 cantonese）。LLM 默认始终用它回复。
    - 只有 ASR 连续 threshold 轮判为同一【非锚】语言（且都是强证据才进得来）才跟随切换；
    - 一旦某轮回到锚语言，立刻回锚、清计数；
    - 已切走后客户又讲第三种语言，也回锚（粤语客服优先讲粤语，不跨语言乱跳）。
    返回 (reply_lang, new_sticky, new_streak)。旧数据 yue 作只读别名归一 cantonese。
    """
    if anchor == "yue":  # 旧数据只读别名
        anchor = "cantonese"
    if asr_lang == "yue":
        asr_lang = "cantonese"
    if cur_sticky == "yue":
        cur_sticky = "cantonese"
    if anchor not in {"zh", "cantonese", "en"}:
        # 无有效锚定（未知/缺失）：直接跟随 ASR，退化为旧行为。
        return asr_lang, asr_lang, 0
    if asr_lang == anchor:
        return anchor, anchor, 0
    if asr_lang not in {"zh", "cantonese", "en"}:
        return cur_sticky, cur_sticky, cur_streak
    if cur_sticky == anchor:
        # 仍在锚语言上，连续看到非锚轮。
        if cur_streak + 1 >= threshold:
            return asr_lang, asr_lang, 0  # 客户确实切语言了（连续多轮），跟随。
        return anchor, anchor, cur_streak + 1
    # 已切到某非锚语言；若客户仍讲同一非锚语言 → 保持；换回锚/第三种 → 回锚。
    if asr_lang == cur_sticky:
        return cur_sticky, cur_sticky, 0
    return anchor, anchor, 0


def _normalize_lang(raw, default: str = "") -> str:
    """把对象/人设里的语言值归一为 zh/cantonese/en。vi 等未支持语言回落到 default。

    粤语统一叫 cantonese（旧数据 yue、中文写法粤/粤语/广东话、外部 Cantonese
    都归一到它）——字段只有一套，模型边界(ASR hint)才传得对。
    """
    key = (raw or "").strip().lower()
    mapping = {
        "zh": "zh", "chinese": "zh", "mandarin": "zh", "普通话": "zh", "中文": "zh",
        "cantonese": "cantonese", "yue": "cantonese", "粤": "cantonese", "粤语": "cantonese", "广东话": "cantonese",
        "en": "en", "english": "en", "英语": "en",
        "vi": "", "vietnamese": "", "auto": "", "": "",
    }
    return mapping.get(key, key if key in {"zh", "cantonese", "en"} else default)


def _build_default_voice_map(tts_cfg: dict) -> dict:
    """组默认音色（persona 未绑定 reference_audio 时使用）。

    产品规则「整场同声」：若设置页配了全局单音色 speaker（云端 MiniMax 默认），
    则 zh/cantonese/en 都用它（返回 {"zh": speaker}，任何语言都解析到 zh 兜底键）。
    仅当 speaker 为空时回落旧的分语言 speaker_zh/speaker_cantonese/en（兼容旧数据
    speaker_yue 作只读别名）。Qwen3TTSTTS 解析时当前语言缺省会回落到 zh，
    因此只配 zh 键也能让各语言出声。
    """
    single = (tts_cfg.get("speaker") or "").strip()
    mapping: dict[str, str] = {}
    if single:
        mapping["zh"] = single
        return mapping
    zh = tts_cfg.get("speaker_zh") or ""
    if zh:
        mapping["zh"] = zh
    # 新键 speaker_cantonese 优先；旧键 speaker_yue 作只读别名（DB 迁移后只剩新键）。
    yue = (tts_cfg.get("speaker_cantonese") or tts_cfg.get("speaker_yue") or "").strip()
    if yue:
        mapping["cantonese"] = yue
    en = tts_cfg.get("speaker_en") or ""
    if en:
        mapping["en"] = en
    return mapping


def _context_rag_enabled(has_steps: bool) -> bool:
    """绑了分步话术(has_steps)的封闭流程，默认不做知识库/联网检索——单对象只上话术。

    开放咨询(无模板)才 RAG；CONTEXT_RAG=1 可强制模板场景也检索（逃生口）。
    """
    if os.environ.get("CONTEXT_RAG", "") == "1":
        return True
    return not has_steps


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
        # 模板(steps_json 或旧式四段)统一由 flow 控制器转成步骤、按轮注入,
        # 这里不再整段塞进 system,避免 LLM 把整份话术当逐字稿念。
        from .flow import template_to_steps

        has_steps = bool(template_to_steps(template))
        if not has_steps:
            # 模板无任何内容(既无 steps 也无四段):退化为通用客服,不注模板。
            parts.append(f"无特定话术模板,按专业客服方式自然应对。")
    if history:
        parts.append(f"上次聊到：{history}")
    parts.append(
        # 分寸：专业、克制、可信的客服；表达自然但不油滑、不闲聊套近乎。
        "角色基调：你是一名专业、可信赖的客服。语气沉稳、礼貌、就事论事，"
        "聚焦帮对方解决问题；不要自称「老朋友」、不要刻意套近乎、不要用「呀/啦/哦」等过度的口语尾音，"
        "也不要像机器人念稿或堆客套（避免「您好，很高兴为您服务」这类模板开场）。"
        "用简短自然的口语句子，一句说清一个重点，说完自然停顿等对方；"
        "不要长篇大论、不要列 1.2.3. 条、不要加书名号/星号/表情符号、不要自我解释。"
        "绝不说「不知道/查不到/不清楚」——资料不足就用客服话术接住客户（去核实后回你、转给跟进同事），"
        "客户永远被接住，不被拒绝。"
    )
    parts.append(
        "回复语言必须与用户使用的语言一致："
        "用户说普通话就用自然口语普通话（可用「嗯、好、那、其实、你看」这类口语词，别用书面语/播音腔）；"
        "用户说粤语就整段用地道粤语口语（唔、冇、嘅、哋、佢、喺、嚟、啲、咁、係、唔該、倾偈、而家、睇下、啱啱，"
        "语气参考：「明白，等我帮你睇下呢单先」），绝不写普通话或书面语；用户说英语就用英语口语。"
        "在尚未确定客户语言（如开场）或客户语言不明时，使用你（客服）的母语。"
        "直接以该语言回应，不要解释你正在使用什么语言，不要添加任何注释或括号说明。"
    )
    return "\n".join(parts)


async def entrypoint(ctx):
    """LiveKit Agent job entrypoint (must be module-level for pickling)."""
    from livekit.agents import Agent, AgentSession, StopResponse, TurnHandlingOptions, inference, stt
    from .providers.livekit_plugins import ContextState, DeepSeekLLM, ExprAwareLLM, lecture_guard

    room_name = ctx.room.name
    call_id = os.environ.get("AGENT_CALL_ID") or room_name
    cp_base = os.environ.get("CONTROL_PLANE_URL") or "http://127.0.0.1:8000"
    cp = ControlPlaneClient(cp_base)
    import time as _t

    _t0 = _t.monotonic()

    def _log_stage(name: str) -> None:
        print(f"[agent] {name} +{(_t.monotonic() - _t0) * 1000:.0f}ms ({call_id})", flush=True)

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

    # 对话流程控制器:载入模板分步 + 对象变量;由它按轮注入"当前步",逐步推进。
    from .flow import FlowController, facts_line

    flow_ctrl = FlowController.from_template(template, object_card)
    _log_stage("context_resolved")
    if flow_ctrl.has_steps:
        context_state.set_flow(flow_ctrl.flow_overview(), flow_ctrl.current_step_text())
        print(f"[agent] flow loaded {len(flow_ctrl.steps)} steps (call {room_name})", flush=True)
    else:
        # 无分步模板:对象事实仍注入(变量在四段参考里也可用)。
        context_state.set_flow("", "")

    instructions = _instructions(
        persona=persona,
        object_card=object_card,
        template=template,
    )
    # 对象已知事实(姓名/单号/物流公司)注入基础 system——facts 让 LLM 回复时引用。
    if object_card:
        parts_tail = instructions + "\n\n" + facts_line(object_card)
        instructions = parts_tail

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
        MiniMaxTTS,
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
    # 粤语客服锚定：LLM 默认始终用锚语言（greet_lang），只有客户连续多轮明显讲其它语言才跟随。
    _lang_sticky: dict = {"sticky": greet_lang if greet_lang in {"zh", "cantonese", "en"} else "zh", "streak": 0}
    # 已上報嘅 WhatsApp 狀態(captured=已報號碼, offered=已報應承加),避免每 call 重複 spam。
    _wa_reported: set[str] = set()
    # 背景 flow judge 防疊:記錄而家 judge 緊邊一步(-1=冇)。推進唔可以同時兩個 judge。
    _judge_inflight: dict = {"step": -1}
    from .plugins.emotion import EmotionState

    emotion_state = EmotionState()
    use_fake = os.environ.get("USE_FAKE_MEDIA") == "1"

    # ---- VAD：设置页 vad.provider / 时长 / 灵敏度 / 打断开关；环境变量仅作部署覆盖 ----
    vad_provider_name = (vad_cfg.get("provider") or "silero").lower()
    if use_fake or vad_provider_name in ("fake", "fake_vad"):
        vad_provider = FakeLiveKitVAD()
    else:
        # activation_threshold = Silero 判定「人声」的概率阈值(0~1)：越高越不易被
        # 环境噪声/键盘声误触发。以前没接线、用库默认 0.5，嘈杂环境一路识别。
        # 设置页 vad.sensitivity 存的就是它（0.6~0.8 抗噪，0.4~0.5 更灵敏）。
        # 打断风暴修复后默认 0.75 + min_speech 0.4：短促噪声/底噪不足以判成"用户开口"，
        # 避免 AI 每次刚要开口就被当插话打断、多次后不再出声。
        vad_provider = inference.VAD(
            max_buffered_speech=_vad_float(vad_cfg, "max_buffered_speech", "VAD_MAX_BUFFERED_SPEECH", "15"),
            min_speech_duration=_vad_float(vad_cfg, "min_speech_duration", "VAD_MIN_SPEECH_DURATION", "0.4"),
            min_silence_duration=_vad_float(vad_cfg, "min_silence_duration", "VAD_MIN_SILENCE_DURATION", "0.45"),
            activation_threshold=_vad_float(vad_cfg, "sensitivity", "VAD_ACTIVATION_THRESHOLD", "0.75"),
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

    # ---- TTS：人设可指定引擎（persona.tts_provider），留空跟随全局 tts.provider。
    # 引擎决定音色池：qwen3_tts 用本地克隆（persona.reference_audio 是本地克隆 ID）；
    # minimax/volcano 是云端，用全局 speaker_zh/speaker_cantonese/en（云端音色 ID），
    # 绝不能把本地克隆 ID 发给云端。fake 出静音测试音。
    global_tts_provider = (tts_cfg.get("provider") or "qwen3_tts").lower()
    persona_tts_provider = ((persona or {}).get("tts_provider") or "").strip().lower()
    tts_provider_name = persona_tts_provider or global_tts_provider
    if persona_tts_provider:
        print(f"[agent] persona tts_provider={persona_tts_provider!r} overrides global {global_tts_provider!r}", flush=True)
    if use_fake or tts_provider_name in ("fake", "fake_tts"):
        # fake = 静音测试音（FakeLiveKitTTS）；绝不落入 Volcano 的 beep 分支。
        tts_provider = FakeLiveKitTTS()
    elif tts_provider_name in ("volcano", "volcano_streaming"):
        tts_provider = VolcanoTTS(
            sample_rate=int(tts_cfg.get("sample_rate") or 24000),
        )
    elif tts_provider_name in ("minimax", "minimax_streaming"):
        # 云端 MiniMax：整场同声——voice 收敛成一个主音色。
        # 人设 reference_audio（{lang: voice_id}，人设页「AI 音色(整场同声)」存的就是
        # zh/cantonese/en 三键同值）优先：取人设主语言对应的音色，统一放 zh 键，无论客户讲
        # 粤/普/英都用同一把声。未绑则回落全局 speaker（也已是单音色）/旧分语言。
        # 兜底防御：过滤本地 Qwen3 音色（预设 9 个 + 克隆 agent-*/acceptance-*），
        # 否则发给 MiniMax 会 2054 voice not exist，整轮无声。
        persona_voice = (persona or {}).get("reference_audio") or ""
        raw_map = _parse_voice_map(persona_voice) if persona_voice else _build_default_voice_map(tts_cfg)
        persona_lang = (persona or {}).get("language") or ""
        # 整场同声必须无条件收敛成单主音色(唔止 persona 绑 voice 嗰阵):以前全局
        # speaker 为空会回落旧分语言 map{zh:普通话音色, cantonese:粤语音色},ASR 一旦判错
        # 一两轮 zh 成个 call 就跳去普通话音色,粤语字读成普通话(「九」→ jiǔ)。
        # 一律按开场锚语言(greet_lang:人设→对象→zh)取主音色,整场唔再逐轮跳。
        anchor_lang = _normalize_lang(persona_lang) or greet_lang or "zh"
        raw_map = _collapse_voice_map(raw_map, anchor_lang)
        _LOCAL_QWEN3 = {
            "serena", "vivian", "uncle_fu", "ryan", "aiden", "ono_anna", "sohee", "eric", "dylan",
        }
        voice_map = {}
        for lang, vid in raw_map.items():
            if not vid:
                continue
            base = str(vid).strip().lower()
            if base in _LOCAL_QWEN3 or base.startswith(("agent-", "acceptance-")):
                print(f"[agent] minimax skip local qwen3 voice {vid!r} for {lang}", flush=True)
                continue
            voice_map[lang] = vid
        tts_provider = MiniMaxTTS(
            voice=voice_map,
            language_state=language_state,
            sample_rate=int(tts_cfg.get("sample_rate") or 24000),
            api_key=str(tts_cfg.get("api_key") or ""),
            # 每轮 mood → MiniMax emotion(安抚/致歉低沉、愤怒郑重、开心轻快),
            # 不然全程一个调听感很平(与 Qwen3 的 instruct_for_mood 同源)。
            emotion_state=emotion_state,
        )
    else:
        if tts_provider_name not in ("", "qwen3_tts"):
            print(f"[agent] unknown tts provider {tts_provider_name!r}, fallback qwen3_tts", flush=True)
        persona_voice = (persona or {}).get("reference_audio") or ""
        if persona_voice:
            # persona 绑定的分语言音色优先（老格式单字符串 → {zh: ...}）。
            voice_map = _parse_voice_map(persona_voice)
        else:
            # 兜底：设置页 speaker_zh/speaker_cantonese/en 组每语言音色。
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

    # 后台预热:首包延迟里冷启动(模型 KV 分配/首次 token 生成)占大头,
    # 发一个极短请求让 MLX warm up;不阻塞 job 启动,失败静默。
    if os.environ.get("LLM_WARMUP", "1") == "1":
        try:
            llm_base = (
                (llm_cfg.get("base_url") or "")
                or os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
            ).rstrip("/")
            llm_model = llm_cfg.get("model") or os.environ.get("MLX_LLM_MODEL", "")

            async def _llm_warmup():
                import httpx

                try:
                    async with httpx.AsyncClient(timeout=10) as c:
                        await c.post(
                            f"{llm_base}/chat/completions",
                            json={
                                "model": llm_model,
                                "messages": [{"role": "user", "content": "hi"}],
                                "max_tokens": 1,
                            },
                        )
                    print("[agent] llm warmup done", flush=True)
                except Exception as exc:  # pragma: no cover - warmup 失败不致命
                    print(f"[agent] llm warmup skipped: {exc!r}", flush=True)

            asyncio.create_task(_llm_warmup())
        except Exception:  # pragma: no cover
            pass
    _log_stage("providers_ready")

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
                # dynamic 自适应，min 0.35s 加速切句；max 收紧到 1.2s——
                # 以前 max 2.0s 会在客户停顿/句子间隙空等最多 2 秒，很"没通话感"。
                "mode": "dynamic",
                "min_delay": float(os.environ.get("ENDPOINT_MIN_DELAY", "0.35")),
                "max_delay": float(os.environ.get("ENDPOINT_MAX_DELAY", "1.2")),
            },
            preemptive_generation={
                # 预生成依赖 ASR 的 interim/preflight 提前量，而 Qwen3-ASR 是离线式
                # (用户整句说完 VAD flush 才出 FINAL_TRANSCRIPT、无 interim)，开了也没有
                # 提前量可抢。保持关闭避免无谓占用。真提速靠缩短 endpointing + 回复变短。
                "enabled": os.environ.get("PREEMPTIVE_GENERATION", "0") == "1",
                "preemptive_tts": os.environ.get("PREEMPTIVE_TTS", "1") == "1",
                "max_speech_duration": 10.0,
                "max_retries": 3,
            },
            interruption={
                "enabled": interruption_enabled,
                # 打断至少要 1.2s 连续人声才算：用户只是连续说话/短句(0.5~1s)不会把
                # AI 正在输出的回复反复掐断(否则 LLM 一直生成、TTS 一直被掐 → 听不到声)。
                # 真插话(客户打断 AI 说较长一句)仍能打断。
                "min_duration": float(os.environ.get("INTERRUPT_MIN_DURATION", "1.2")),
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
        # 渐进披露：每轮按用户当前问题实时检索（本地知识库 + 免费联网检索），
        # 覆盖初始知识，并维护整场对话摘要。联网失败静默降级，绝不阻塞通话。
        try:
            if role == "user":
                from .web_search import web_search_text

                hits = await cp.search_knowledge(text, context_state.account_id, 5)
                context_state.set_knowledge(hits)
                # 联网补充：知识库不足时给 LLM 实时事实（Wikipedia/DDG，按用户语言）。
                # 可用 WEB_SEARCH=0 关闭（隐私/离线场景）。
                if os.environ.get("WEB_SEARCH", "1") == "1":
                    try:
                        web = await web_search_text(text, language_state.lang)
                        if web:
                            context_state.set_web(web)
                    except Exception as exc:  # pragma: no cover - 联网是增强
                        print(f"[agent] web search skipped: {exc!r}", flush=True)
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
                # 只触发异步检索；回复语言由 on_user_turn_completed 锚定后统一注入，
                # 避免这里读到 ASR 原始 lang 抢先注入造成竞态。
                pass
            asyncio.create_task(_async_update_context(role, text))

    # 会话关闭事件：置位后 supervisor watcher 退出、结算触发。
    closed = asyncio.Event()

    def _on_close(ev):
        closed.set()
        asyncio.create_task(cp.settle(call_id))

    session.on("conversation_item_added", _on_conversation_item)
    session.on("conversation_item_added", _on_item_for_context)
    session.on("close", _on_close)

    async def _background_flow_judge(step_at: int, utt: str) -> None:
        """背景跑 LLM 推進判定:唔好喺開聲前同步等(會每輪拖慢),判定完喺下一輪先生效。

        唔會 double-advance:只喺 flow 仲喺 judge 嗰步(step_at)時先落 advance。
        """
        try:
            from .flow import build_judge_messages, parse_judge_output

            jbase = (
                (llm_cfg.get("base_url") or "")
                or os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
            ).rstrip("/")
            jmodel = llm_cfg.get("model") or os.environ.get("MLX_LLM_MODEL", "")
            if not jbase or not jmodel:
                return
            goal, ref = flow_ctrl.current_goal_ref()
            msgs = build_judge_messages(
                current_index=flow_ctrl.current + 1,
                total=len(flow_ctrl.steps),
                overview_lines=flow_ctrl.overview_goal_lines(),
                goal=goal,
                ref=ref,
                next_goal=flow_ctrl.next_goal(),
                user_text=utt,
                facts=flow_ctrl.vars_map,
            )
            jv = parse_judge_output(await _llm_judge(jbase, jmodel, msgs))
            if flow_ctrl.current == step_at and flow_ctrl.has_steps and not flow_ctrl.done:
                if jv == CONFIRM:
                    flow_ctrl.advance()
                    context_state.set_flow_current(flow_ctrl.current_step_text())
                    print(f"[flow] judge(bg)=confirm step={flow_ctrl.current + 1} (call {room_name})", flush=True)
                else:
                    print(f"[flow] judge(bg)={jv} step={step_at + 1} (call {room_name})", flush=True)
        except Exception as exc:  # pragma: no cover - 背景判定失敗唔影響回覆
            print(f"[flow] judge(bg) failed: {exc!r} (call {room_name})", flush=True)
        finally:
            _judge_inflight["step"] = -1

    class PausableAgent(Agent):
        """可被主管台暂停/接管/恢复的 Agent：暂停期间抑制自动回复，但保留转写与历史。

        用 on_user_turn_completed 抛 StopResponse 跳过本轮回复（livekit 会忽略该轮），
        同时在 chat_ctx 里保留用户消息，恢复后上下文不丢。
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.paused = False

        async def on_user_turn_completed(self, turn_ctx, new_message):
            # 同步注入用户语言：on_user_turn_completed 在自动回复生成【之前】被调用，
            # 此时 ASR 已把 language_state 更新为本轮语言。若只挂在 conversation_item_added
            # 事件上，会晚于 LLM 请求发出（竞态）→ 模型收不到本轮粤语指令而回普通话。
            try:
                # 语言锚定（粤语客服始终讲粤语）：默认用锚语言(greet_lang)回复，只有
                # 客户连续多轮明显讲其它语言才跟随。计算出的回复语言写回 language_state，
                # 供 LLM 指令/联网语言使用；TTS 音色已固定不受影响。
                cur = language_state.lang
                _lang_sticky["sticky"], _lang_sticky["streak"] = cur, _lang_sticky.get("streak", 0)
                reply_lang, _lang_sticky["sticky"], _lang_sticky["streak"] = _sticky_reply_language(
                    greet_lang if greet_lang in {"zh", "cantonese", "en"} else "zh",
                    cur,
                    _lang_sticky["sticky"],
                    _lang_sticky["streak"],
                )
                language_state.lang = reply_lang
                context_state.set_user_language(reply_lang)
            except Exception:  # pragma: no cover - 语言注入失败不致命
                pass
            # WhatsApp 对接触发:喺 flow 推进【前】偵測(step context 係舊步/當前步,offered 先啱);
            # 客戶俾號碼(captured)照推下一步;應承加但未俾號碼(offered)→ 唔自動跳,等 AI 叫佢俾號碼。
            _wa_signal: tuple | None = None
            try:
                user_text = ""
                nm = getattr(new_message, "text_content", None) or ""
                user_text = str(nm or "")
                if flow_ctrl.has_steps:
                    _g, _r = flow_ctrl.current_goal_ref()
                    _wa_signal = detect_whatsapp_signal(
                        user_text, step_goal=_g, step_ref=_r, facts=flow_ctrl.vars_map
                    )
                    if _wa_signal:
                        _kind, _num = _wa_signal
                        if _kind == "captured_implicit":
                            # WhatsApp 綁定呢個來電/號碼:號喺系統度,攞對象電話上報 captured。
                            # 對象冇電話 → 上報 offered(操作台見「待對接」);兩種都照推進,唔死鎖。
                            _phone = str((object_card or {}).get("phone") or "").strip()
                            _num = _phone
                            _key = f"implicit:{_phone or '-'}"
                        else:
                            _key = _num or "offered"
                        if _key not in _wa_reported:
                            _wa_reported.add(_key)
                            asyncio.create_task(cp.report_whatsapp(call_id, _num))
                            print(f"[whatsapp] {_kind} num={_num or '-'} (call {room_name})", flush=True)
            except Exception:  # pragma: no cover - WhatsApp 偵測失敗唔阻斷
                pass
            # 流程推进:读用户最新话,判定是否进入下一步,更新"当前步"约束注入。
            if flow_ctrl.has_steps and not flow_ctrl.done:
                try:
                    from .flow import should_auto_advance

                    verdict = flow_ctrl.rule_verdict(user_text)
                    _g2, _r2 = flow_ctrl.current_goal_ref()
                    # 規則級必定推進 override(開場步客已回應 / 核實步答到平台):
                    # 唔靠 LLM judge,防止卡死(offered 應承加嗰輪除外——要等客俾號碼)。
                    _wa_blocks = _wa_signal is not None and _wa_signal[0] == "offered"
                    _auto = (not _wa_blocks) and should_auto_advance(
                        current=flow_ctrl.current,
                        goal=_g2,
                        ref=_r2,
                        user_text=user_text,
                        verdict=verdict,
                        # 兼要攞WhatsApp嘅核實步:要客戶俾咗號碼/應承先推
                        wa=(_wa_signal[0] if _wa_signal else None),
                    )
                    if _auto:
                        flow_ctrl.advance()
                        print(f"[flow] rule=auto step={flow_ctrl.current + 1} (call {room_name})", flush=True)
                    elif verdict == CONFIRM:
                        # offered(應承加但未俾號碼)→ 唔推,停喺辦理步叫佢俾號碼(captured 先推)。
                        if _wa_signal and _wa_signal[0] == "offered":
                            pass
                        else:
                            flow_ctrl.advance()
                            print(f"[flow] rule=confirm step={flow_ctrl.current + 1} (call {room_name})", flush=True)
                    elif verdict in (UNCLEAR, QUESTION) and os.environ.get("FLOW_LLM_ADVANCE", "1") == "1":
                        # 模糊轮(客答唔記得/問後續/開放式回答)→ 背景跑 LLM 判定(唔同步等,
                        # 唔好為咗推進令每輪開聲慢幾秒);判定完若仲喺同一歩就推進,下一輪先生效。
                        _step_at = flow_ctrl.current
                        if _judge_inflight["step"] != _step_at:
                            _judge_inflight["step"] = _step_at
                            asyncio.create_task(_background_flow_judge(_step_at, user_text))
                    context_state.set_flow_current(flow_ctrl.current_step_text())
                except Exception:  # pragma: no cover - 流程推进失败不阻断回复
                    pass
            if self.paused:
                chat_ctx = getattr(self, "chat_ctx", None)
                if chat_ctx is not None and new_message is not None:
                    try:
                        chat_ctx.items.append(new_message)
                    except Exception:  # pragma: no cover - 历史保留失败不致命
                        pass
                raise StopResponse()

        async def on_user_turn_exceeded(self, ev):
            if self.paused:
                raise StopResponse()
            await super().on_user_turn_exceeded(ev)

    agent = PausableAgent(instructions=instructions or "你是 Bok Voice 客服助手。")

    async def _supervisor_watch():
        """轮询通话状态，让主管台的暂停/接管/转人工对 agent 真实生效。

        - escalated 或 status=paused：暂停自动回复并打断当前发言（人工接管会话）；
        - status 回到 active 且未 escalated：恢复自动回复；
        - 房间关闭（closed）或通话已结束：退出轮询（结算由 _on_close 幂等触发）。
        """
        while not closed.is_set():
            call = None
            try:
                call = await cp.get_call(call_id)
            except Exception:
                call = None
            if call:
                status = str(call.get("status") or "")
                escalated = bool(call.get("escalated_to_human"))
                paused = escalated or status == "paused"
                if paused and not agent.paused:
                    agent.paused = True
                    print(f"[agent] supervisor paused agent ({room_name})", flush=True)
                    try:
                        session.interrupt(force=True)
                    except Exception:  # pragma: no cover - 无正在播放内容时中断抛错
                        pass
                elif not paused and agent.paused:
                    agent.paused = False
                    print(f"[agent] supervisor resumed agent ({room_name})", flush=True)
            try:
                await asyncio.wait_for(closed.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    watch_task = asyncio.create_task(_supervisor_watch())
    # AgentSession 内部已注册 job shutdown callback（自动 aclose），
    # 这里不能提前 close，否则会话在接通后立刻被销毁。
    await session.start(agent=agent, room=ctx.room)
    _log_stage("session_started")

    if not agent.paused:
        # 开场白用开场语言（对象/人设语言决定）；generate_reply 的 instructions 会
        # 在基础指令上叠加，配合 system 里的母语设定让首句即用对的语言。
        greetings = {"zh": "请问有什么可以帮您？", "cantonese": "請問有咩可以幫到你？", "en": "How can I help you?"}
        await session.generate_reply(instructions=greetings.get(greet_lang, greetings["zh"]))
        _log_stage("greeting_queued")

    # session.start 只负责拉起流水线（返回后会话在后台运行）。保持 entrypoint
    # 存活直到房间关闭，supervisor watcher 在此期间持续轮询；_on_close 置位
    # closed 后退出并清理 watcher（结算由 _on_close 幂等触发）。
    try:
        await closed.wait()
    finally:
        watch_task.cancel()


def run_agent() -> None:
    """Start the LiveKit Agent worker (imports are lazy so tests need no livekit)."""
    import sys

    from livekit.agents import WorkerOptions, cli

    # livekit-agents 1.7.x 的 cli.run_app 需要显式子命令（start）。
    if len(sys.argv) == 1:
        sys.argv.append("start")
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
