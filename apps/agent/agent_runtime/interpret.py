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
import json
import os
import re


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

    2026-09-16 P0 句级出稿:turn_detection 默认 stt + 句级提交(QWEN3_ASR_SENTENCE_COMMIT
    默认 1,见 bok._interp_env)——STT 说话中按句 FINAL+EOS 成轮,翻译+TTS 与源语音
    重叠,同传粒度从「停嘴整段」提前到句级。函数体 import agent.py 拿单源实现
    (kill-switch 配对:TURN_DETECTION≠stt → 句级 FINAL 熄火 + endpointing min_delay
    自动回 ≥0.35 地板),B 线不复制这份逻辑。打断默认关(同传语义:源说话人续讲
    ≠抢话,见 interruption 块注释;BOK_INTERP_INTERRUPT=1 实验档恢复)。"""
    from .agent import _endpointing_delays_from_env, _turn_detection_mode_from_env

    mode = _turn_detection_mode_from_env()
    min_delay, max_delay = _endpointing_delays_from_env()
    opts: dict = {
        "endpointing": {"mode": "dynamic", "min_delay": min_delay, "max_delay": max_delay},
        "preemptive_generation": _preemptive_generation_opts(),
        "interruption": {
            # 同传语义(2026-09-16 E2E 实证 call-b79e1f1b):源说话人继续讲≠抢话——
            # 句级提交后译文在途时源语音续讲,框架按「用户插话」打断会整轮取消
            # 生成中/播报中的译文(fwd 译文被吞,I1/I5 FAIL 根因)。译员不可能被
            # 源说话人打断,后续句排队接续播。BOK_INTERP_INTERRUPT=1 显式恢复
            # A 线打断语义(实验档,勿在生产开)。
            "enabled": os.environ.get("BOK_INTERP_INTERRUPT", "0") == "1",
            "min_duration": float(os.environ.get("INTERRUPT_MIN_DURATION", "0.6")),
            "min_words": 0,
            "resume_false_interruption": os.environ.get("RESUME_FALSE_INTERRUPTION", "1") == "1",
            "false_interruption_timeout": float(os.environ.get("FALSE_INTERRUPTION_TIMEOUT", "1.0")),
        },
    }
    if mode:
        # 唔设 key = 框架默认(EOT 模型),kill-switch 档原样回退,唔整 None 别名分支。
        opts["turn_detection"] = mode
    return opts


def _sidecar_url(cfg_value: str, env_key: str, default: str) -> str:
    """sidecar 地址解析:settings 值 > env > 缺省(去尾部斜杠)。"""
    return (cfg_value or os.environ.get(env_key) or default).rstrip("/")


def _build_llm_provider(llm_cfg: dict, target_lang: str, glossary: str = ""):
    """组装 B 线翻译 LLM:MT 小模型(:1236)优先,回退 DeepSeek 云端 / 主 LLM(:1235)。

    MT 分支按官方 Hy-MT2 推荐采样收窄(setdefault 不抢用户显式 env),MlxLlmLLM
    构造时读进 extra_body;StatelessMTLLM 负责逐句无状态模板化,glossary 非空时
    进 _mt_prompt 术语槽(会话级常量,前缀稳定)。回退开关 = unset MT_LLM_BASE_URL,
    老 DeepSeek/主 LLM 路径原样保留(术语一致性由 instructions 的 glossary 行兜)。
    """
    from .providers.livekit_plugins import DeepSeekLLM, MlxLlmLLM, StatelessMTLLM

    mt_base = os.environ.get("MT_LLM_BASE_URL", "").strip()
    if mt_base:
        # Hy-MT2 官方推荐采样:temperature 0.7 / top_p 0.6 / top_k 20 / 重复惩罚
        # 1.05——翻译要贴原文,采样收窄防小模型自由发挥/复读。
        os.environ.setdefault("LLM_TEMPERATURE", "0.7")
        os.environ.setdefault("LLM_TOP_P", "0.6")
        os.environ.setdefault("LLM_TOP_K", "20")
        os.environ.setdefault("LLM_REPETITION_PENALTY", "1.05")
        print(f"[interp] llm=hy-mt2 base={mt_base}", flush=True)
        # 滚动上下文(默认 0=关,治代词/指代断裂的 A/B 档):非零=带最近 N 对
        # 「源→译」进 MT prompt 上文参考块(LLMA 式)。代价=参考段逐轮位移,
        # prefix 从该段失效(术语槽/模板头仍命中)——延迟影响用
        # scripts/probe_interpret_latency.py 实测后再定默认。
        context_turns = int(os.environ.get("BOK_INTERP_MT_CONTEXT", "0") or 0)
        return StatelessMTLLM(
            MlxLlmLLM(base_url=mt_base, model=os.environ.get("MT_LLM_MODEL", "")),
            target_lang,
            glossary=glossary,
            context_turns=context_turns,
        )

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


def _build_tts_provider(tts_cfg: dict, target_lang: str):
    """组装 B 线 TTS:settings 指定 minimax → 云端 MiniMax;否则本地 Qwen3-TTS 兜底。

    音色锁口音——粤语音色读普/英自然,普通话音色读粤文变广普,故按 target_lang
    三键换音色;B 线默认 turbo 档(agent 场景 <250ms、$60/M),A 线仍 2.8-hd。
    """
    from .providers.livekit_plugins import LanguageState, MiniMaxTTS, Qwen3TTSTTS

    tts_ls = LanguageState()
    tts_ls.lang = target_lang
    provider = (tts_cfg.get("provider") or "qwen3_tts").lower()
    if provider in ("minimax", "minimax_streaming"):
        keymap = {"zh": "speaker_zh", "cantonese": "speaker_cantonese", "en": "speaker_en"}
        # 防御与 A 线 agent.py 同源:设置页误选本地 Qwen3 音色(预设 9 个 + 克隆
        # agent-*/acceptance-*)发给云端 MiniMax 会 2054 voice not exist,逐句 beep。
        local_qwen3 = {
            "serena", "vivian", "uncle_fu", "ryan", "aiden", "ono_anna", "sohee", "eric", "dylan",
        }
        voice_map = {}
        for lang, key in keymap.items():
            vid = str(tts_cfg.get(key) or "").strip()
            base = vid.lower()
            if not base:
                continue
            if base in local_qwen3 or base.startswith(("agent-", "acceptance-")):
                print(f"[interp] minimax skip local qwen3 voice {vid!r} for {lang}", flush=True)
                continue
            voice_map[lang] = vid
        # 设置页没配/被过滤掉的分语言音色用验证过的默认(各语种母语音色,口音不串)。
        voice_map.setdefault("zh", "Chinese (Mandarin)_News_Anchor")
        voice_map.setdefault("cantonese", "Cantonese_crisp_news_anchor_vv2")
        # EN 默认音色 2026-09-07 换:旧 male_english_speaker 已被 MiniMax 下线
        # （每轮 2054 voice id not exist → 目标侧整轮静音,B 线 E2E 实证）,
        # 换成 minimax-voices.ts 里 preview 验证过的 English_magnetic_voiced_man。
        voice_map.setdefault("en", "English_magnetic_voiced_man")
        os.environ.setdefault("MINIMAX_MODEL", "speech-2.6-turbo")
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
    # 数字主导项丢弃/去重/120 字上限/BOK_ASR_HOTWORDS kill-switch 全部复用单源。
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
    llm_provider = _build_llm_provider(llm_cfg, target_lang, glossary=_glossary)
    tts_provider = _build_tts_provider(tts_cfg, target_lang)

    # 轮次判定走 _turn_handling_opts(纯函数):默认 turn_detection=stt + 句级提交,
    # 说话中按句成轮(翻译+TTS 与源语音重叠);kill-switch 配对与 A 线同一对 env。
    turn_handling = _turn_handling_opts()
    _th = turn_handling
    print(
        "[interp] turn_handling: "
        f"endpointing=dynamic({_th['endpointing']['min_delay']}/{_th['endpointing']['max_delay']}) "
        f"preemptive={'on' if _th['preemptive_generation']['enabled'] else 'off'} "
        f"max_retries={_th['preemptive_generation']['max_retries']} "
        f"interruption={'on' if _th['interruption']['enabled'] else 'off(同传语义)'} "
        f"turn_detection={_th.get('turn_detection') or 'default(EOT kill-switch)'}",
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
    _turn_metrics: dict = {}

    def _capture_metrics(ev) -> None:
        m = getattr(ev, "metrics", None)
        if getattr(m, "type", "") == "llm_metrics":
            try:
                _turn_metrics["llm_ttft_ms"] = int(m.ttft * 1000)
            except Exception:  # pragma: no cover
                pass

    session.on("metrics_collected", _capture_metrics)

    async def _add_turn(text: str, language: str, latency: int = 0) -> None:
        try:
            await cp.add_turn(
                call_id, speaker_role, text, provider="interpret", latency_ms=latency, language=language,
                line="b", speaker=speaker_role,  # B 线账本:此前缺省误标 line=a(P0 遗留)
            )
        except Exception as exc:  # pragma: no cover - 落库失败不阻翻译
            print(f"[interp] add_turn failed: {exc!r}", flush=True)

    def _on_item(ev) -> None:
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        text = str(getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or "").strip()
        if not text:
            return
        if role == "user":
            last_user["text"] = text
            asyncio.create_task(_add_turn(f"原文：{text}", source_lang))
        elif role == "assistant":
            latency = int(_turn_metrics.get("llm_ttft_ms") or 0)
            asyncio.create_task(_add_turn(f"译文：{text}", target_lang, latency))

    session.on("conversation_item_added", _on_item)

    # 房间断开 → SessionReport(真实 usage) + settle(总结/知识蒸馏/vault,服务端幂等;失败不阻塞退出)。
    async def _shutdown() -> None:
        try:
            report = ctx.make_session_report(session)
            await cp.post_session_report(call_id, report.to_dict())
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
