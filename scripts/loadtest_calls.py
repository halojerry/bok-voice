"""A 线并发负载 E2E（2026-09-07 QA 新增）。

N 路并发通话（默认 2/4，机器上限 4-8 路），每路 2 轮：
  - 每路零失败（两轮都有回复语音）
  - turns 落库数正确、call_id 无串号
  - 首响延迟（推流结束→回复语音出现）p50/p95 不劣于单路 2 倍

用法：<venv-python> scripts/loadtest_calls.py [CONC]   # CONC 默认 2
"""
from __future__ import annotations

import asyncio
import math
import os
import statistics
import struct
import sys
import time
import wave
from pathlib import Path

import httpx
from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
LIVEKIT_URL = "ws://127.0.0.1:7880"
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"


def frame_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def read_wav_pcm(path: Path, max_seconds: float = 10.0) -> bytes:
    with wave.open(str(path), "rb") as w:
        n = int(min(w.getnframes(), w.getframerate() * max_seconds))
        return w.readframes(n)


async def wait_speech(agent_audio: bytearray, mark: int, timeout_s: float) -> float:
    """等到出现语音，返回首响秒数（自 mark 起的采集长度换算）；失败 -1。"""
    deadline = time.perf_counter() + timeout_s
    processed = mark
    speech = 0.0
    t0 = time.perf_counter()
    while time.perf_counter() < deadline:
        step = 320
        while processed + step <= len(agent_audio):
            if frame_rms(bytes(agent_audio[processed : processed + step])) >= 200:
                speech += 0.02
            processed += step
        if speech >= 0.6:
            return time.perf_counter() - t0
        await asyncio.sleep(0.1)
    return -1.0


async def one_call(idx: int, pushes: list[bytes]) -> dict:
    ts = int(time.time() * 1000) % 1000000
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"并发{idx}-{ts}", "role_template": "buyer", "language": "cantonese", "background": "load"},
        timeout=15,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": f"并发客服{idx}", "language": "cantonese"}, timeout=15,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
              "mode": "live", "direction": "webrtc", "language": "cantonese"},
        timeout=15,
    ).json()
    call_id = call["id"]
    data = httpx.post(f"{CONTROL_PLANE_URL}/api/token", json={"account_id": "acc-001", "call_id": call_id}, timeout=15).json()
    room = rtc.Room()
    await room.connect(data["serverUrl"], data["participantToken"])
    audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
    src = rtc.LocalAudioTrack.create_audio_track("qa-src", audio_source)
    await room.local_participant.publish_track(
        src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    agent_audio = bytearray()
    latencies: list[float] = []

    async def _read(stream: rtc.AudioStream):
        try:
            async for event in stream:
                frame = getattr(event, "frame", event)
                agent_audio.extend(bytes(frame.data))
        except Exception:
            pass

    def on_track(track, pub, participant):
        if int(track.kind) == int(rtc.TrackKind.KIND_AUDIO) and getattr(track, "name", "") in ("roomio_audio", "background_audio"):
            asyncio.get_running_loop().create_task(_read(rtc.AudioStream(track, sample_rate=16000, num_channels=1)))

    room.on("track_subscribed", on_track)
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                on_track(track, pub, participant)

    ok = True
    note = ""
    try:
        await asyncio.sleep(14)  # 开场白
        for pcm in pushes:
            mark = len(agent_audio)
            chunk = int(16000 * 0.1) * 2
            for i in range(0, len(pcm), chunk):
                seg = pcm[i : i + chunk]
                frame = rtc.AudioFrame(
                    data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2
                )
                await audio_source.capture_frame(frame)
                await asyncio.sleep(0.08)
            first = await wait_speech(agent_audio, mark, 45)
            if first < 0:
                ok = False
                note = "reply timeout"
                break
            latencies.append(first)
            await asyncio.sleep(3)  # 让回复播完，间隔 <8s 心跳窗
    except Exception as exc:
        ok = False
        note = repr(exc)[:80]
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10)
        except Exception:
            pass
    turns = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()
    return {"call_id": call_id, "ok": ok, "note": note, "latencies": latencies, "turns": len(turns)}


async def main() -> int:
    conc = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    zh_pcm = read_wav_pcm(AUDIO_DIR / "zh.wav")
    cantonese_pcm = read_wav_pcm(AUDIO_DIR / "cantonese.wav")
    pushes = [zh_pcm, cantonese_pcm]

    t0 = time.perf_counter()
    results = await asyncio.gather(*(one_call(i, pushes) for i in range(conc)))
    wall = time.perf_counter() - t0

    all_lat = sorted(l for r in results for l in r["latencies"])
    ok_calls = sum(1 for r in results if r["ok"])
    p50 = statistics.median(all_lat) if all_lat else -1
    p95 = all_lat[int(len(all_lat) * 0.95) - 1] if all_lat else -1
    turns_ok = all(r["turns"] >= len(pushes) * 2 for r in results)
    print(f"[load] conc={conc} ok_calls={ok_calls}/{conc} wall={wall:.0f}s first_reply p50={p50:.1f}s p95={p95:.1f}s turns_ok={turns_ok}")
    for r in results:
        print(f"  {r['call_id']}: ok={r['ok']} turns={r['turns']} lat={[round(x,1) for x in r['latencies']]} {r['note']}")
    passed = ok_calls == conc and turns_ok
    print(f"LOADTEST_E2E conc={conc} {'PASSED' if passed else 'FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
