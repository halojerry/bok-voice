"""垫话时序探针:用户讲完一句后,agent 出声(垫话或回复)必须 <2s。

背景(2026-09-09):垫话改走 BackgroundAudioPlayer out-of-band 音轨(独立 track
`background_audio`,本探针两条音轨都收)。改版前垫话被 speech 队列堵在回复后面,
慢轮纯静音 2.5-4s;改版后垫话 ~700ms 出声,感知首声由回复 TTFT 决定变为垫话
定时器决定。断言:用户推完音频到 agent 首声 <2000ms(垫话 ~0.7-1.2s,回复
warm TTFT+TTFB ~2.5-4s,阈值两边都分开);随后有 ≥1.5s 总语音(回复真来了,
唔係误触噪声)。

用法:python3 scripts/probe_filler_timing.py [lang](默认 cantonese,需 dev 栈在跑)
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import sys
import time
from pathlib import Path

import httpx
import livekit.rtc as rtc

ROOT = Path(__file__).resolve().parents[1]
LIVEKIT_URL = "ws://127.0.0.1:7880"
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
LANG = os.environ.get("FILLER_LANG", "cantonese")
# 垫话 700ms+播报起音 → <2s;旧通道实测 2.5s+,阈值两边都分得开
ONSET_BUDGET_MS = int(os.environ.get("FILLER_ONSET_BUDGET_MS", "2000"))
TEXT = os.environ.get("FILLER_TEXT", "我想問下我張單點解仲未到。")


def frame_rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def tts_pcm(text: str, lang: str) -> bytes:
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{TTS_URL}/v1/audio/speech",
            json={"input": text, "language": lang, "voice": "Vivian", "sample_rate": 16000},
        )
        r.raise_for_status()
        return r.content


async def push_pcm(audio_source: rtc.AudioSource, pcm: bytes) -> None:
    frame_size = 320  # 20ms @16k
    for i in range(0, len(pcm), frame_size * 2):
        chunk = pcm[i : i + frame_size * 2]
        if len(chunk) < frame_size * 2:
            chunk = chunk + b"\0" * (frame_size * 2 - len(chunk))
        frame = rtc.AudioFrame(
            data=chunk,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=len(chunk) // 2,
        )
        await audio_source.capture_frame(frame)


def speech_stats(pcm: bytes, processed: int, threshold: float = 220.0):
    """从 processed 偏移继续统计 (新增语音ms, 新增静音ms, 新消费偏移)。20ms 步进。"""
    new_speech_ms = 0.0
    new_sil_ms = 0.0
    i = processed
    step = 320  # 20ms
    while i + step * 2 <= len(pcm):
        seg = pcm[i : i + step * 2]
        if frame_rms(seg) > threshold:
            new_speech_ms += 20
        else:
            new_sil_ms += 20
        i += step * 2
    return new_speech_ms, new_sil_ms, i


async def main() -> None:
    lang = LANG
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={
            "display_name": f"E2E-fillerprobe-{int(time.time())}",
            "role_template": "buyer",
            "language": lang,
            "background": "filler timing probe",
        },
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas?account_id=acc-001",
        json={"name": "E2E客服", "language": lang, "tone": "礼貌专业"},
        timeout=10,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={
            "account_id": "acc-001",
            "object_id": obj["id"],
            "persona_id": persona["id"],
            "mode": "live",
            "direction": "webrtc",
            "language": lang,
        },
        timeout=10,
    ).json()
    room_name = call["id"]
    resp = httpx.post(
        f"{CONTROL_PLANE_URL}/api/token",
        json={"account_id": "acc-001", "call_id": room_name},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    room = rtc.Room()
    agent_audio = bytearray()
    read_tasks: list[asyncio.Task] = []

    def attach(track):
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        # 主音轨 + 垫话 out-of-band 音轨都收(垫话在独立 background_audio track 上)
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

        read_tasks.append(asyncio.create_task(_read()))

    def on_track(track, publication, participant):
        attach(track)

    room.on("track_subscribed", on_track)

    try:
        await room.connect(data["serverUrl"], data["participantToken"])
        print(f"[filler-probe] joined {room_name} (lang={lang})", flush=True)
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("e2e-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        # 等开场白播完(≥1s 语音 + 3s 尾静音)
        state = {"processed": 0, "speech": 0.0, "silent": 0.0}
        deadline = time.perf_counter() + 40
        while time.perf_counter() < deadline:
            s, sil, state["processed"] = speech_stats(bytes(agent_audio), state["processed"])
            state["speech"] += s
            if state["speech"] >= 1.0 and sil >= 3.0 * 50:  # sil 以 20ms 计
                break
            if state["speech"] < 1.0:
                state["silent"] = 0.0
            await asyncio.sleep(0.1)
        state.update(processed=len(agent_audio) - 640, speech=0.0, silent=0.0)
        await asyncio.sleep(0.5)

        # 推一句用户音频 → 计时 agent 首声
        pcm = tts_pcm(TEXT, lang)
        t0 = time.perf_counter()
        await push_pcm(audio_source, pcm)
        t_pushed = time.perf_counter()

        onset_ms = None
        speech_total = 0.0
        probe = len(agent_audio)
        speech_acc = 0.0
        while time.perf_counter() - t_pushed < 20:
            s, _, probe = speech_stats(bytes(agent_audio), probe)
            speech_acc += s
            if onset_ms is None and speech_acc >= 0.12:  # 连续 ≥120ms 语音=真出声
                onset_ms = (time.perf_counter() - t_pushed) * 1000
            if onset_ms is not None and speech_acc >= 1.5:
                break
            await asyncio.sleep(0.1)

        total_ms = (time.perf_counter() - t_pushed) * 1000
        ok = (
            onset_ms is not None
            and onset_ms < ONSET_BUDGET_MS
            and speech_acc >= 1.5
        )
        print(
            f"FILLER-PROBE {'PASS' if ok else 'FAIL'} "
            f"first_audio_ms={onset_ms:.0f} budget={ONSET_BUDGET_MS} "
            f"speech_total={speech_acc:.2f}s observed_ms={total_ms:.0f}",
            flush=True,
        )
        await asyncio.sleep(2)
        if not ok:
            raise SystemExit(1)
    finally:
        room.off("track_subscribed", on_track)
        for t in read_tasks:
            t.cancel()
        await room.disconnect()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{room_name}/hangup", timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
