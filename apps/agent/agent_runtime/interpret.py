"""双端同声传译 interpreter(B 线 v2):LiveKit 房间 × AgentSession 对话模型。

每个方向一个 worker 进程(agent_name=bok-interp-fwd/rev,由 INTERP_DIRECTION 决定),
复用 A 线同一套会话机制:AgentSession + silero VAD(基线参数同 A 线) +
Qwen3-ASR(源语言钉死) + 翻译 LLM(Hy-MT2 MT 小模型 :1236 逐句无状态优先,
缺省回退主 LLM :1235 / DeepSeek 云端,「只输出译文」) + TTS(目标语言音色,
本地 Qwen3-TTS 兜底 / settings 选 MiniMax 云端)。

- 听谁:RoomInputOptions(participant_identity) —— fwd 听 me-<room>,rev 听 other-<room>。
- 译文给谁:发布译文轨后 set_track_subscription_permissions 幂等白名单——
  fwd 只授权 other-<room>、rev 只授权 me-<room>(「我方输出=对方听到的内容」;
  开源 SFU 在订阅时强制执行;双方原声互听不受影响,权限只约束 agent 自己发的轨)。
- 字幕:AgentSession 自带 lk.transcription 转写流(原文+译文都广播),
  前端 useTranscriptions 渲染双栏——音频定向、字幕全量,各听各的、都看得到。
- 落库/沉淀:译文句到达时 add_turn(role=说话方, transcript="原文：…\\n译文：…");
  房间断开 settle → 总结/知识蒸馏/vault 原文落盘,全套复用 A 线结算链路。

方向与语言对来自 CP 签发 me 端 token 时挂的 RoomAgentDispatch metadata:
  {"listen": "me-", "deliver": "other-", "source_lang": "zh", "target_lang": "en"}
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import time
from collections import deque
from pathlib import Path


def _norm_lang(raw: str, default: str = "zh") -> str:
    key = (raw or "").strip().lower()
    if key in {"zh", "chinese", "mandarin", "普通话", "中文"}:
        return "zh"
    if key in {"cantonese", "粤", "粤语", "广东话"}:
        return "cantonese"
    if key in {"en", "english", "英语"}:
        return "en"
    return default


def _translation_instructions(src: str, tgt: str, glossary: str = "") -> str:
    """同传 system 指令(对齐 services/realtime-translation 的 local-openai prompt,
    补电话同传节奏与港式粤语输出规则)。glossary 非空时追加术语行——回退 LLM
    路径(DeepSeek/主 LLM)的术语一致性挂点;MT 快路(StatelessMTLLM)走
    _mt_prompt 的术语槽,不靠 instructions。"""
    names = {
        "zh": "Mandarin Chinese",
        "cantonese": "Hong Kong Cantonese (港式粤语口語,繁體)",
        "en": "natural spoken English",
    }
    s = names.get(src, src)
    t = names.get(tgt, tgt)
    lines = [
        "You are a professional simultaneous-interpretation engine on a live phone call.",
        f"Translate EVERY user utterance from {s} into {t}.",
        "Rules:",
        "- Output ONLY the translation; no explanations, no quotes, no notes, never the source language.",
        "- Keep names, numbers and technical terms where sensible; preserve the original tone (casual/courteous).",
        "- Speak like a live interpreter: short spoken sentences, one utterance at a time, no summaries.",
        "- If the utterance is already in the target language, output it unchanged.",
    ]
    if glossary:
        lines.append(f"- Glossary (keep these renderings exactly): {glossary}")
    if tgt == "cantonese":
        lines.append(
            "- 港式粵語:輸出繁體中文口語(唔好用書面語/普通話詞),"
            "數字/單號逐個讀寫漢字(7890→七八九零),可自然夾英文詞。"
        )
    return "\n".join(lines)


# MiniMax 2.8 系语气词标记(官方文档 2026-09-16 核实:仅 speech-2.8-hd/2.8-turbo
# 支持;非 2.8 模型会把标记当文本念出来——所以有 _voice_tags_supported 门控)。
_VOICE_TAG_RE = re.compile(
    r"\((?:laughs|chuckle|coughs?|clear-throat|groans|breath|pant|inhale|exhale|gasps?|"
    r"sniffs|sighs?|snorts|burps|lip-smacking|humming|hissing|emm|sneezes)\)",
    re.IGNORECASE,
)
# 引导词→标记:Hy-MT2 会把源文语气词照词翻译(Hahaha/Coughs/Ah),MiniMax 2.8
# 对这些词只会「念字」;say 前换成括号标记,合成层才出真声(笑/咳/叹)。
_VOICE_TAG_LEAD_RE = re.compile(
    r"^\s*((?:(?:ha){2,}|(?:he)+|lol|coughs?|ahem|sighs?|alas)\b)[,;:!.\s]*",
    re.IGNORECASE,
)


def _voice_tags_supported(model: str) -> bool:
    """语气词标记仅 2.8 系合成模型支持(纯函数,单测直喂)。"""
    return "2.8" in (model or "")


def _apply_voice_tags(text: str) -> str:
    """句首语气引导词 → MiniMax 2.8 语气标记(纯函数,单测直喂)。

    Hy-MT2 实测会照词翻译语气(Hahaha/Coughs/…),念出来是假人念稿;换成括号
    标记后 2.8 合成层出真声。只动句首(位置最稳),不认识的中性句原样返回。"""
    m = _VOICE_TAG_LEAD_RE.match(text)
    if not m:
        return text
    word = m.group(1).lower()
    if word.startswith("hah") or word == "lol":
        tag = "(laughs)"
    elif word.startswith("heh"):
        tag = "(chuckle)"
    elif word.startswith(("cough", "ahem")):
        tag = "(coughs)"
    elif word.startswith(("sigh", "alas")):
        tag = "(sighs)"
    else:
        return text
    rest = text[m.end():].lstrip()
    return f"{tag} {rest}" if rest else tag


def _strip_voice_tags(text: str) -> str:
    """剥语气词标记(字幕/落库口径):标记只属合成层,不该出现在读者面前。
    剥后把标点前的悬挂空格收掉(「morning (laughs),」→「morning,」)。"""
    cleaned = _VOICE_TAG_RE.sub("", text)
    cleaned = re.sub(r"\s+([,!?;:.，。？！；：、])", r"\1", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# 术语表分隔符:中英逗号/顿号/分号/换行都收(与 A 线 hotwords 字段同口径)。
_GLOSSARY_SPLIT_RE = re.compile(r"[,，、;；\n]+")
# MT prompt 术语块总长护栏:术语表是会话级常量,进每轮请求前缀——超长吃
# 1.8B MT 模型的 prefill;超限从尾部丢弃(装配期一次性,运行时零开销)。
_GLOSSARY_MAX_CHARS = 400


def parse_glossary(raw: str) -> tuple[tuple[str, str], ...]:
    """解析术语表文本(纯函数,单测用):每条「源=译」或纯词条,分隔符同 hotwords。

    纯词条(无=)表示「源语原样保留」:ASR 热词照收,MT 侧提示保持原词。
    顺序保留(先到先得),空段丢弃;不做大小写归一(专名区分大小写)。
    """
    pairs: list[tuple[str, str]] = []
    for part in _GLOSSARY_SPLIT_RE.split(str(raw or "")):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            src, _, tgt = part.partition("=")
            src, tgt = src.strip(), tgt.strip()
            if src:
                pairs.append((src, tgt))
        else:
            pairs.append((part, ""))
    return tuple(pairs)


def glossary_block(pairs) -> str:
    """渲染 MT prompt 术语块(纯函数):「A=B；C=D」;空对/全部超限→空串(逐字节同旧)。"""
    if not pairs:
        return ""
    parts: list[str] = []
    total = 0
    for src, tgt in pairs:
        piece = f"{src}={tgt}" if tgt else src
        if total + len(piece) + 1 > _GLOSSARY_MAX_CHARS:
            break
        parts.append(piece)
        total += len(piece) + 1
    return "；".join(parts)


def glossary_source_terms(pairs) -> str:
    """ASR 热词侧:逗号拼接源语词条(数字主导项由 asr_hotword_context 统一丢弃)。"""
    return ",".join(src for src, _tgt in pairs)


def _turn_handling_opts() -> dict:
    """B 线 turn_handling 组装(纯函数,单测喂 env 断言;与 A 线同一对 env 单源)。

    2026-09-16 P2 定案:框架模式 **manual**——STT 句级 FINAL 不再走框架自动回复,
    由 worker 的 `user_input_transcribed(is_final=True)` 监听自驱 MT→`session.say()`
    队列。理由(实弹 probe_interp_backlog 实证):框架对「播报中到达的新用户轮」
    只有两条路——打断(current_speech.allow_interruptions=True,整轮取消生成中/
    播报中的译文)或**整轮丢弃**(=False,agent_activity「skipping reply to user
    input, current speech generation cannot be interrupted」,连原文落库都没有;
    低门槛压测 8 句只落 5 句)。两条都唔係译员行为;manual 零丢弃零打断,句子
    进 say 队列串行播,背压归 _PlaybackBacklog 管。kill-switch 配对不变:
    TURN_DETECTION≠stt → 不设 turn_detection(框架回 EOT)+ 插件句级 FINAL 熄火,
    旧行为原样回退。打断恒关(BOK_INTERP_INTERRUPT=1 也不该开——manual 下无
    自动回复可打断,仅余 VAD 音频打断译文的实验危害)。"""
    from .agent import _endpointing_delays_from_env, _turn_detection_mode_from_env

    mode = _turn_detection_mode_from_env()
    min_delay, max_delay = _endpointing_delays_from_env()
    opts: dict = {
        "endpointing": {"mode": "dynamic", "min_delay": min_delay, "max_delay": max_delay},
        "preemptive_generation": _preemptive_generation_opts(),
        "interruption": {
            # 同传语义:manual 模式下框架不再自动回复,此开关只余「音频活动打断
            # 译文」一条路,恒关。字段保留为显式声明+防未来框架行为变化。
            "enabled": False,
            "min_duration": float(os.environ.get("INTERRUPT_MIN_DURATION", "0.6")),
            "min_words": 0,
            "resume_false_interruption": os.environ.get("RESUME_FALSE_INTERRUPTION", "1") == "1",
            "false_interruption_timeout": float(os.environ.get("FALSE_INTERRUPTION_TIMEOUT", "1.0")),
        },
    }
    if mode == "stt":
        # 与 sentence_commit_enabled 同判:TURN_DETECTION=stt(默认)→ B 线走
        # manual 自驱管线;其余值(vad/""→EOT)整族回退框架自动回复旧档——
        # 插件句级 FINAL 同源熄火,不可能出现「manual 模式吃不到句子 FINAL」
        # 的错配(kill-switch 一对 env 同进同退)。
        opts["turn_detection"] = "manual"
    return opts


def _build_mt_context(instructions: str, pairs, text: str):
    """单句翻译用 ChatContext(纯函数,单测直喂):instructions(系统)+滚动「源→译」
    对(旧→新)+当前句(user)。StatelessMTLLM 只读最后一条 user(模板无状态),
    滚动对由 _rolling_pairs 现场重抽做参考块;回退通用 LLM 路径则把 instructions
    与对历史当真实上下文翻译。"""
    from livekit.agents import llm as lk_llm

    ctx = lk_llm.ChatContext()
    if instructions:
        ctx.add_message(role="system", content=[instructions])
    for src_t, tgt_t in pairs:
        ctx.add_message(role="user", content=[src_t])
        ctx.add_message(role="assistant", content=[tgt_t])
    ctx.add_message(role="user", content=[text])
    return ctx


async def _mt_once(llm_provider, ctx, *, timeout_s: float = 15.0) -> str:
    """单句直调翻译 LLM(StatelessMTLLM/通用 LLM 同一入口),超时保护防句堆积。

    conn_options 必显式给——插件内芯直接读 conn_options.max_retry,session 托管
    调用才有默认值,直调传 None 会 AttributeError(max_retry of None)。直调档
    单次尝试不重试:重试是延迟放大器,积压由背压门槛管。"""
    from livekit.agents import APIConnectOptions

    stream = llm_provider.chat(chat_ctx=ctx, conn_options=APIConnectOptions(max_retry=1))
    if inspect.isawaitable(stream):
        stream = await stream
    parts: list[str] = []

    async def _drain() -> None:
        async for chunk in stream:
            delta = getattr(chunk, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                parts.append(content)

    await asyncio.wait_for(_drain(), timeout=timeout_s)
    return "".join(parts).strip()


def _sidecar_url(cfg_value: str, env_key: str, default: str) -> str:
    """sidecar 地址解析:settings 值 > env > 缺省(去尾部斜杠)。"""
    return (cfg_value or os.environ.get(env_key) or default).rstrip("/")


def _mt_model_valid(p: str) -> bool:
    """MT 模型路径门禁(纯函数,单测直喂):必须是真实存在的本地绝对路径。

    mlx_lm server 收到 repo-id/占位符等非本地路径会当 HF hub id 解析,断网时
    持锁挂死整个 server(B 线同传 2/8 FAIL 实弹,MT :1236 全灭)——非绝对路径
    或不在盘一律 False,装配侧跳过 MT 分支走既有回退链。"""
    path = Path((p or "").strip())
    return path.is_absolute() and path.exists()


def _mt_sampling(env_key: str, mt_default: float) -> float:
    """MT 采样档解析(单测直喂):用户显式 env 优先,缺省/非法回落 MT 推荐值。

    旧版用 os.environ.setdefault 下发采样档再由 MlxLlmLLM 构造时读回——同
    worker 先服务过 MT 有效会话后 env 永久驻留,后续会话 MT 失效落回主 LLM
    会带着 MT 采样档跑(主 LLM 期望 0.35,评审 P2-3 跨会话 env 泄漏)。现只在
    构造参数处解析,唔写回进程 env。"""
    raw = (os.environ.get(env_key) or "").strip()
    try:
        return float(raw) if raw else mt_default
    except ValueError:
        return mt_default


def _build_llm_provider(llm_cfg: dict, target_lang: str, glossary: str = ""):
    """组装 B 线翻译 LLM:MT 小模型(:1236)优先,回退 DeepSeek 云端 / 主 LLM(:1235)。

    MT 分支按官方 Hy-MT2 推荐采样收窄,MlxLlmLLM 构造时显式传参(用户显式 env
    优先、唔写回进程 env,防跨会话泄漏——见 _mt_sampling);StatelessMTLLM 负责
    逐句无状态模板化,glossary 非空时进 _mt_prompt 术语槽(会话级常量,前缀稳定)。
    回退开关 = unset MT_LLM_BASE_URL,老 DeepSeek/主 LLM 路径原样保留(术语一致
    性由 instructions 的 glossary 行兜)。MT_LLM_MODEL 须经 _mt_model_valid(本地
    绝对路径且在盘)才进 MT 分支——非法值原样透传会让 mlx_lm server 挂死,跳过
    MT 走回退链 + 日志留值。
    """
    from .providers.livekit_plugins import DeepSeekLLM, MlxLlmLLM, StatelessMTLLM

    mt_base = os.environ.get("MT_LLM_BASE_URL", "").strip()
    mt_model = os.environ.get("MT_LLM_MODEL", "").strip()
    if mt_base and _mt_model_valid(mt_model):
        # Hy-MT2 官方推荐采样:temperature 0.7 / top_p 0.6 / top_k 20 / 重复惩罚
        # 1.05——翻译要贴原文,采样收窄防小模型自由发挥/复读。经构造参数显式下发
        # (用户显式 env 优先),唔再用 env setdefault——那会在同 worker 跨会话驻留,
        # MT 失效落回主 LLM 时采样档跟着泄漏(评审 P2-3)。
        print(f"[interp] llm=hy-mt2 base={mt_base}", flush=True)
        # 滚动上下文(默认 0=关,治代词/指代断裂的 A/B 档):非零=带最近 N 对
        # 「源→译」进 MT prompt 上文参考块(LLMA 式)。代价=参考段逐轮位移,
        # prefix 从该段失效(术语槽/模板头仍命中)——延迟影响用
        # scripts/probe_interpret_latency.py 实测后再定默认。
        context_turns = int(os.environ.get("BOK_INTERP_MT_CONTEXT", "0") or 0)
        return StatelessMTLLM(
            MlxLlmLLM(
                base_url=mt_base,
                model=mt_model,
                temperature=_mt_sampling("LLM_TEMPERATURE", 0.7),
                top_p=_mt_sampling("LLM_TOP_P", 0.6),
                top_k=int(_mt_sampling("LLM_TOP_K", 20)),
                repetition_penalty=_mt_sampling("LLM_REPETITION_PENALTY", 1.05),
            ),
            target_lang,
            glossary=glossary,
            context_turns=context_turns,
        )

    if mt_base:
        # 挂死防线:base 有值但 model 非法(repo-id/占位符/空)——跳过 MT 走既有
        # 回退链(DeepSeek/主 LLM 原逻辑不动),日志留值方便查 env(超 60 字截断)。
        shown = mt_model[:60] + ("…" if len(mt_model) > 60 else "")
        print(f"[interp] mt model invalid ('{shown}') — fallback main LLM", flush=True)

    if (llm_cfg.get("provider") or "local_openai") == "deepseek" and (
        llm_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
    ):
        return DeepSeekLLM(
            api_key=llm_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", ""),
            model=llm_cfg.get("model") or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            base_url=llm_cfg.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
    return MlxLlmLLM(
        base_url=llm_cfg.get("base_url") or os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
        model=llm_cfg.get("model") or os.environ.get("MLX_LLM_MODEL", ""),
    )


# 与 A 线 agent.py _MINIMAX_LOCAL_VOICES 同源（复制不 import：agent 模块装配重，
# interpret 单测要保持轻导入）。设置页/同传页误配本地 Qwen3 音色（预设 9 个 +
# 克隆 agent-*/acceptance-*）发给云端 MiniMax 会 2054 voice not exist，逐句 beep。
_MINIMAX_LOCAL_VOICES = frozenset({
    "serena", "vivian", "uncle_fu", "ryan", "aiden", "ono_anna", "sohee", "eric", "dylan",
})
_MINIMAX_LOCAL_PREFIXES = ("agent-", "acceptance-")


def _cloud_voice(vid: str) -> str:
    """云端 MiniMax 可用音色过滤（纯函数，单测直喂）：本地 Qwen3 音色返回空串，
    有效云端音色原样返回（trim 后）。"""
    raw = (vid or "").strip()
    base = raw.lower()
    if not base:
        return ""
    if base in _MINIMAX_LOCAL_VOICES or base.startswith(_MINIMAX_LOCAL_PREFIXES):
        return ""
    return raw


def _parse_session_voices(raw) -> dict:
    """会话级音色 map 解析（纯函数，单测直喂）：同传页建单 voices_json 经 CP
    dispatch metadata 下发，`{"zh": "voice_id", ...}` → 归一 {lang: voice_id}。

    坏 JSON / 形状不对 / 空值 / 未知语言键 → 丢弃 + 日志，返回空 dict——配置
    错误绝不能打死通话，全链回落设置三键 > 硬编码默认。键经 _norm_lang 归一
    成 zh/cantonese/en 三态。"""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[interp] session voices parse failed: {exc!r} — ignore", flush=True)
        return {}
    if not isinstance(data, dict):
        print(f"[interp] session voices not an object: {type(data).__name__} — ignore", flush=True)
        return {}
    voices: dict = {}
    for key, val in data.items():
        lang = _norm_lang(str(key), default="")
        vid = str(val or "").strip()
        if lang and vid:
            voices[lang] = vid
    return voices


def _build_tts_provider(tts_cfg: dict, target_lang: str, session_voices=None):
    """组装 B 线 TTS:settings 指定 minimax → 云端 MiniMax;否则本地 Qwen3-TTS 兜底。

    音色锁口音——粤语音色读普/英自然,普通话音色读粤文变广普,故按 target_lang
    三键换音色;音色优先级=会话级(同传页建单选定,session_voices)>设置页三键>
    硬编码默认。B 线默认 turbo 档(agent 场景 <250ms、$60/M),A 线仍 2.8-hd。
    """
    from .providers.livekit_plugins import LanguageState, MiniMaxTTS, Qwen3TTSTTS

    tts_ls = LanguageState()
    tts_ls.lang = target_lang
    provider = (tts_cfg.get("provider") or "qwen3_tts").lower()
    if provider in ("minimax", "minimax_streaming"):
        # 防御与 A 线 agent.py 同源:设置页误选本地 Qwen3 音色发给云端会 2054。
        keymap = {"zh": "speaker_zh", "cantonese": "speaker_cantonese", "en": "speaker_en"}
        voice_map = {}
        for lang, key in keymap.items():
            vid = _cloud_voice(str(tts_cfg.get(key) or ""))
            if vid:
                voice_map[lang] = vid
        # 会话级音色最优先,覆盖设置三键;误配本地音色同样过滤 → 回落设置/默认。
        if isinstance(session_voices, str):
            session_voices = _parse_session_voices(session_voices)
        for lang_raw, vid_raw in (session_voices or {}).items():
            lang = _norm_lang(str(lang_raw), default="")
            if not lang:
                continue
            vid = str(vid_raw or "").strip()
            cloud = _cloud_voice(vid)
            if not cloud:
                if vid:
                    print(f"[interp] minimax skip local qwen3 session voice {vid!r} for {lang}", flush=True)
                continue
            if voice_map.get(lang) != cloud:
                print(f"[interp] session voice {lang}: {voice_map.get(lang) or '(default)'} -> {cloud}", flush=True)
            voice_map[lang] = cloud
        # 设置页/会话级都没配的分语言音色用验证过的默认(各语种母语音色,口音不串)。
        voice_map.setdefault("zh", "Chinese (Mandarin)_News_Anchor")
        voice_map.setdefault("cantonese", "Cantonese_crisp_news_anchor_vv2")
        # EN 默认音色 2026-09-07 换:旧 male_english_speaker 已被 MiniMax 下线
        # （每轮 2054 voice id not exist → 目标侧整轮静音,B 线 E2E 实证）,
        # 换成 minimax-voices.ts 里 preview 验证过的 English_magnetic_voiced_man。
        voice_map.setdefault("en", "English_magnetic_voiced_man")
        # B 线默认 2026-09-16 起 2.8-turbo(原 2.6-turbo):语气词标记 (laughs)/
        # (coughs)/(sighs) 仅 2.8 系支持——真人感需求拍板上 2.8,实测代价 ~0.2s
        # 感知 lag(2532→2721ms,预算 3500 内);要快可 MINIMAX_MODEL=speech-2.6-
        # turbo 回退(标记自动熄火)或 BOK_INTERP_VOICE_TAGS=0 只关标记。
        os.environ.setdefault("MINIMAX_MODEL", "speech-2.8-turbo")
        # language_boost 锁目标语,防源语音夹词时合成语种漂移;值是 MiniMax API
        # 的外部枚举字面量(术语门禁白名单单点),唔系语言字段命名。
        boost_map = {"zh": "Chinese", "cantonese": "Chinese,Yue", "en": "English"}
        boost = boost_map.get(target_lang, "")
        if boost:
            os.environ.setdefault("MINIMAX_LANGUAGE_BOOST", boost)
        tts = MiniMaxTTS(
            voice=voice_map,
            language_state=tts_ls,
            sample_rate=int(tts_cfg.get("sample_rate") or 24000),
            api_key=str(tts_cfg.get("api_key") or ""),
        )
        # keep-warm 预连(同 A 线):无事件循环时静默跳过,失败零影响。
        try:
            tts.prewarm()
        except Exception:  # noqa: BLE001
            pass
        return tts
    # 本地 Qwen3-TTS 兜底(离线可用):设置页全局单音色 speaker 优先,否则分语言。
    voice = str(tts_cfg.get("speaker") or "").strip()
    if not voice:
        keymap = {"zh": "speaker_zh", "cantonese": "speaker_cantonese", "en": "speaker_en"}
        voice = str(tts_cfg.get(keymap.get(target_lang, "speaker_zh")) or "")
    return Qwen3TTSTTS(
        base_url=_sidecar_url(tts_cfg.get("base_url") or "", "QWEN3_TTS_BASE_URL", "http://127.0.0.1:8788"),
        voice=voice,
        language_state=tts_ls,
        sample_rate=int(tts_cfg.get("sample_rate") or 24000),
    )


def _preemptive_generation_opts() -> dict:
    """抢跑（preemptive generation）预算（单测直接喂 env 断言，唔使起 worker）。

    max_retries 默认 3（P1 churn 收敛，旧 8 会把误判轮的 prefill 白烧放大）：
    每个 PREFLIGHT 事件 count+1，烧穿后 FINAL 到达只 cancel 不重建 → 译文从零
    生成。PREEMPTIVE_MAX_RETRIES 可回退。
    """
    return {
        "enabled": True,
        "preemptive_tts": False,
        "max_speech_duration": 10.0,
        "max_retries": int(os.environ.get("PREEMPTIVE_MAX_RETRIES", "3")),
    }


def _direction_audio_enabled(speaker_role: str) -> bool:
    """该方向译文是否合成+发布音频(纯函数,单测用)。

    2026-09-12 用户拍板:同传操作台**只听我方译文(fwd TTS,给对方听)**;对方→我
    方向(rev,speaker_role=other)只看双栏字幕,不出声——省一半 MiniMax 合成,
    也令「两路译文分两个扬声器」的需求消失(只剩一路音频)。BOK_INTERP_REV_AUDIO=1
    恢复双向出声(旧双端形态/未来我要听对方译文的场景)。"""
    if speaker_role != "other":
        return True
    return os.environ.get("BOK_INTERP_REV_AUDIO", "0") == "1"


def _session_report_payload(report_dict: dict) -> dict:
    """SessionReport dict + 来源 worker 标识(纯函数,单测直喂)。

    P1-A(2026-09-17 全量 debug):fwd/rev 双 worker 共享同一 call_id、各自产一份
    真实 usage 的 SessionReport,后收尾者曾撞 CP ghost guard 409(报告静默丢失,
    interp-fwd.log 2026-09-16 call-c7a63c17 实证)。CP 端按 worker 维度收多份,
    body 带 worker 字段区分来源;不带 worker 的旧格式(A 线 _close)走原单报告
    ghost guard 语义,行为不变。深拷贝入 payload,绝不 mutate caller 的 dict。
    """
    payload = dict(report_dict)
    payload["worker"] = f"bok-interp-{os.environ.get('INTERP_DIRECTION', 'fwd')}"
    return payload


def _spawn_pooled_task(coro, pool: set, err_tag: str) -> None:
    """fire-and-forget 但入池(2026-09-17 全量 debug P2-A 的通用机制,单测直喂)。

    事件循环对 task 只持弱引用,GC 中途回收=任务静默丢失(B 线纪要账本行丢失
    不可补)。入强引用池 + done-callback 自清(防池无界增长)+ 失败打点 err_tag;
    纯引用+打点,不补重试、不改异常语义。镜像 A 线 agent.py `_spawn_report`。
    """
    task = asyncio.create_task(coro)
    pool.add(task)

    def _done(t: asyncio.Task) -> None:
        pool.discard(t)
        if not t.cancelled() and t.exception() is not None:
            print(f"{err_tag} {t.exception()!r}", flush=True)

    task.add_done_callback(_done)


def _estimate_speech_seconds(text: str, target_lang: str) -> float:
    """粗估译文播报时长(秒,纯函数,单测直喂)。

    zh/cantonese 按字(去标点;MiniMax speed 1.2 ≈ 5 字/秒),en 按词(≈2.6 词/秒)。
    估算只喂背压门槛不求精确;非空地板 0.8s(TTS 起播+段间开销)。"""
    if not text:
        return 0.0
    if target_lang == "en":
        return max(0.8, len(text.split()) / 2.6)
    stripped = "".join(ch for ch in text if ch.isalnum())
    return max(0.8, len(stripped) / 5.0)


class _PlaybackBacklog:
    """译文播放背压——v1 PlaybackScheduler 的 maxBacklogMs 门移植(2026-09-16 P2)。

    句级提交+打断默认关之后,源语语速 > 译文播报速度时,译文在框架 speech 队列
    无限堆积,体感 lag 变雪球(字幕早出声、译文越拖越远)。策略=「追最新」
    (AlignAtt 的 always-attend-latest 哲学,同传译员摘译的工程化形态):估时总量
    超限时从最旧开始 force 中断**未开播**的译文句;队头(当前播报)与最新一条
    永不弃——弃音保字,已生成文本的 chat item 照常落库/进字幕。生成中被取消的
    句=整句摘掉(译员压力下的跳句形态)。

    `BOK_INTERP_BACKLOG=0` 总闸关;`BOK_INTERP_MAX_BACKLOG_S` 调门槛(默认 6s
    ≈通路 lag 2.5s+一句余量)。只在 speech_created 事件判定——队列只在新建句时
    增长;interrupt 必须 force=True(会话打断默认关,非 force 会 RuntimeError)。
    """

    def __init__(self, target_lang: str):
        self._target_lang = target_lang
        try:
            self._max_s = float(os.environ.get("BOK_INTERP_MAX_BACKLOG_S", "6") or 6)
        except ValueError:
            self._max_s = 6.0
        self._pending: list[tuple[object, float]] = []  # [(handle, 估时秒)]
        self.dropped = 0
        self.dropped_est_s = 0.0

    @property
    def enabled(self) -> bool:
        return os.environ.get("BOK_INTERP_BACKLOG", "1") == "1" and self._max_s > 0

    def _est_of(self, handle) -> float:
        texts = []
        for item in getattr(handle, "chat_items", None) or []:
            t = getattr(item, "text_content", None) or getattr(item, "raw_text_content", "")
            if t:
                texts.append(str(t))
        return _estimate_speech_seconds(" ".join(texts), self._target_lang)

    def on_speech_created(self, handle) -> tuple[int, float, int]:
        """登记新译文句并按门槛弃旧。返回 (队列深度, 估时总量秒, 本轮弃句数)。"""
        if not self.enabled:
            return (0, 0.0, 0)
        # 1) 清账:已播完/已取消的句出队(text-only 方向句句秒完,队列天然不积)。
        self._pending = [(h, e) for (h, e) in self._pending if not h.done()]
        # 2) 估时:chat item 尚未生成的按旧值/地板记,下轮决策自动补准。
        refreshed = [(h, self._est_of(h) or e) for (h, e) in self._pending]
        est_new = self._est_of(handle) or 0.8  # 新句此刻多半还没有 chat item
        refreshed.append((handle, est_new))
        self._pending = refreshed
        # 3) 门槛:队头=当前播报永不弃、最新一条永不弃 → 只从 index 1 起弃。
        total = sum(e for _, e in self._pending)
        dropped_now = 0
        while total > self._max_s and len(self._pending) > 2:
            h, e = self._pending.pop(1)
            try:
                h.interrupt(force=True)
            except RuntimeError:  # 已完成/已取消——账面照扣
                pass
            total -= e
            dropped_now += 1
            self.dropped += 1
            self.dropped_est_s += e
        return len(self._pending), total, dropped_now


async def entrypoint(ctx) -> None:
    from livekit import rtc
    from livekit.agents import (
        Agent,
        AgentSession,
        RoomInputOptions,
        RoomOutputOptions,
        inference,
    )
    from livekit.agents import stt as lk_stt

    from .control_plane import ControlPlaneClient
    from .providers.livekit_plugins import (
        LanguageState,
        Qwen3ASRLiveSTT,
        Qwen3ASRSTT,
    )

    meta: dict = {}
    try:
        meta = json.loads(getattr(ctx.job, "metadata", "") or "{}")
    except Exception:
        meta = {}
    # CP /api/token 写入精确 identity(它已知房间名);缺关键 metadata 说明分发
    # 配置不对,无法安全选边,直接放弃本 job。
    listen_identity = str(meta.get("listen_identity") or "").strip()
    deliver_identity = str(meta.get("deliver_identity") or "").strip()
    source_lang = _norm_lang(str(meta.get("source_lang") or "zh"))
    target_lang = _norm_lang(str(meta.get("target_lang") or "en"))
    # 术语表(可选,建单时填,CP 随 dispatch metadata 下发):ASR 热词 + MT prompt
    # 术语槽双路注入,治领域词误听与译名漂移(「无术语表」是 B 线对业界同传的
    # 结构性差距,2026-09-16 调研定案 P0-2)。
    glossary_pairs = parse_glossary(str(meta.get("glossary") or ""))
    # 会话级音色(可选,同传页建单时我方/对方各选一把):voices_json 随 dispatch
    # metadata 下发,_build_tts_provider 组 voice_map 时最优先(> 设置三键 > 默认)。
    session_voices = _parse_session_voices(meta.get("voices"))
    if not listen_identity or not deliver_identity:
        print(
            f"[interp] job metadata missing listen_identity/deliver_identity: {meta!r} — abort",
            flush=True,
        )
        return

    room = ctx.room
    room_name = room.name
    call_id = room_name  # 房间名 = call_id(CP 建会话时生成,与 A 线同约定)
    speaker_role = "me" if listen_identity.startswith("me-") else "other"

    print(
        f"[interp] room={room_name} listen={listen_identity} deliver={deliver_identity} "
        f"{source_lang}->{target_lang} "
        f"audio={'on' if _direction_audio_enabled(speaker_role) else 'text-only'}",
        flush=True,
    )

    cp = ControlPlaneClient(os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"), call_id=call_id)
    settings: dict = {}
    try:
        settings = await cp.get_settings()
    except Exception as exc:  # pragma: no cover - 设置失败回退默认
        print(f"[interp] settings resolve failed: {exc!r}", flush=True)
    llm_cfg = settings.get("llm", {}) or {}
    asr_cfg = settings.get("asr", {}) or {}
    tts_cfg = settings.get("tts", {}) or {}
    vad_cfg = settings.get("vad", {}) or {}

    def _cfg_float(key: str, env_key: str, default: str) -> float:
        env_raw = os.environ.get(env_key)
        try:
            return float(env_raw if env_raw is not None else (default if vad_cfg.get(key) is None else vad_cfg.get(key)))
        except Exception:
            return float(default)

    # 翻译输出按句合成,token 上限放宽(默认 160 是客服短句口径,长句会截断)。
    os.environ.setdefault("LLM_MAX_TOKENS", "512")

    # VAD 基线与 A 线一致(0.45 静音/0.15 起声/0.75 抗噪):压缩端点会让轮次
    # 在整包 ASR 返回前提交,转写被丢——同传同样受此约束。
    vad_provider = inference.VAD(
        max_buffered_speech=_cfg_float("max_buffered_speech", "VAD_MAX_BUFFERED_SPEECH", "15"),
        min_speech_duration=_cfg_float("min_speech_duration", "VAD_MIN_SPEECH_DURATION", "0.15"),
        min_silence_duration=_cfg_float("min_silence_duration", "VAD_MIN_SILENCE_DURATION", "0.45"),
        activation_threshold=_cfg_float("sensitivity", "VAD_ACTIVATION_THRESHOLD", "0.75"),
    )

    # 源语言钉死(用户建房时选定):zh/en/cantonese 都下发 hint——粤语防 auto 误判成
    # 普通话,英语/普通话钉定保证整场识别稳定,不吃 auto 的偶发漂移。
    asr_ls = LanguageState()
    asr_ls.lang = source_lang
    # 热词 context:术语表源语词条(用户建单指定,最贴当前场景)。include_industry
    # =False——A 线行业静态词(单号/运单/赔偿…)是快递客服域,B 线通用同传不吃;
    # 数字主导项丢弃/去重/200 字上限/BOK_ASR_HOTWORDS kill-switch 全部复用单源。
    from .agent import asr_hotword_context

    _asr_hotword_ctx = asr_hotword_context(
        source_lang,
        None,
        extra_hotwords=glossary_source_terms(glossary_pairs),
        include_industry=False,
    )
    _asr_inner = Qwen3ASRSTT(
        base_url=_sidecar_url(asr_cfg.get("base_url") or "", "QWEN3_ASR_BASE_URL", "http://127.0.0.1:8787"),
        language_state=asr_ls,
        pin_language=True,
        hotword_context=_asr_hotword_ctx,
    )
    if os.environ.get("QWEN3_ASR_STREAM", "1") == "1":
        # 同传更要 partial:源语音边说边出稳定前缀 → 抢跑 prefill,译文首句更早。
        stt_provider = Qwen3ASRLiveSTT(stt_=_asr_inner, vad_=vad_provider)
    else:
        stt_provider = lk_stt.StreamAdapter(stt=_asr_inner, vad=vad_provider)

    # 翻译 LLM 与 TTS 组装走模块级纯函数(单测直接喂 cfg,唔使起 worker)。
    _glossary = glossary_block(glossary_pairs)
    if _glossary:
        print(f"[interp] glossary {len(glossary_pairs)} terms -> asr+mt", flush=True)
    tts_provider = _build_tts_provider(tts_cfg, target_lang, session_voices)
    # 语气词标记(2026-09-16 用户拍板):Hy-MT2 会把语气照词翻译(Hahaha/Coughs),
    # say 前由 _apply_voice_tags 换成 MiniMax 2.8 括号标记——合成层出真声(笑/咳/
    # 叹),不再是假人念稿。双门控:模型档(仅 2.8 系支持,非 2.8 会把标记念出来)
    # + env 总闸(BOK_INTERP_VOICE_TAGS=0 关)。标记进 say() 文本,字幕/落库由
    # _strip_voice_tags(译文行)与前端 stripVoiceTags(字幕)剥掉,只活合成层。
    tts_model = os.environ.get("MINIMAX_MODEL", "")
    voice_tags = os.environ.get("BOK_INTERP_VOICE_TAGS", "1") == "1" and _voice_tags_supported(tts_model)
    if tts_model:
        print(f"[interp] voice_tags {'on' if voice_tags else 'off'} (tts={tts_model})", flush=True)
    llm_provider = _build_llm_provider(llm_cfg, target_lang, glossary=_glossary)

    # 轮次判定走 _turn_handling_opts(纯函数):manual 模式 + STT 句级 FINAL 自驱
    # MT→say 队列(P2 定案,函数注释有框架打断/丢弃两条路的实证);kill-switch
    # 配对与 A 线同一对 env(TURN_DETECTION≠stt → 回 EOT 整段档)。
    turn_handling = _turn_handling_opts()
    _th = turn_handling
    print(
        "[interp] turn_handling: "
        f"endpointing=dynamic({_th['endpointing']['min_delay']}/{_th['endpointing']['max_delay']}) "
        f"preemptive={'on' if _th['preemptive_generation']['enabled'] else 'off'} "
        f"max_retries={_th['preemptive_generation']['max_retries']} "
        f"interruption={'on' if _th['interruption']['enabled'] else 'off(同传语义)'} "
        f"turn_detection={_th.get('turn_detection') or 'default(EOT kill-switch)'}"
        f"{'(STT句final自驱MT→say队列)' if _th.get('turn_detection') == 'manual' else ''}",
        flush=True,
    )
    session = AgentSession(
        vad=vad_provider,
        stt=stt_provider,
        llm=llm_provider,
        tts=tts_provider,
        turn_handling=turn_handling,
    )

    # 落库:原文/译文拆成两条 turn(2026-09-07 审计闭环——旧行为合成一条,
    # 无 language 标签、译文延迟无从查)。译文行带 latency;原文行即时落,
    # 译文后到再落——总结/蒸馏按序读仍是对照文本(language 字段区分)。
    last_user = {"text": ""}

    async def _add_turn(text: str, language: str, latency: int = 0) -> None:
        try:
            await cp.add_turn(
                call_id, speaker_role, text, provider="interpret", latency_ms=latency, language=language,
                line="b", speaker=speaker_role,  # B 线账本:此前缺省误标 line=a(P0 遗留)
            )
        except Exception as exc:  # pragma: no cover - 落库失败不阻翻译
            print(f"[interp] add_turn failed: {exc!r}", flush=True)

    # 在途账本任务强引用池(2026-09-17 全量 debug P2-A):GC 中途回收=原文/译文
    # 行静默缺行——B 线纪要账本行丢失不可补,池化首选。机制在模块级
    # _spawn_pooled_task(单测直喂),此处只是入口点作用域的薄包装。
    _ledger_tasks: set = set()

    def _spawn_ledger(coro) -> None:
        _spawn_pooled_task(coro, _ledger_tasks, "LEDGER_TASK_ERR")

    # —— 同传主链(P2 manual 架构):STT 句 final 自驱 MT → session.say() 队列 ——
    # 框架 turn_detection=manual:不再自动回复。框架对「播报中到达的新用户轮」
    # 只有两条路——打断在途译文(allow_interruptions=True)或整轮丢弃(=False,
    # 连原文落库都没有;probe_interp_backlog 实弹 8 句只落 5 句),两条都唔係
    # 译员行为。manual 下句子经 user_input_transcribed(is_final) 进本 worker 的
    # 单消费队列,MT 完一条 say 一条:say 队列串行播,MT 与播报流水线重叠,
    # 积压由 _PlaybackBacklog 门槛追最新弃旧。
    _llm_instructions = _translation_instructions(source_lang, target_lang, _glossary)
    _mt_pairs: deque = deque(maxlen=8)  # (源,译) 滚动对——_rolling_pairs 的参考料
    _mt_latency = {"ms": 0}
    _src_q: asyncio.Queue = asyncio.Queue(maxsize=48)

    def _on_item(ev) -> None:
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        text = str(getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or "").strip()
        if not text:
            return
        if role == "user":
            # manual 模式用户轮不进 chat ctx(原文行由 _on_user_input 即时落);
            # 此分支只兜收线 drain 的残余 user item,不再落库防重复行。
            last_user["text"] = text
        elif role == "assistant":
            latency = int(_mt_latency.get("ms") or 0)
            _spawn_ledger(_add_turn(f"译文：{_strip_voice_tags(text)}", target_lang, latency))

    session.on("conversation_item_added", _on_item)

    async def _mt_say_worker() -> None:
        # 单消费 FIFO:句序=翻译序=播报序。MT 下一句时上一句照播(流水线重叠)。
        while True:
            text = await _src_q.get()
            try:
                t0 = time.perf_counter()
                ctx = _build_mt_context(_llm_instructions, list(_mt_pairs), text)
                translated = await _mt_once(llm_provider, ctx)
                _mt_latency["ms"] = int((time.perf_counter() - t0) * 1000)
                if translated:
                    _mt_pairs.append((text, translated))
                    session.say(_apply_voice_tags(translated) if voice_tags else translated)
                else:
                    print(f"[interp] mt empty for {len(text)} chars, skipped", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单句失败不阻后续
                print(f"[interp] mt/say failed: {exc!r}", flush=True)
            finally:
                _src_q.task_done()

    _mt_worker = asyncio.create_task(_mt_say_worker())

    def _on_user_input(ev) -> None:
        # STT 句级 FINAL 是 manual 模式下唯一句子入口(interim/空串过滤);
        # 原文行即时落库,翻译进单消费队列。
        if not getattr(ev, "is_final", False):
            return
        text = str(getattr(ev, "transcript", "") or "").strip()
        if not text:
            return
        last_user["text"] = text
        _spawn_ledger(_add_turn(f"原文：{text}", source_lang))
        try:
            _src_q.put_nowait(text)
        except asyncio.QueueFull:  # 48 句积压=极端场景,摘最新句防雪崩
            print("[interp] source queue overflow, sentence dropped(摘译)", flush=True)

    session.on("user_input_transcribed", _on_user_input)

    # 译文播放背压(P2):manual 之下译文堆在 say 队列——超门槛从最旧弃起
    # (已生成文本照常进字幕/落库,只弃音)。text-only 方向句句秒完播,队列
    # 天然不积,同一钩子零害。
    backlog = _PlaybackBacklog(target_lang)
    if backlog.enabled:
        print(f"[interp] backlog gate={backlog._max_s:g}s (追最新弃音保字)", flush=True)

    def _on_speech_created(ev) -> None:
        handle = getattr(ev, "speech_handle", None)
        if handle is None or not backlog.enabled:
            return
        depth, est_s, dropped = backlog.on_speech_created(handle)
        if dropped or depth > 1:
            print(
                f"[interp] INTERP_BACKLOG depth={depth} est_ms={int(est_s * 1000)} "
                f"drop={dropped} total_dropped={backlog.dropped}",
                flush=True,
            )

    session.on("speech_created", _on_speech_created)

    # 房间断开 → SessionReport(真实 usage) + settle(总结/知识蒸馏/vault,服务端幂等;失败不阻塞退出)。
    async def _shutdown() -> None:
        _mt_worker.cancel()  # 排空 MT 消费协程(挂队列 get 上,不 cancel 会泄漏到下个 job)
        try:
            await _mt_worker
        except (asyncio.CancelledError, Exception):
            pass
        # 在途账本行先落地再报告/结算(镜像 A 线 _close 的 _report_tasks gather):
        # job teardown 会把裸任务杀掉——原文/译文行丢失不可补。短超时防收尾卡死。
        if _ledger_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(_ledger_tasks), return_exceptions=True), timeout=5.0
                )
            except asyncio.TimeoutError:
                pass
        try:
            report = ctx.make_session_report(session)
            # P1-A(2026-09-17):body 带 worker 来源标识,CP 按方向维度收多份报告
            # (见 _session_report_payload 注释)。
            await cp.post_session_report(call_id, _session_report_payload(report.to_dict()))
        except Exception as exc:
            print(f"[interp] session report failed: {exc!r}", flush=True)
        try:
            await cp.settle(call_id)
            print(f"[interp] settled {call_id}", flush=True)
        except Exception as exc:
            print(f"[interp] settle failed: {exc!r}", flush=True)
        try:
            await cp.aclose()
        except Exception:
            pass

    ctx.add_shutdown_callback(_shutdown)

    # 官方姿势显式 connect:1.8 的 session.start 只把 ctx.connect 挂成后台任务
    # (agent_session.py "automatically connect"),而 RoomIO 建 legacy 转写输出时
    # 会同步访问 room.local_participant——本 worker 传了 RoomInputOptions
    # .participant_identity 必走该路径,不先 connect 必抛 "cannot access local
    # participant before connecting"、job 秒崩(同传零反应根因,2026-09-06)。
    # A 线没传 participant_identity 提前 return 才侥幸不炸。
    await ctx.connect()

    await session.start(
        room=room,
        agent=Agent(instructions=_translation_instructions(source_lang, target_lang, _glossary)),
        room_input_options=RoomInputOptions(
            participant_identity=listen_identity,
            audio_enabled=True,
            text_enabled=False,
        ),
        # 具名译文轨 trans-<目标语言>(前端可按名渲染);订阅权限白名单见下。
        # audio_enabled=False = 文字-only agent:框架跳过 TTS 推理(agent_activity
        # 的 perform_tts_inference 只在 audio_output 非空时跑),译文照常走
        # transcription 通道——对方→我方向默认只看字幕不出声(_direction_audio_enabled)。
        room_output_options=RoomOutputOptions(
            audio_enabled=_direction_audio_enabled(speaker_role),
            audio_track_name=f"trans-{target_lang}",
        ),
    )

    # 「我方输出=对方听到的内容」:本 agent 的译文轨只授权 deliver 端订阅。
    # 发布者单方声明即生效;新发布轨/新加入参与者默认无权限 → 幂等重设三处触发。
    def _apply_track_permissions() -> None:
        try:
            lp = room.local_participant
            sids = [
                pub.sid
                for pub in lp.track_publications.values()
                if getattr(pub, "kind", None) == rtc.TrackKind.KIND_AUDIO
            ]
            if not sids:
                return
            lp.set_track_subscription_permissions(
                allow_all_participants=False,
                participant_permissions=[
                    rtc.ParticipantTrackPermission(
                        participant_identity=deliver_identity,
                        allow_all=False,
                        allowed_track_sids=sids,
                    )
                ],
            )
            print(f"[interp] audio tracks {sids} -> only {deliver_identity}", flush=True)
        except Exception as exc:  # pragma: no cover - 权限失败退化为全场可听(不阻翻译)
            print(f"[interp] track permissions failed: {exc!r}", flush=True)

    _apply_track_permissions()

    @room.on("local_track_published")
    def _on_local_published(_publication, _participant) -> None:
        _apply_track_permissions()

    @room.on("participant_connected")
    def _on_participant(_participant) -> None:
        _apply_track_permissions()

    # 保持 entrypoint 存活直到会话关闭(框架在房间断开时终止 job 并跑 shutdown 回调)。
    closed = asyncio.Event()
    session.on("close", lambda _ev: closed.set())
    try:
        await closed.wait()
    finally:
        pass


def run_interpreter() -> None:
    """启动一个方向的 worker:INTERP_DIRECTION=fwd(听我方/译给对方)|rev(对称)。"""
    import sys

    direction = os.environ.get("INTERP_DIRECTION", "fwd")
    if direction not in ("fwd", "rev"):
        raise SystemExit(f"INTERP_DIRECTION must be fwd or rev, got {direction!r}")
    from livekit.agents import WorkerOptions, cli

    # livekit-agents 1.7.x 的 cli.run_app 需要显式子命令(start),与 A 线 run_agent 同。
    if len(sys.argv) == 1:
        sys.argv.append("start")
    # 与 A 线 main(8081)并存须分端口:三 worker 同抢默认 8081,后绑者 Errno 48
    # 即崩(A 线输掉竞态时每通通话 "Agent did not join the room")。
    ports = {"fwd": 8082, "rev": 8083}
    # C6-2 端口单例守卫(2026-09-13):重复 spawn 良性退出(治 Errno 48 崩溃)。
    from .worker_guard import worker_port_singleton_guard

    worker_port_singleton_guard(ports[direction], f"interp-{direction}")
    cli.run_app(
        WorkerOptions(entrypoint_fnc=entrypoint, agent_name=f"bok-interp-{direction}", port=ports[direction])
    )


if __name__ == "__main__":
    run_interpreter()
