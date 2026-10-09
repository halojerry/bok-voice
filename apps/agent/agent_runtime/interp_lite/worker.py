"""interp_lite worker：薄 B 线 LiveKit 入口（镜像旧线 worker 契约，CP/前端零感知）。

同槽位契约（与 ``agent_runtime.interpret`` 完全一致，serve 开关 ``BOK_INTERP_LITE=1``
时占 8082/8083）：agent_name=``bok-interp-fwd/rev``（INTERP_DIRECTION 决定）、
``GET :port/worker`` 健康面、dispatch metadata 键（listen_identity/deliver_identity/
source_lang/target_lang/glossary/voices/persona_id）、``trans-<目标语言>`` 具名轨、
订阅权限白名单（deliver 端；fwd 侧补授权 listen 端「听对方听到的翻译」）、
turns line=b 原文/译文分行落库、SessionReport 带 worker 标识、settle 半场闸、
订阅看护自愈。差异只在管线内核（pipeline.py docstring）与 provider 装配
（官方参数档，providers/ 各文件头钉文档）。

cloud-only 姿势：ASR 非 doubao 档/MT 车道非 openai 档 → 本 job 放弃并留日志
（绝不静默回退本地——旧线的回退链是 A 线客服语义，同传试点要的是干净的云姿态）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

# 旧线单源复用（不双轨；这些是跨线共享的业务/账本/收尾件）。
from ..interpret import (
    _caption_text,
    _direction_audio_enabled,
    _exit_stage,
    _lag_turn_timing,
    _LagLedger,
    _parse_session_voices,
    _persona_voice_map,
    _resolve_minimax_model,
    _session_report_payload,
    _spawn_pooled_task,
    _src_track_watch_loop,
    _turn_handling_opts,
)
from ..providers.livekit_plugins import LanguageState, _parse_vocab_terms
from .config import build_instructions, glossary_block, glossary_source_terms, norm_lang, parse_glossary
from .pipeline import InterpPipeline
from .providers import asr_doubao, mt_deepseek, tts_minimax


def _cfg_float(cfg: dict, key: str, env_key: str, default: str) -> float:
    env_raw = os.environ.get(env_key)
    try:
        return float(env_raw if env_raw is not None else (default if cfg.get(key) is None else cfg.get(key)))
    except Exception:  # noqa: BLE001
        return float(default)


async def entrypoint(ctx) -> None:
    from livekit import rtc
    from livekit.agents import (
        Agent,
        AgentSession,
        RoomInputOptions,
        RoomOutputOptions,
        inference,
    )

    from ..control_plane import ControlPlaneClient

    try:
        from bok_voice_obs.sentry_hook import init_sentry as _init_sentry

        _init_sentry("agent-worker")
    except Exception:  # noqa: BLE001 - 观测缺席绝不阻业务
        pass

    meta: dict = {}
    try:
        meta = json.loads(getattr(ctx.job, "metadata", "") or "{}")
    except Exception:  # noqa: BLE001
        meta = {}
    listen_identity = str(meta.get("listen_identity") or "").strip()
    deliver_identity = str(meta.get("deliver_identity") or "").strip()
    source_lang = norm_lang(str(meta.get("source_lang") or "zh"))
    target_lang = norm_lang(str(meta.get("target_lang") or "en"))
    glossary_pairs = parse_glossary(str(meta.get("glossary") or ""))
    session_voices = _parse_session_voices(meta.get("voices"))
    if not listen_identity or not deliver_identity:
        print(f"[interp-lite] job metadata missing identities: {meta!r} — abort", flush=True)
        return

    room = ctx.room
    room_name = room.name
    call_id = room_name
    speaker_role = "me" if listen_identity.startswith("me-") else "other"
    dir_audio = _direction_audio_enabled(speaker_role)
    print(
        f"[interp-lite] room={room_name} listen={listen_identity} deliver={deliver_identity} "
        f"{source_lang}->{target_lang} audio={'on' if dir_audio else 'text-only'}",
        flush=True,
    )

    cp = ControlPlaneClient(os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000"), call_id=call_id)
    settings: dict = {}
    try:
        settings = await cp.get_settings()
    except Exception as exc:  # noqa: BLE001 - 设置失败回默认
        print(f"[interp-lite] settings resolve failed: {exc!r}", flush=True)
    asr_cfg = settings.get("asr", {}) or {}
    tts_cfg = settings.get("tts", {}) or {}
    vad_cfg = settings.get("vad", {}) or {}

    persona_voices: dict = {}
    persona_id = str(meta.get("persona_id") or "").strip()
    if persona_id:
        try:
            persona_voices = _persona_voice_map(await cp.get_persona(persona_id))
            print(f"[interp-lite] persona voice id={persona_id[:16]} (A-line collapse)", flush=True)
        except Exception as exc:  # noqa: BLE001 - 人设拉取失败不阻通话
            print(f"[interp-lite] persona fetch failed id={persona_id[:16]}: {exc!r}", flush=True)

    # ---- ASR（cloud-only：doubao 档，官方参数档见 providers/asr_doubao.py）----
    vad_provider = inference.VAD(
        max_buffered_speech=_cfg_float(vad_cfg, "max_buffered_speech", "VAD_MAX_BUFFERED_SPEECH", "15"),
        min_speech_duration=_cfg_float(vad_cfg, "min_speech_duration", "VAD_MIN_SPEECH_DURATION", "0.15"),
        min_silence_duration=_cfg_float(vad_cfg, "min_silence_duration", "VAD_MIN_SILENCE_DURATION", "0.45"),
        activation_threshold=_cfg_float(vad_cfg, "sensitivity", "VAD_ACTIVATION_THRESHOLD", "0.75"),
    )
    asr_ls = LanguageState()
    asr_ls.lang = source_lang
    from ..agent import asr_hotword_context
    from ..providers.doubao_asr import doubao_asr_enabled

    provider_name = str(asr_cfg.get("provider") or "").strip().lower()
    hot_terms = _parse_vocab_terms(
        asr_hotword_context(
            source_lang, None, extra_hotwords=glossary_source_terms(glossary_pairs), include_industry=False
        )
    )
    stt_provider = asr_doubao.build(asr_cfg, language_state=asr_ls, hotword_terms=hot_terms, vad_=vad_provider)
    if stt_provider is None:
        if not (provider_name in ("doubao", "doubao_asr") and doubao_asr_enabled()):
            print(
                f"[interp-lite] cloud-only posture: asr provider={provider_name!r} or gate off — abort job",
                flush=True,
            )
        else:
            print("[interp-lite] doubao credentials missing — abort job", flush=True)
        return
    print(f"[interp-lite] asr=doubao-lite (nonstream+force_to_speech) lang={source_lang}", flush=True)

    # ---- TTS（MiniMax bidi 复用装配；text-only 方向不装配=零握手浪费）----
    tts_provider = (
        tts_minimax.build(tts_cfg, target_lang, session_voices, persona_voices) if dir_audio else None
    )
    tts_model = _resolve_minimax_model() if dir_audio else ""
    voice_tags = (
        dir_audio
        and os.environ.get("BOK_INTERP_VOICE_TAGS", "1") == "1"
        and "2.8" in tts_model
    )
    if tts_model:
        print(f"[interp-lite] voice_tags {'on' if voice_tags else 'off'} (tts={tts_model})", flush=True)

    # ---- MT（DeepSeek 车道；非 openai 档=本地姿势，拒装）----
    from bok_voice_core.model_routes import PROVIDER_OPENAI, resolve_route

    _route = resolve_route("mt", os.environ, str(settings.get("model_routing_json") or ""))
    if _route.provider != PROVIDER_OPENAI:
        print(f"[interp-lite] mt lane provider={_route.provider!r} — cloud-only, abort job", flush=True)
        return
    mt = mt_deepseek.DeepSeekMT(
        base_url=_route.base_url or mt_deepseek.DEEPSEEK_BASE_DEFAULT,
        model=_route.model or mt_deepseek.DEEPSEEK_MODEL_DEFAULT,
        api_key=_route.api_key or "",
    )
    # 会话 llm 占位（manual 模式永不调用；AgentSession 构造兼容性保险，同车道参数）。
    from ..providers.livekit_plugins import MlxLlmLLM

    llm_placeholder = MlxLlmLLM(
        base_url=mt._base, model=mt._model, api_key=mt._key, enable_thinking=False, max_tokens=512
    )

    _glossary = glossary_block(glossary_pairs)
    if _glossary:
        print(f"[interp-lite] glossary {len(glossary_pairs)} terms -> asr+mt", flush=True)

    turn_handling = _turn_handling_opts()
    session = AgentSession(
        vad=vad_provider,
        stt=stt_provider,
        llm=llm_placeholder,
        tts=tts_provider,
        turn_handling=turn_handling,
    )

    # ---- 账本/落库（口径与旧线逐字节同：line=b、三列时间轴、INTERP_LAG 行）----
    _t0 = time.monotonic()
    _lag = _LagLedger()
    _first_ms = {"ms": 0}
    _ledger_tasks: set = set()

    async def _add_turn(text: str, language: str, latency: int = 0, *, started_ms: int = 0,
                        ended_ms: int = 0, perceived_ms: int = 0) -> None:
        try:
            await cp.add_turn(
                call_id, speaker_role, text, provider="interpret-lite", latency_ms=latency,
                language=language, line="b", speaker=speaker_role,
                started_ms=started_ms, ended_ms=ended_ms, perceived_ms=perceived_ms,
            )
        except Exception as exc:  # noqa: BLE001 - 落库失败不阻翻译
            print(f"[interp-lite] add_turn failed: {exc!r}", flush=True)

    def _on_item(ev) -> None:
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)
        text = str(getattr(item, "text_content", None) or getattr(item, "raw_text_content", "") or "").strip()
        if not text or role != "assistant":
            return
        latency = int(pipeline.last_ms.get("ms") or 0)  # 逐句 MT 时长（done_mt 时覆写）
        rec = _lag.pop_pending()
        if rec is not None:
            started_ms, ended_ms, perceived_ms = _lag_turn_timing(rec, time.monotonic(), _t0)
            print(
                f"[interp-lite] INTERP_LAG src_chars={rec[1]} first_ms={_first_ms.get('ms') or 0} "
                f"queue_ms={pipeline.queue_wait_ms.get('ms') or 0} perceived_ms={perceived_ms}",
                flush=True,
            )
            _first_ms["ms"] = 0
            _spawn_pooled_task(
                _add_turn(
                    f"译文：{_caption_text(text, target_lang)}", target_lang, latency,
                    started_ms=started_ms, ended_ms=ended_ms, perceived_ms=perceived_ms,
                ),
                _ledger_tasks,
                "LEDGER_TASK_ERR",
            )
        else:
            _spawn_pooled_task(
                _add_turn(f"译文：{_caption_text(text, target_lang)}", target_lang, latency),
                _ledger_tasks,
                "LEDGER_TASK_ERR",
            )

    session.on("conversation_item_added", _on_item)

    pipeline = InterpPipeline(
        session, mt, build_instructions(source_lang, target_lang, _glossary),
        target_lang=target_lang, voice_tags=voice_tags, lag=_lag, first_ms=_first_ms,
    )

    def _on_user_input(ev) -> None:
        text = str(getattr(ev, "transcript", "") or "").strip()
        if not text or not getattr(ev, "is_final", False):
            return  # interim 不喂（投机/抢跑=本地档补偿，lite 不带）
        _spawn_pooled_task(_add_turn(f"原文：{text}", source_lang), _ledger_tasks, "LEDGER_TASK_ERR")
        pipeline.enqueue(text)

    session.on("user_input_transcribed", _on_user_input)

    _mt_worker = asyncio.create_task(pipeline.run())

    async def _shutdown() -> None:
        _mt_worker.cancel()
        await _exit_stage("mt_drain", _mt_worker, timeout_s=3.0)
        await _exit_stage("mt_aclose", mt.aclose(), timeout_s=3.0)
        if tts_provider is not None:
            await _exit_stage("tts_aclose", tts_provider.aclose(), timeout_s=3.0)
        if _ledger_tasks:
            await _exit_stage(
                "ledger_flush",
                asyncio.gather(*list(_ledger_tasks), return_exceptions=True),
                timeout_s=5.0,
            )
        report = None
        try:
            report = ctx.make_session_report(session)
        except Exception as exc:  # noqa: BLE001
            print(f"[interp-lite] session report build failed: {exc!r}", flush=True)
        if report is not None:
            await _exit_stage(
                "report", cp.post_session_report(call_id, _session_report_payload(report.to_dict()))
            )
        # 半场结算闸（双 worker 同房：有人仍在=留给最后离场方向；CP 幂等兜底）。
        humans_alive: list[str] = []
        try:
            from livekit.rtc import ParticipantState as _PState

            for _p in room.remote_participants.values():
                if str(_p.identity).startswith("agent-"):
                    continue
                if getattr(_p, "state", None) == _PState.ACTIVE:
                    humans_alive.append(str(_p.identity))
        except Exception:  # noqa: BLE001 - 判定失败回旧行为（照结算）
            humans_alive = []
        if humans_alive:
            print(
                f"[interp-lite] settle deferred (participants still active: {humans_alive[:3]})",
                flush=True,
            )
        else:
            _res = await _exit_stage("settle", cp.settle(call_id))
            if _res is not None:
                print(f"[interp-lite] settled {call_id}", flush=True)
        await _exit_stage("cp_close", cp.aclose())

    ctx.add_shutdown_callback(_shutdown)

    # 官方姿势显式 connect（participant_identity 路径必须先 connect，旧线同坑）。
    await ctx.connect()
    await session.start(
        room=room,
        agent=Agent(instructions=build_instructions(source_lang, target_lang, _glossary)),
        room_input_options=RoomInputOptions(
            participant_identity=listen_identity,
            audio_enabled=True,
            text_enabled=False,
        ),
        room_output_options=RoomOutputOptions(
            audio_enabled=dir_audio,
            audio_track_name=f"trans-{target_lang}",
            # 字幕先出（旧线 Wave 2 同款缺省）：delta 随生成流下发。
            sync_transcription=False,
        ),
    )

    # 「我方输出=对方听到的内容」：译文轨只授权 deliver 端订阅；fwd 侧额外放开
    # listen 端（控制台「听对方听到的翻译」开关）；rev 不对称放开（回声无诉求）。
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
            perms = [
                rtc.ParticipantTrackPermission(
                    participant_identity=deliver_identity, allow_all=False, allowed_track_sids=sids
                )
            ]
            note = ""
            if speaker_role == "me":
                perms.append(
                    rtc.ParticipantTrackPermission(
                        participant_identity=listen_identity, allow_all=False, allowed_track_sids=sids
                    )
                )
                note = f" (+{listen_identity} hear-their-trans)"
            lp.set_track_subscription_permissions(
                allow_all_participants=False, participant_permissions=perms
            )
            print(f"[interp-lite] audio tracks {sids} -> {deliver_identity}{note}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 权限失败退化为全场可听（不阻翻译）
            print(f"[interp-lite] track permissions failed: {exc!r}", flush=True)

    _apply_track_permissions()

    @room.on("local_track_published")
    def _on_local_published(_publication, _participant) -> None:
        _apply_track_permissions()

    @room.on("participant_connected")
    def _on_participant(_participant) -> None:
        _apply_track_permissions()

    closed = asyncio.Event()
    session.on("close", lambda _ev: closed.set())

    # 订阅看护+自愈（call-72112fd7 实证件，旧线单源复用；纯遥测，=0 关）。
    _watch_tasks: set = set()

    if os.environ.get("BOK_INTERP_SRC_TELEMETRY", "1") == "1":
        try:
            interval = float(os.environ.get("BOK_INTERP_SRC_TELEMETRY_S", "10") or 10)
        except ValueError:
            interval = 10.0
        heal = os.environ.get("BOK_INTERP_SRC_HEAL", "1") == "1"
        _spawn_pooled_task(
            _src_track_watch_loop(room, listen_identity, closed, max(interval, 1.0), heal),
            _watch_tasks,
            "SRC_WATCH_ERR",
        )

    try:
        await closed.wait()
    finally:
        pass


def run_interpreter() -> None:
    """启动一个方向的薄线 worker：INTERP_DIRECTION=fwd|rev（与旧线同槽位）。"""
    import sys

    direction = os.environ.get("INTERP_DIRECTION", "fwd")
    if direction not in ("fwd", "rev"):
        raise SystemExit(f"INTERP_DIRECTION must be fwd or rev, got {direction!r}")
    from livekit.agents import WorkerOptions, cli

    if len(sys.argv) == 1:
        sys.argv.append("start")
    ports = {"fwd": 8082, "rev": 8083}
    from ..worker_guard import worker_port_singleton_guard

    worker_port_singleton_guard(ports[direction], f"interp-lite-{direction}")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=f"bok-interp-{direction}",
            port=ports[direction],
            num_idle_processes=1,
            load_threshold=float(os.environ.get("BOK_WORKER_LOAD_THRESHOLD", "0.99") or 0.99),
        )
    )


if __name__ == "__main__":
    run_interpreter()
