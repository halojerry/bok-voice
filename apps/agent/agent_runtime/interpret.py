"""双端同声传译 interpreter(B 线 v2):LiveKit 房间 × AgentSession 对话模型。

每个方向一个 worker 进程(agent_name=bok-interp-fwd/rev,由 INTERP_DIRECTION 决定),
复用 A 线同一套会话机制:AgentSession + silero VAD(基线参数同 A 线) +
Qwen3-ASR(源语言钉死) + 翻译 LLM(本地 :1235,「只输出译文」) + Qwen3-TTS(目标语言音色)。

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


def _norm_lang(raw: str, default: str = "zh") -> str:
    key = (raw or "").strip().lower()
    if key in {"zh", "chinese", "mandarin", "普通话", "中文"}:
        return "zh"
    if key in {"cantonese", "yue", "粤", "粤语", "广东话"}:  # yue 只读别名
        return "cantonese"
    if key in {"en", "english", "英语"}:
        return "en"
    return default


def _translation_instructions(src: str, tgt: str) -> str:
    """同传 system 指令(对齐 services/realtime-translation 的 local-openai prompt,
    补电话同传节奏与港式粤语输出规则)。"""
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
    if tgt == "cantonese":
        lines.append(
            "- 港式粵語:輸出繁體中文口語(唔好用書面語/普通話詞),"
            "數字/單號逐個讀寫漢字(7890→七八九零),可自然夾英文詞。"
        )
    return "\n".join(lines)


async def entrypoint(ctx) -> None:
    from livekit import rtc
    from livekit.agents import (
        Agent,
        AgentSession,
        RoomInputOptions,
        RoomOutputOptions,
        TurnHandlingOptions,
        inference,
    )
    from livekit.agents import stt as lk_stt

    from .control_plane import ControlPlaneClient
    from .providers.livekit_plugins import (
        LanguageState,
        MlxLlmLLM,
        Qwen3ASRSTT,
        Qwen3TTSTTS,
    )

    meta: dict = {}
    try:
        meta = json.loads(getattr(ctx.job, "metadata", "") or "{}")
    except Exception:
        meta = {}
    listen_prefix = str(meta.get("listen") or "me-")
    deliver_prefix = str(meta.get("deliver") or "other-")
    source_lang = _norm_lang(str(meta.get("source_lang") or "zh"))
    target_lang = _norm_lang(str(meta.get("target_lang") or "en"))

    room = ctx.room
    room_name = room.name
    call_id = room_name  # 房间名 = call_id(CP 建会话时生成,与 A 线同约定)
    listen_identity = f"{listen_prefix}{room_name}"
    deliver_identity = f"{deliver_prefix}{room_name}"
    speaker_role = "me" if listen_prefix.startswith("me") else "other"

    print(
        f"[interp] room={room_name} listen={listen_identity} deliver={deliver_identity} "
        f"{source_lang}->{target_lang}",
        flush=True,
    )

    cp = ControlPlaneClient(os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"))
    settings: dict = {}
    try:
        settings = await cp.get_settings()
    except Exception as exc:  # pragma: no cover - 设置失败回退默认
        print(f"[interp] settings resolve failed: {exc!r}", flush=True)
    llm_cfg = settings.get("llm", {}) or {}
    asr_cfg = settings.get("asr", {}) or {}
    tts_cfg = settings.get("tts", {}) or {}
    vad_cfg = settings.get("vad", {}) or {}

    def _sidecar(cfg_value: str, env_key: str, default: str) -> str:
        return (cfg_value or os.environ.get(env_key) or default).rstrip("/")

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
    stt_provider = lk_stt.StreamAdapter(
        stt=Qwen3ASRSTT(
            base_url=_sidecar(asr_cfg.get("base_url") or "", "QWEN3_ASR_BASE_URL", "http://127.0.0.1:8787"),
            language_state=asr_ls,
            pin_language=True,
        ),
        vad=vad_provider,
    )

    if (llm_cfg.get("provider") or "local_openai") == "deepseek" and (
        llm_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
    ):
        from .providers.livekit_plugins import DeepSeekLLM

        llm_provider = DeepSeekLLM(
            api_key=llm_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", ""),
            model=llm_cfg.get("model") or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            base_url=llm_cfg.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
    else:
        llm_provider = MlxLlmLLM(
            base_url=llm_cfg.get("base_url") or os.environ.get("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1"),
            model=llm_cfg.get("model") or os.environ.get("MLX_LLM_MODEL", ""),
        )

    # 目标语言音色:设置页全局单音色 speaker 优先;否则分语言
    # speaker_zh/speaker_cantonese/en(键 yue 只读别名)。
    tts_ls = LanguageState()
    tts_ls.lang = target_lang
    voice = str(tts_cfg.get("speaker") or "").strip()
    if not voice:
        keymap = {"zh": "speaker_zh", "cantonese": "speaker_cantonese", "en": "speaker_en"}
        voice = str(tts_cfg.get(keymap.get(target_lang, "speaker_zh")) or "")
        if not voice and target_lang == "cantonese":
            voice = str(tts_cfg.get("speaker_yue") or "")  # 旧数据只读别名
    tts_provider = Qwen3TTSTTS(
        base_url=_sidecar(tts_cfg.get("base_url") or "", "QWEN3_TTS_BASE_URL", "http://127.0.0.1:8788"),
        voice=voice,
        language_state=tts_ls,
        sample_rate=int(tts_cfg.get("sample_rate") or 24000),
    )

    session = AgentSession(
        vad=vad_provider,
        stt=stt_provider,
        llm=llm_provider,
        tts=tts_provider,
        # 端点判定与 A 线同基线(0.35/1.2 dynamic):离线式 ASR 整句返回等得起;
        # 同传不需要 preemptive(A 线同理由:无 interim 提前量可抢)。
        turn_handling=TurnHandlingOptions(
            endpointing={"mode": "dynamic", "min_delay": 0.35, "max_delay": 1.2},
            preemptive_generation={
                "enabled": False,
                "preemptive_tts": False,
                "max_speech_duration": 10.0,
                "max_retries": 3,
            },
            interruption={"enabled": True, "min_duration": 1.2, "min_words": 0},
        ),
    )

    # 落库:译文句到达时把「原文+译文」合成一条 turn(role=说话方)。
    # 原文(user item)先到存 last_user;翻译(assistant item)后到即组合上报——
    # 后续 settle 的总结/蒸馏/vault 落盘直接吃到双语对照文本。
    last_user = {"text": ""}

    def _on_item(ev) -> None:
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        text = str(getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or "").strip()
        if not text:
            return
        if role == "user":
            last_user["text"] = text
        elif role == "assistant":
            src = last_user["text"]

            async def _add(src: str = src, tgt: str = text) -> None:
                try:
                    await cp.add_turn(call_id, speaker_role, f"原文：{src}\n译文：{tgt}", provider="interpret")
                except Exception as exc:  # pragma: no cover - 落库失败不阻翻译
                    print(f"[interp] add_turn failed: {exc!r}", flush=True)

            asyncio.create_task(_add())

    session.on("conversation_item_added", _on_item)

    # 房间断开 → settle(总结/知识蒸馏/vault,服务端幂等;失败不阻塞退出)。
    async def _shutdown() -> None:
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

    await session.start(
        room=room,
        agent=Agent(instructions=_translation_instructions(source_lang, target_lang)),
        room_input_options=RoomInputOptions(
            participant_identity=listen_identity,
            audio_enabled=True,
            text_enabled=False,
        ),
        # 具名译文轨 trans-<目标语言>(前端可按名渲染);订阅权限白名单见下。
        room_output_options=RoomOutputOptions(audio_enabled=True, audio_track_name=f"trans-{target_lang}"),
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
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=f"bok-interp-{direction}"))


if __name__ == "__main__":
    run_interpreter()
