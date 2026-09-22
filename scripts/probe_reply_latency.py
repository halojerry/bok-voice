"""真实通话延迟探针：**遮羞布盖住了多少**（2026-09-21）。

用户口径：垫音（垫话）是遮羞布——让客户觉得「没等那么久」。那真实测试要回答三件事：

1. 遮羞布多快出现（`first_onset_ms`：客户停嘴 → agent 第一段出声）；
2. **它盖不住的部分有多大**（`naked_gap_ms`：第一段出声结束 → 真回复开始之间的**静音**）；
3. 真回复到底多晚（`reply_onset_ms`：客户停嘴 → 真回复开始 = **没有遮羞布时的体感**）。

## 为什么从**外部**打时间戳

不插桩 agent、不改共享栈：探针作为客户加入房间，自己推音频、自己按 100ms 采样
agent 两条音轨（主音轨 + 垫话 out-of-band `background_audio`），用能量口径切
「出声段/静音段」。客户侧听到什么，这里就量到什么。

## 口径

- 100ms 采样 `speech_stats`（20ms 帧 RMS≥200 记语音），得到 100ms 粒度的
  出声/静音时间轴；
- **段** = 被 ≥300ms 静音隔开的连续出声块；
- 段 1 = 遮羞布（若日志本窗内有 `BOK_FILLER fired`）否则 = 真回复；
- **裸洞 = 段 2 起点 − 段 1 终点**（= 客户实际听到的静音，遮羞布失效窗口）。

用法（dev 栈在跑、无在途通话）：`python scripts/probe_reply_latency.py [--turns 5]`
对象名走 `E2E-` 前缀族 → 心跳豁免（否则 nudge 会污染测量窗）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

import httpx
import livekit.rtc as rtc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_barge_in import push_pcm, speech_stats, tts_pcm, wait_speech_then_silence  # noqa: E402

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
_CP_HEADERS: dict[str, str] = {}
if os.environ.get("BOK_CP_TOKEN", "").strip():
    _CP_HEADERS["Authorization"] = f"Bearer {os.environ['BOK_CP_TOKEN'].strip()}"
LOG_PATH = Path.home() / "Library/Application Support/BokVoice/logs/agent.log"
LANG = os.environ.get("REPLY_LAT_LANG", "cantonese")
PERSONA_VOICE = os.environ.get("REPLY_LAT_PERSONA_VOICE", "Cantonese_GentleLady")

# 客户话：短、无逗号、无大换气（E2E 句形铁律——逗号/长停顿会被 vad-pause 劈轮）
UTTERANCES = [
    "唔該幫我查下張單",
    "我係拼多多買嘅",
    "點解會唔見咗",
    "我要問下賠幾多",
    "你哋係邊間公司",
    "我唔係好明白",
]
BIN_MS = 100
BURST_GAP_MS = 300  # ≥300ms 静音 = 段边界
WINDOW_S = float(os.environ.get("REPLY_LAT_WINDOW_S", "25"))


def segments(bins: list[bool], t0_ms: float) -> list[tuple[float, float]]:
    """bins → [(start_ms, end_ms)]（相对推流结束）。段 = 被 ≥BURST_GAP 静音隔开的出声块。"""
    out: list[tuple[float, float]] = []
    start: int | None = None
    gap = 0
    for i, on in enumerate(bins):
        if on:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap * BIN_MS >= BURST_GAP_MS:
                end = i - gap
                out.append((t0_ms + start * BIN_MS, t0_ms + (end + 1) * BIN_MS))
                start = None
                gap = 0
    if start is not None:
        out.append((t0_ms + start * BIN_MS, t0_ms + len(bins) * BIN_MS))
    return out


def _log_marks(off: int) -> dict:
    """本窗 agent.log 增量里的关键打点——把「慢在哪」逐轮归因。"""
    out: dict = {"filler_fired": False, "watchdog": False, "eou_ms": None, "tts_ttfb_ms": None,
                 "prewarm_fail": False, "ttft_ms": None}
    if not LOG_PATH.exists():
        return out
    try:
        blob = LOG_PATH.read_bytes()[off:]
    except Exception:  # noqa: BLE001 - 日志读不到不影响测量
        return out
    out["filler_fired"] = b"BOK_FILLER fired" in blob
    out["watchdog"] = b"no assistant audio 4s after commit" in blob
    out["prewarm_fail"] = b"prefix prewarm skipped" in blob
    m = re.search(rb"eou delay=(\d+)ms", blob)
    if m:
        out["eou_ms"] = int(m.group(1))
    m = re.search(rb"tts ttfb=(\d+)ms", blob)
    if m:
        out["tts_ttfb_ms"] = int(m.group(1))
    m = re.search(rb"LLM_TTFT_MS[^\n]*?(\d+)ms", blob)
    if m:
        out["ttft_ms"] = int(m.group(1))
    return out


async def run_turn(audio_source, agent_audio: bytearray, text: str, log_off: int) -> dict:
    probe = len(agent_audio)
    pcm = tts_pcm(text, LANG)
    await push_pcm(audio_source, pcm)
    t_pushed = time.perf_counter()

    bins: list[bool] = []
    while time.perf_counter() - t_pushed < WINDOW_S:
        s, _sil, probe = speech_stats(bytes(agent_audio), probe)
        bins.append(s > 0.04)
        # 收够了就停：出现 ≥2 段 且 第二段已经播了 ≥1.5s
        segs = segments(bins, 0.0)
        if len(segs) >= 2 and (len(bins) * BIN_MS - segs[1][0]) >= 1500:
            break
        await asyncio.sleep(BIN_MS / 1000)

    segs = segments(bins, 0.0)
    marks = _log_marks(log_off)
    fired = marks["filler_fired"]
    res = {
        "text": text,
        "segments": segs,
        "filler_fired": fired,
        "first_onset_ms": segs[0][0] if segs else None,
        **marks,
    }
    if fired and len(segs) >= 2:
        res["naked_gap_ms"] = segs[1][0] - segs[0][1]
        res["reply_onset_ms"] = segs[1][0]
    else:
        # 无垫话：第一段就是真回复
        res["naked_gap_ms"] = None
        res["reply_onset_ms"] = segs[0][0] if segs else None
    return res


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=4)
    args = ap.parse_args()

    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        headers=_CP_HEADERS,
        json={"display_name": f"E2E-latency-{int(time.time())}", "role_template": "buyer",
              "language": LANG, "background": "reply latency probe"},
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas?account_id=acc-001",
        headers=_CP_HEADERS,
        json={"name": "E2E客服", "language": LANG, "tone": "礼貌专业", "reference_audio": PERSONA_VOICE},
        timeout=10,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        headers=_CP_HEADERS,
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
              "mode": "live", "direction": "webrtc", "language": LANG},
        timeout=10,
    ).json()
    room_name = call["id"]
    resp = httpx.post(f"{CONTROL_PLANE_URL}/api/token", headers=_CP_HEADERS,
                      json={"account_id": "acc-001", "call_id": room_name}, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    room = rtc.Room()
    agent_audio = bytearray()
    tasks: list[asyncio.Task] = []

    def attach(track):
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") not in ("roomio_audio", "background_audio"):
            return

        async def _read():
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                async for event in stream:
                    frame = getattr(event, "frame", event)
                    agent_audio.extend(bytes(frame.data))
            finally:
                await stream.aclose()

        tasks.append(asyncio.create_task(_read()))

    room.on("track_subscribed", lambda t, p, pa: attach(t))
    results: list[dict] = []
    try:
        await room.connect(data["serverUrl"], data["participantToken"])
        print(f"[latency-probe] joined {room_name} lang={LANG}", flush=True)
        src_audio = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("e2e-src", src_audio)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        # 等开场白（冷 worker 可能晚到，最多 3 轮）
        st = {"processed": 0, "speech": 0.0, "silent": 0.0}
        for _ in range(3):
            await wait_speech_then_silence(agent_audio, st, need_speech=1.0, need_silence=3.0, timeout=40)
            if st["speech"] >= 1.0:
                break
        assert st["speech"] >= 1.0, "开场白 40s 内没出声（agent 未就绪？）"
        print("[latency-probe] greeting done", flush=True)

        for i in range(args.turns):
            # 每轮前等 agent 真静下来：2.5s 无语音 **且** 这 1s 内没有新音频字节
            # （只按 RMS 静音判会落在长回复句间停顿里，把下一句推进去 → 段切错）
            st.update(processed=len(agent_audio), speech=0.0, silent=0.0)
            await wait_speech_then_silence(agent_audio, st, need_speech=0.0, need_silence=2.5, timeout=30)
            n0 = len(agent_audio)
            await asyncio.sleep(1.0)
            for _ in range(30):
                if len(agent_audio) == n0:
                    break
                n0 = len(agent_audio)
                await asyncio.sleep(0.5)
            log_off = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
            text = UTTERANCES[i % len(UTTERANCES)]
            r = await run_turn(src_audio, agent_audio, text, log_off)
            results.append(r)
            segs = " ".join(f"[{a:.0f}-{b:.0f}]" for a, b in r["segments"])
            print(
                f"  #{i + 1} 「{text}」 首声={_fmt(r['first_onset_ms'])} "
                f"裸洞={_fmt(r['naked_gap_ms'])} 真回复={_fmt(r['reply_onset_ms'])} "
                f"| eou={_fmt(r['eou_ms'])} 垫话={int(r['filler_fired'])} "
                f"watchdog={int(r['watchdog'])} ttft={_fmt(r['ttft_ms'])} 段={segs}",
                flush=True,
            )
        await asyncio.sleep(1)
    finally:
        for t in tasks:
            t.cancel()
        await room.disconnect()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{room_name}/hangup", headers=_CP_HEADERS, timeout=10)
        except Exception:  # noqa: BLE001 - 收线失败不影响读数
            pass

    print("\n=== 汇总（相对「客户推完音频」）===")
    for key, label in (("first_onset_ms", "遮羞布首声"), ("naked_gap_ms", "裸洞(垫话尾→真回复)"),
                       ("reply_onset_ms", "真回复首声(无遮羞布时的体感)")):
        vals = [r[key] for r in results if r.get(key) is not None]
        if vals:
            vals.sort()
            print(f"  {label:<28} n={len(vals)} p50={vals[len(vals) // 2]:.0f}ms max={vals[-1]:.0f}ms "
                  f"各次={[round(v) for v in vals]}")
    nf = sum(1 for r in results if not r["filler_fired"])
    print(f"  未垫话轮次={nf}/{len(results)}（快轮按设计不垫）")
    return 0


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.0f}ms"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
