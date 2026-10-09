#!/usr/bin/env python3
"""W7-P4 终极可行性探针·agent 侧入口（官方管线形状，一次性 worker）。

问题（W7 probe P4，2026-10-09）：P1-P3 已把三条腿裸链验完（豆包 SAUC 显式
end_window_size=500 → definite 静音后 549ms；DeepSeek 流式 TTFT p50 ~620ms；
裸链 DeepSeek→MiniMax bidi 逐 token 直发 utterance EVS 821-1428ms）。本探针把
同一批现役 provider 装进 livekit-agents 1.8.2 的**官方 AgentSession 管线形状**
（官方 pipeline_translator recipe 形状）跑真房间一轮同传，量框架叠加后的
端到端（音频进→译文音频出）延迟与打断行为：

  S1 单轮 EVS×3、S2 连续语音轮切分、S3 播报中打断（driver=probe_official_session.py）。

与产品 B 线（interpret.py）的形状差异（本探针测的正是这个差异）：
  - turn_detection="stt"（产品 B 线=manual 自驱 MT→say 队列）——框架对 STT
    FINAL 自动成轮；
  - preemptive_generation.enabled=True（产品 B 线同开但 manual 下的消费面不同
    ——这里框架在说话中按 interim 稳定前缀**抢跑 LLM**，FINAL 前可能已发请求）；
  - interruption.enabled=True（产品 B 线恒关——manual 无自动回复可打断）；
  - 翻译契约=Agent instructions（_translation_instructions zh→cantonese 镜像）
    ——不包 StatelessMTLLM，LLM 就是官方 openai 插件直连 DeepSeek。

provider 装配逐字节镜像产品装配点（只读 import，不改产品码）：
  STT=DoubaoSTT（apps/agent providers/doubao_asr.py，凭据=设置库 asr 段；
       **子类注入 end_window_size=500**——P2 定案官方路径轮界必须显式设）；
  LLM=livekit.plugins.openai.LLM（base_url/model/api_key=model_routes resolve_route
       的 mt 车道，现役 DeepSeek 云档；thinking 关闭契约=bok_voice_core.deepseek_llm）；
  TTS=MiniMaxTTS（bidi 长连、Cantonese_GentleLady、language_boost=Chinese,Yue、
       语速 1.2 由插件 minimax_speed_for 自取——音色/boost 构造参数镜像
       interpret._build_tts_provider）；
  VAD=inference.VAD 基线参数（min_silence 0.45 / min_speech 0.15 / threshold 0.75，
       interpret.py 装配点同源）。

打点：全部事件（STT interim/final、LLM 请求发出、LLM/TTS metrics、agent 状态
机、speech_created）以 wall_ms 时间戳写 JSONL（W7P4_EVENTS，driver 传入）——
driver 用 wall_ms 跨进程对齐「音频推完→译文首帧」与「preemptive 是否 FINAL
前发请求」。key 绝不打印。

凭据（env 优先，缺省回设置库只读）：BOK_PROBE_DB 覆盖 DB 路径
（缺省 ~/Library/Application Support/BokVoice/bok_voice.db）。
出站域=livekit 本地 + api.deepseek.com + api.minimax.* + openspeech.bytedance.com。

用法（driver 拉起；单独手跑同款）：
  .venv312/bin/python scripts/probes/w7p4_official_agent.py start
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 产品码只读 import 面：apps/agent（agent_runtime）+ packages/core（bok_voice_core）
for _p in (ROOT / "apps" / "agent", ROOT / "packages" / "core"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

DB = Path(os.environ.get("BOK_PROBE_DB", "")).expanduser() if os.environ.get("BOK_PROBE_DB") else (
    Path.home() / "Library/Application Support/BokVoice/bok_voice.db"
)
END_WINDOW_MS = int(os.environ.get("W7P4_END_WINDOW_MS", "500"))
TARGET_LANG = "cantonese"  # 本探针钉 zh→cantonese（语言值规范拼写）
AGENT_NAME = os.environ.get("W7P4_AGENT_NAME", "bok-w7p4")
WORKER_PORT = int(os.environ.get("W7P4_PORT", "8085"))
EVENTS_PATH = os.environ.get("W7P4_EVENTS", "")

# 出站白名单（Mimosa SSRF 门禁形状）：livekit 本地栈 ws + 豆包 SAUC 云端 wss。
# DeepSeek/MiniMax 端点是产品 provider 内部常量，不经过本文件的变量拼接面。
_LK_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SAUC_ALLOWED_HOSTS = frozenset({"openspeech.bytedance.com"})


def _url_host_ok(url: str, schemes: tuple[str, ...], allowed: frozenset[str]) -> bool:
    """出站校验：钉死 scheme 族 + host 精确匹配，拒凭据注入。"""
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme in schemes
        and (parts.hostname or "").lower() in allowed
        and not parts.username
        and not parts.password
    )

# —— 设置库（只读；key 只取用不打印）——


def load_settings() -> dict:
    """global_settings 三段 JSON 读出（缺列/坏 JSON→空段，装配点显式报错）。"""
    out: dict = {"asr": {}, "tts": {}, "routing": ""}
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT asr_json, tts_json, model_routing_json FROM global_settings LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[w7p4] settings db unavailable ({DB}): {exc!r}", flush=True)
        return out
    if not row:
        return out
    try:
        out["asr"] = json.loads(row[0] or "{}") or {}
    except Exception:  # noqa: BLE001
        out["asr"] = {}
    try:
        out["tts"] = json.loads(row[1] or "{}") or {}
    except Exception:  # noqa: BLE001
        out["tts"] = {}
    out["routing"] = row[2] or ""
    return out


def preflight() -> dict:
    """起 worker 前的装配自检（key 只验在场，绝不打印）。缺腿/出站越白名单 exit 2。"""
    lk_url = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if not _url_host_ok(lk_url, ("ws", "wss"), _LK_ALLOWED_HOSTS):
        raise SystemExit(f"[w7p4] LIVEKIT_URL not in local allowlist: {lk_url!r}")
    s = load_settings()
    asr, tts = s["asr"], s["tts"]
    sauc_ep = str(asr.get("endpoint") or "").strip()
    if sauc_ep and not _url_host_ok(sauc_ep, ("ws", "wss"), _SAUC_ALLOWED_HOSTS):
        raise SystemExit("[w7p4] asr.endpoint host not in SAUC allowlist")
    from bok_voice_core.model_routes import PROVIDER_OPENAI, resolve_route

    route = resolve_route("mt", os.environ, s["routing"])
    problems: list[str] = []
    if str(asr.get("provider") or "").lower() not in ("doubao", "doubao_asr"):
        problems.append(f"asr.provider={asr.get('provider')!r} != doubao")
    if not (
        str(asr.get("api_key") or "").strip()
        or (str(asr.get("app_id") or "").strip() and str(asr.get("access_token") or "").strip())
    ):
        problems.append("asr credentials missing (api_key or app_id+access_token)")
    if str(tts.get("provider") or "").lower() not in ("minimax", "minimax_streaming"):
        problems.append(f"tts.provider={tts.get('provider')!r} != minimax")
    if not (
        os.environ.get("MINIMAX_API_KEY", "").strip() or str(tts.get("api_key") or "").strip()
    ):
        problems.append("tts.api_key missing")
    if route.provider != PROVIDER_OPENAI:
        problems.append(f"mt lane provider={route.provider!r} != openai (需云档 DeepSeek)")
    if problems:
        for p in problems:
            print(f"[w7p4] PREFLIGHT FAIL: {p}", flush=True)
        raise SystemExit(2)
    print(
        f"[w7p4] preflight ok asr=doubao(resource={asr.get('resource_id')}) "
        f"llm=openai({route.model}) tts=minimax end_window_ms={END_WINDOW_MS} "
        f"target={TARGET_LANG}",
        flush=True,
    )
    return s


# —— 时间线（JSONL；wall_ms 跨进程对齐 driver）——


class Timeline:
    def __init__(self, path: str):
        self._fh = open(path, "a", encoding="utf-8") if path else None
        self._t0 = time.time()

    def ev(self, kind: str, **kw) -> None:
        rec = {
            "wall_ms": int(time.time() * 1000),
            "rel_ms": int((time.time() - self._t0) * 1000),
            "ev": kind,
        }
        rec.update({k: v for k, v in kw.items() if v is not None})
        line = json.dumps(rec, ensure_ascii=False)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()
        print(f"[w7p4-ev] {line}", flush=True)


def _last_user_text(chat_ctx) -> str:
    try:
        items = list(getattr(chat_ctx, "items", []) or [])
    except Exception:  # noqa: BLE001
        return ""
    for item in reversed(items):
        if str(getattr(item, "role", "")) == "user":
            text = str(
                getattr(item, "text_content", None)
                or getattr(item, "raw_text_content", "")
                or ""
            ).strip()
            if text:
                return text
    return ""


def _make_doubao_stt(asr_cfg: dict, vad_provider, language_state):
    from agent_runtime.providers.doubao_asr import DoubaoSTT

    class _EndWindowDoubaoSTT(DoubaoSTT):
        # W7-P2 定案：SAUC 不显式设 end_window_size 时服务端缺省 ~3.0s 才
        # definite；显式 w=500 → 静音后 549ms。官方路径轮界必须显式设。
        _end_window_ms = END_WINDOW_MS

        def _config(self) -> dict:  # type: ignore[override]
            cfg = super()._config()
            if self._end_window_ms > 0:
                cfg["request"]["end_window_size"] = int(self._end_window_ms)
            return cfg

    return _EndWindowDoubaoSTT(
        api_key=str(asr_cfg.get("api_key") or "").strip(),
        resource_id=str(asr_cfg.get("resource_id") or "").strip(),
        ws_url=str(asr_cfg.get("endpoint") or "").strip(),
        app_id=str(asr_cfg.get("app_id") or "").strip(),
        access_token=str(asr_cfg.get("access_token") or "").strip(),
        language_state=language_state,
        hotword_terms=[],
        vad_=vad_provider,
        # 官方 recipe 形状=最小参数：B 线专用旋钮（utt_merge/clause_commit）不传
        # ——它们是客户端分段增强；本探针测「框架原生 stt 轮界」吃多少延迟。
    )


def _make_llm(routing: str, tl: Timeline):
    """官方 openai 插件直连 mt 车道；子类只在 chat() 发出瞬间打 LLM_REQ 点
    （isinstance 安全，session 对 llm 的类型面零影响）。"""
    from livekit.plugins import openai as lk_openai

    from bok_voice_core.deepseek_llm import is_deepseek_endpoint, thinking_extra_body
    from bok_voice_core.model_routes import PROVIDER_OPENAI, resolve_route

    route = resolve_route("mt", os.environ, routing)
    if route.provider != PROVIDER_OPENAI:
        raise SystemExit(f"[w7p4] mt lane not openai: {route.provider!r}")

    class _ProbeOpenAILLM(lk_openai.LLM):
        def chat(self, *, chat_ctx, **kw):  # type: ignore[override]
            tl.ev("llm_req", last_user=_last_user_text(chat_ctx)[:40] or "(empty)")
            return super().chat(chat_ctx=chat_ctx, **kw)

    extra_body: dict = {}
    if is_deepseek_endpoint(route.base_url):
        extra_body.update(thinking_extra_body(route.base_url, ""))
    return _ProbeOpenAILLM(
        base_url=route.base_url,
        model=route.model,
        api_key=route.api_key or "missing",
        temperature=0.7,
        top_p=0.6,
        extra_body=extra_body or None,
    )


def _make_tts(tts_cfg: dict) -> object:
    from agent_runtime.providers.livekit_plugins import LanguageState, MiniMaxTTS

    ls = LanguageState()
    ls.lang = TARGET_LANG
    keymap = {"zh": "speaker_zh", "cantonese": "speaker_cantonese", "en": "speaker_en"}
    voice_map: dict = {}
    for lang, cfg_key in keymap.items():
        vid = str(tts_cfg.get(cfg_key) or "").strip()
        if vid:
            voice_map[lang] = vid
    # 探针缺省钉 Cantonese_GentleLady（任务口径；P3 裸链同款）
    voice_map.setdefault("cantonese", "Cantonese_GentleLady")
    model = (os.environ.get("MINIMAX_MODEL") or "").strip() or "speech-2.8-turbo"
    # language_boost 锁目标语（MiniMax API 外部枚举字面量，同产品 boost_map）
    boost = "Chinese,Yue" if TARGET_LANG == "cantonese" else "Chinese"
    return MiniMaxTTS(
        voice=voice_map,
        language_state=ls,
        sample_rate=int(tts_cfg.get("sample_rate") or 24000),
        api_key=os.environ.get("MINIMAX_API_KEY", "").strip()
        or str(tts_cfg.get("api_key") or ""),
        model_override=model,
        language_boost=boost,
    )


def _turn_handling(preemptive_tts: bool) -> dict:
    """官方管线形状（turn_handling=TurnHandlingOptions 字典形状，1.8.2 官方推荐
    入口；逐键镜像 interpret._turn_handling_opts 的结构，只翻语义位）：
    turn_detection=stt（STT FINAL 成轮）+ 抢跑开 + 打断开。

    preemptive_tts：默认档 False（抢跑只到 LLM，译文不出声）；S4 追嘴档 True
    （说话中投机译文直接合成开播，final 不一致时框架 cancel 在途语音重生成）。
    档位随 dispatch metadata 下发（1.8.2 PreemptiveGenerationOptions 字典形状
    含 preemptive_tts 键，已验证）。"""
    return {
        "turn_detection": "stt",
        "preemptive_generation": {
            "enabled": True,
            "preemptive_tts": bool(preemptive_tts),
            "max_speech_duration": 10.0,
            "max_retries": 3,
        },
        "endpointing": {"mode": "dynamic", "min_delay": 0.25, "max_delay": 0.6},
        "interruption": {
            "enabled": True,
            "min_duration": 0.6,
            "min_words": 0,
            "resume_false_interruption": True,
            "false_interruption_timeout": 1.0,
        },
    }


async def entrypoint(ctx) -> None:
    from livekit.agents import (
        Agent,
        AgentSession,
        RoomInputOptions,
        RoomOutputOptions,
        inference,
    )
    from livekit.agents import stt as lk_stt

    from agent_runtime.interpret import _translation_instructions
    from agent_runtime.providers.livekit_plugins import LanguageState

    tl = Timeline(EVENTS_PATH)
    tl.ev("entrypoint", room=getattr(ctx.room, "name", ""))
    meta: dict = {}
    try:
        meta = json.loads(getattr(ctx.job, "metadata", "") or "{}")
    except Exception:  # noqa: BLE001
        meta = {}
    listen_identity = str(meta.get("listen_identity") or "").strip()
    source_lang = str(meta.get("source_lang") or "zh").strip() or "zh"
    # S4 追嘴档：driver 随 dispatch metadata 下发 preemptive_tts=1。
    # 机制注（1.8.2 audio_recognition.py 实读）：框架 preemptive 只吃
    # PREFLIGHT_TRANSCRIPT 事件（或 vad 档 final）——emit INTERIM_TRANSCRIPT 的
    # 流式 STT 永远不触发（首跑实测 llm_req#=1/轮、preempted=False）。追嘴档
    # 因此同时在 stt_node 扩展点把 INTERIM 重标 PREFLIGHT（官方事件通道，语义
    # 即「preflight mode STT 的 interim」）；这本身是本探针要交付的结论之一。
    preemptive_tts = str(meta.get("preemptive_tts") or "").strip() == "1"
    if not listen_identity:
        print(f"[w7p4] metadata missing listen_identity: {meta!r} — abort", flush=True)
        return

    settings = load_settings()
    asr_cfg, tts_cfg = settings["asr"], settings["tts"]

    # VAD 基线（interpret.py 装配点同源参数；探针不吃设置面覆盖=确定性）
    vad_provider = inference.VAD(
        max_buffered_speech=15,
        min_speech_duration=0.15,
        min_silence_duration=0.45,
        activation_threshold=0.75,
    )
    asr_ls = LanguageState()
    asr_ls.lang = source_lang
    stt_provider = _make_doubao_stt(asr_cfg, vad_provider, asr_ls)
    llm_provider = _make_llm(settings["routing"], tl)
    tts_provider = _make_tts(tts_cfg)
    # prewarm 预连（产品同姿势：bidi 会话入池，首段零握手；失败零影响）
    try:
        pw = tts_provider.prewarm()
        if asyncio.iscoroutine(pw):
            asyncio.get_running_loop().create_task(pw)
    except Exception:  # noqa: BLE001
        pass

    session = AgentSession(
        vad=vad_provider,
        stt=stt_provider,
        llm=llm_provider,
        tts=tts_provider,
        turn_handling=_turn_handling(preemptive_tts),
    )
    tl.ev("arm_configured", preemptive_tts=preemptive_tts, preflight_retag=preemptive_tts)

    class ProbeAgent(Agent):
        """官方 stt_node 扩展点：追嘴档把 INTERIM_TRANSCRIPT 重标 PREFLIGHT_TRANSCRIPT
        （1.8.2 框架 preemptive 的唯一流式触发事件；其余事件逐字节透传）。"""

        def __init__(self, *, instructions: str, retag: bool):
            super().__init__(instructions=instructions)
            self._retag = retag

        async def stt_node(self, audio, model_settings):  # type: ignore[override]
            async for ev in Agent.default.stt_node(self, audio, model_settings):
                if (
                    self._retag
                    and getattr(ev, "type", None) == lk_stt.SpeechEventType.INTERIM_TRANSCRIPT
                ):
                    ev = lk_stt.SpeechEvent(
                        type=lk_stt.SpeechEventType.PREFLIGHT_TRANSCRIPT,
                        request_id=getattr(ev, "request_id", "") or "",
                        alternatives=list(getattr(ev, "alternatives", []) or []),
                        speech_start_time=getattr(ev, "speech_start_time", None),
                        speech_end_time=getattr(ev, "speech_end_time", None),
                    )
                yield ev

    probe_agent = ProbeAgent(
        instructions=_translation_instructions(source_lang, TARGET_LANG, ""),
        retag=preemptive_tts,
    )

    @session.on("user_input_transcribed")
    def _on_uit(ev):
        tl.ev("user_input", text=str(ev.transcript or "")[:48], is_final=bool(ev.is_final))

    @session.on("agent_state_changed")
    def _on_asc(ev):
        tl.ev("agent_state", state=str(ev.new_state))

    @session.on("user_state_changed")
    def _on_usc(ev):
        tl.ev("user_state", state=str(ev.new_state))

    @session.on("conversation_item_added")
    def _on_cia(ev):
        item = ev.item
        tl.ev(
            "item_added",
            role=str(getattr(item, "role", "")),
            text=str(getattr(item, "text_content", "") or "")[:48],
            interrupted=bool(getattr(item, "interrupted", False)),
        )

    @session.on("speech_created")
    def _on_sc(ev):
        tl.ev(
            "speech_created",
            source=str(getattr(ev, "source", "")),
            user_initiated=bool(getattr(ev, "user_initiated", False)),
        )

    @session.on("metrics_collected")
    def _on_metrics(ev):
        m = ev.metrics
        kind = str(getattr(m, "type", ""))
        rec = {"metric": kind}
        if kind == "llm_metrics":
            rec.update(
                ttft_ms=int(getattr(m, "ttft", 0) * 1000),
                cached=int(getattr(m, "prompt_cached_tokens", 0)),
                prompt=int(getattr(m, "prompt_tokens", 0)),
                cancelled=bool(getattr(m, "cancelled", False)),
                speech_id=str(getattr(m, "speech_id", "") or ""),
            )
        elif kind == "tts_metrics":
            rec.update(
                ttfb_ms=int(getattr(m, "ttfb", 0) * 1000),
                chars=int(getattr(m, "characters_count", 0) or 0),
                cancelled=bool(getattr(m, "cancelled", False)),
                speech_id=str(getattr(m, "speech_id", "") or ""),
            )
        elif kind == "interruption_metrics":
            rec.update(num_interruptions=int(getattr(m, "num_interruptions", 0) or 0))
        elif kind == "eou_metrics":
            _delay = getattr(m, "end_of_speech_delay", None)
            rec.update(eou_ms=int(_delay * 1000) if _delay else 0)
        tl.ev("metrics", **rec)

    await ctx.connect()
    tl.ev("connected")
    await session.start(
        room=ctx.room,
        agent=probe_agent,
        room_input_options=RoomInputOptions(
            participant_identity=listen_identity,
            audio_enabled=True,
            text_enabled=False,
        ),
        room_output_options=RoomOutputOptions(
            audio_enabled=True,
            audio_track_name=f"trans-{TARGET_LANG}",
        ),
    )
    tl.ev("session_started")
    # 会话随房间生命周期收线（driver 断开时 room_close 交还框架）
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        tl.ev("entrypoint_done")


def main() -> None:
    parser = argparse.ArgumentParser(description="W7-P4 official AgentSession probe worker")
    parser.add_argument("cmd", nargs="?", default="start", choices=["start"])
    parser.parse_args()
    if len(sys.argv) == 1:
        sys.argv.append("start")
    # 本地栈连接缺省（bok.py env.py 注入 worker 的同款三键；env 显式值优先）
    os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
    os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
    os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
    preflight()
    from livekit.agents import WorkerOptions, cli

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=AGENT_NAME,
            port=WORKER_PORT,
            num_idle_processes=1,
            load_threshold=0.99,
        )
    )


if __name__ == "__main__":
    main()
