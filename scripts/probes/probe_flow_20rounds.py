"""probe_flow_20rounds.py — A 线 20 轮全流程实弹探针。

真话术六步走完（身份→通知→平台→赔偿→办理收号→收尾）+ 离稿/重问/催办压力轮，
逐轮对账 turns（gen/provider/template_step）与 agent.log 车道标记。
前置：栈已起且带探针窗（BOK_LOCAL_TTS=1，本地 TTS :8788 供刺激音频合成）。
Run: .venv312/bin/python scripts/probes/probe_flow_20rounds.py
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


import asyncio
import sys
import time
from pathlib import Path

import httpx
from livekit import rtc

_SCRIPTS = Path(__file__).resolve().parents[1]  # G1c 入桶后 scripts/ 根=parents[1]
sys.path.insert(0, str(_SCRIPTS))

import e2e_real_customer as erc  # noqa: E402  复用骨架:建通/推流/收音/turns/日志窗口

# 20 轮剧本：走满六步 + 三类压力（劈轮收号/快语速/显式重问）
ROUNDS: list[dict] = [
    {"text": "你好", "op": "normal", "note": "开场/身份步"},
    {"text": "我係陳大文", "op": "normal", "note": "身份确认"},
    {"text": "邊個呀？咩事？", "op": "normal", "note": "质疑→通知步"},
    {"text": "係咩，拼多多買嘅", "op": "normal", "note": "平台步"},
    {"text": "咁可以點樣賠啊", "op": "normal", "note": "赔偿直念步"},
    {"text": "咁你即係賠幾多錢", "op": "normal", "note": "金额追问"},
    {"text": "好啦我接受", "op": "normal", "note": "接受→办理步"},
    {"text": "我個WhatsApp係六四三二二二三三", "op": "digits",
     "segments": ["我個WhatsApp係六四三", "二二二三三"], "note": "WA收号(两段真停顿)"},
    {"text": "你會點跟我跟進", "op": "normal", "note": "跟进问"},
    {"text": "咁幾時處理得好呀", "op": "normal", "note": "时效问"},
    {"text": "唔好意思頭先冇聽清，你講多次點賠", "op": "normal", "note": "显式重问(豁免门)"},
    {"text": "我個件幾時送到", "op": "normal", "note": "到货问"},
    {"text": "你哋係邊間公司嚟㗎", "op": "normal", "note": "离稿QA"},
    {"text": "好啦好啦", "op": "normal", "note": "短应承"},
    {"text": "會唔會有短信通知我", "op": "normal", "note": "短信QA"},
    {"text": "唔該晒你", "op": "normal", "note": "感谢"},
    {"text": "咁你快啲搞掂佢", "op": "normal", "note": "催办"},
    {"text": "我趕住出街，快啲啦", "op": "fast", "note": "快语速赶场"},
    {"text": "冇嘢問啦", "op": "normal", "note": "收线前"},
    {"text": "好，拜拜", "op": "normal", "note": "告别→收线"},
]


DIGIT_GAP_S = 0.25  # 收号两段之间的真实停顿（真人报号中途换气）


def join_segments(segments: list[bytes], gap_s: float = DIGIT_GAP_S) -> bytes:
    """把多段合成话音用真静音拼接成一条刺激流（模拟真人分段说话）。

    旧 `split_pcm` 把**一句**合成音频按固定 0.55 比例腰斩——数字串被从中间劈开
    产生垃圾碎片，真实管线本就会正确拒收，于是探针在测一个不存在的问题。现改为
    分别合成两段完整转写（如「我個WhatsApp係六四三」+「二二二三三」），中间垫
    ~250ms 真静音：这既是真人报号中途停顿，也能照常压测 WA 累积/跨段 join-hold。
    """
    if not segments:
        return b""
    silence = b"\x00" * (int(16000 * gap_s) * 2)
    out = segments[0]
    for seg in segments[1:]:
        out = out + silence + seg
    return out


async def run(narrowband: bool = False) -> int:
    call_id, _voice = erc.create_call("cantonese", None)
    log_offset = erc.LOG_PATH.stat().st_size if erc.LOG_PATH.exists() else 0
    print(f"[flow20] call={call_id} (log offset {log_offset})", flush=True)
    if narrowband:
        print("[flow20] narrowband=ON（仅 WA 数字轮走 8k 电话窄带，其余轮恒等）", flush=True)

    # 预合成客户刺激音频
    pcms: dict[int, bytes] = {}
    for i, r in enumerate(ROUNDS):
        if r["op"] == "digits":
            # 两段分别合成（完整转写）→ 真静音拼接；绝不再腰斩单句。
            segs = [erc.tts_pcm(t, "cantonese") for t in r["segments"]]
            pcm = join_segments(segs)
        else:
            pcm = erc.tts_pcm(r["text"], "cantonese")
            if r["op"] == "fast":
                from probe_fast_speech import speedup_pcm

                pcm = speedup_pcm(pcm, 1.4)
        if narrowband and r["op"] == "digits":
            from probe_stimulus import narrowband_pcm

            pcm = narrowband_pcm(pcm)
        pcms[i] = pcm
    print(f"[flow20] 预合成 {len(pcms)} 轮完成", flush=True)

    room = rtc.Room()
    agent_audio = bytearray()
    read_tasks: list[asyncio.Task] = []

    def attach(track) -> None:
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") not in ("roomio_audio", "background_audio"):
            return

        async def _read() -> None:
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                async for event in stream:
                    frame = getattr(event, "frame", event)
                    agent_audio.extend(bytes(frame.data))
            except Exception:
                pass
            finally:
                try:
                    await stream.aclose()
                except Exception:
                    pass

        read_tasks.append(asyncio.get_running_loop().create_task(_read()))

    room.on("track_subscribed", lambda track, _p, _pt: attach(track))
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                attach(track)

    measures: list[dict] = []
    try:
        data = httpx.post(
            f"{erc.CONTROL_PLANE_URL}/api/token",
            json={"account_id": "acc-001", "call_id": call_id},
            timeout=10,
            headers=erc.CP_HEADERS,
        ).json()
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("customer-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        if not await erc.wait_greeting(agent_audio):
            print("[flow20] WARN 开场白未出声，照常推进", flush=True)
        agent_audio.clear()
        await asyncio.sleep(0.5)

        for i, r in enumerate(ROUNDS):
            pcm = pcms[i]
            m = await erc.play_and_listen(audio_source, agent_audio, pcm)
            m["text"] = r["text"]
            m["note"] = r["note"]
            measures.append(m)
            first = f"{m['first_audio_ms'] / 1000:.2f}s" if m.get("first_audio_ms") is not None else "-"
            flag = "✓" if m.get("answered") else "✗哑"
            print(
                f"    轮{i + 1:>2}[{r['note']}] 「{r['text'][:14]}」 → 首声 {first} · 语音 {m.get('speech_s', 0):.1f}s · {flag}",
                flush=True,
            )
            await asyncio.sleep(0.8)
    except Exception as exc:
        print(f"[flow20] 异常中断: {exc!r}", flush=True)
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        for t in read_tasks:
            t.cancel()
        try:
            httpx.post(f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10, headers=erc.CP_HEADERS)
            httpx.post(f"{erc.CONTROL_PLANE_URL}/api/calls/{call_id}/settle", timeout=30, headers=erc.CP_HEADERS)
        except Exception:
            pass

    # ---- 对账 1：逐轮 turns（user→assistant 配对 + 车道 + 步号） ----
    print("\n════════ turns 对账 ════════", flush=True)
    try:
        turns = await erc.fetch_turns(call_id, settle_s=15.0)
    except Exception as exc:
        turns = []
        print(f"fetch_turns 失败: {exc!r}", flush=True)
    pending_user = ""
    for t in turns:
        role = t.get("role")
        if role == "user":
            pending_user = (t.get("transcript") or "")[:22]
        elif role == "assistant":
            step = t.get("template_step")
            print(
                f"  「{pending_user}」→ [{t.get('gen')}/{t.get('provider')}/步{step}] {(t.get('transcript') or '')[:38]!r}",
                flush=True,
            )
            pending_user = ""

    # ---- 对账 2：agent.log 车道标记 ----
    window = ""
    if erc.LOG_PATH.exists():
        with erc.LOG_PATH.open("rb") as f:
            f.seek(log_offset)
            window = f.read().decode("utf-8", errors="ignore")
    bad_markers = ["STALL_DEGRADE", "STALL_BYPASS", "STALL_CLOSE", "LLM_FALLBACK_TEXT",
                   "LLM_FIRST_TOKEN_TIMEOUT", "LLM_LATE_ANSWER", "REPEAT_CROSS_TURN_EMPTY"]
    lane_markers = ["WA captured", "REPEAT_CROSS_TURN", "FLOW_GRAPH", "BRANCH_ACTION",
                    "qa_fastpath", "FAREWELL", "TAIL_REWRITE", "starve-ack", "defer-ack",
                    "followup-ack", "LAYA_JUDGE"]
    print("\n════════ 车道标记 ════════", flush=True)
    bad_hits: list[str] = []
    for mk in bad_markers:
        n = window.count(mk)
        if n:
            bad_hits.append(f"{mk}×{n}")
    lane_hits: list[str] = []
    for mk in lane_markers:
        n = window.count(mk)
        if n:
            lane_hits.append(f"{mk}×{n}")
    print(f"  坏标记: {bad_hits or '无'}", flush=True)
    print(f"  车道:  {lane_hits}", flush=True)
    for line in window.splitlines():
        if "REPEAT_CROSS_TURN" in line or "WA captured" in line or "whatsapp" in line.lower() and "captured" in line.lower():
            print(f"    · {line.strip()[:120]}", flush=True)

    answered = sum(1 for m in measures if m.get("answered"))
    steps_seen = sorted({t.get("template_step") for t in turns if t.get("role") == "assistant" and t.get("template_step")})
    ok = (
        answered == len(ROUNDS)
        and not bad_hits
        and bool(steps_seen)
        and max(steps_seen) >= 5
    )
    print(
        f"\nFLOW20 应答={answered}/{len(ROUNDS)} 步号面={steps_seen} 坏标记={len(bad_hits)} → "
        + ("PASS" if ok else "FAIL"),
        flush=True,
    )
    return 0 if ok else 1


def aggregate_passk(results: list[bool]) -> dict:
    """pass^k 聚合（D3，2026-09-30）：k 次重跑全过才算过——τ-bench 实证单跑
    pass@1 61% 的配置 pass^8 掉到 <25%，单跑 20/20 的置信度被高估。

    passk(n)=len(results)>=n 且前 n 次全 True；返回 {"n","pass1","passk","k"}
    供报告行/退出码消费（k=n=runs 数）。纯函数。"""
    n = len(results)
    return {
        "n": n,
        "pass1": bool(results) and all(results[:1]),
        "k": n,
        "passk": n > 0 and all(results),
    }


if __name__ == "__main__":
    import argparse

    _ap = argparse.ArgumentParser(description="A 线 20 轮全流程实弹探针")
    _ap.add_argument(
        "--narrowband",
        action="store_true",
        help="仅 WA 数字轮走 8k 电话窄带刺激（其余轮恒等）——生产真实感 ASR 测量臂",
    )
    _ap.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="重复跑 N 次报 pass^k（τ-bench 口径：全过才算过）；退出码取聚合",
    )
    _args = _ap.parse_args()
    _results: list[bool] = []
    for _i in range(max(1, _args.repeats)):
        _rc = asyncio.run(run(narrowband=_args.narrowband))
        _results.append(_rc == 0)
        print(f"[passk] run {_i + 1}/{max(1, _args.repeats)} -> {'PASS' if _rc == 0 else 'FAIL'}", flush=True)
    _agg = aggregate_passk(_results)
    print(
        f"[passk] n={_agg['n']} pass1={_agg['pass1']} pass^k={_agg['passk']} (k={_agg['k']})",
        flush=True,
    )
    raise SystemExit(0 if _agg["passk"] else 1)
