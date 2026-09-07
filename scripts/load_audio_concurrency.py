"""音频链路并发压测：4 路真实通话（各自 call/token/房间）同时进行 3 轮对话。

单机 mlx 设计并发参照 bok.py prompt-cache 注释（4-6 路）。输出每轮
「客户说完→AI 开口」真实口径时延（扣除推音频耗时）与错误率。
运行：<runtime-python> scripts/load_audio_concurrency.py
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import time
import wave
from pathlib import Path

import httpx
from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"
ROADS = int(os.environ.get("LOAD_ROADS", "4"))
TURNS = int(os.environ.get("LOAD_TURNS", "3"))
LANG = os.environ.get("LOAD_LANG", "cantonese")
AUDIO = os.environ.get("LOAD_AUDIO", "cantonese.wav")


def frame_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def read_pcm16(path: Path, max_seconds: float = 4.0) -> bytes:
    with wave.open(str(path), "rb") as w:
        n = int(min(w.getnframes(), w.getframerate() * max_seconds))
        return w.readframes(n)


async def road(idx: int, results: list) -> None:
    lang = LANG
    pcm = read_pcm16(AUDIO_DIR / AUDIO)
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"LOAD-audio-{idx}-{int(time.time())}", "role_template": "buyer", "language": lang},
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas?account_id=acc-001",
        json={"name": "压测客服", "language": lang, "tone": "礼貌专业"},
        timeout=10,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
              "mode": "live", "direction": "webrtc", "language": lang},
        timeout=10,
    ).json()
    room_name = call["id"]
    data = httpx.post(f"{CONTROL_PLANE_URL}/api/token",
                      json={"account_id": "acc-001", "call_id": room_name}, timeout=10).json()
    room = rtc.Room()
    agent_audio = bytearray()
    read_task = None

    def attach(track):
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO) or getattr(track, "name", "") != "roomio_audio":
            return

        async def _read():
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                async for event in stream:
                    frame = getattr(event, "frame", event)
                    agent_audio.extend(bytes(frame.data))
            finally:
                await stream.aclose()

        nonlocal read_task
        read_task = asyncio.get_running_loop().create_task(_read())

    room.on("track_subscribed", attach)
    try:
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track(f"load-{idx}", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        # 等开场白结束（1s 语音+3s 静音）
        processed, speech, silent = 0, 0.0, 0.0
        deadline = time.perf_counter() + 40
        while time.perf_counter() < deadline:
            step = 320
            while processed + step <= len(agent_audio):
                if frame_rms(agent_audio[processed: processed + step]) >= 200:
                    speech += 0.02; silent = 0.0
                else:
                    silent += 0.02
                processed += step
            if speech >= 1.0 and silent >= 3.0:
                break
            await asyncio.sleep(0.5)
        agent_audio.clear()

        for turn in range(1, TURNS + 1):
            agent_audio.clear()
            processed = 0
            t0 = time.perf_counter()
            chunk = int(16000 * 0.1) * 2
            pushed = 0
            for i in range(0, len(pcm), chunk):
                seg = pcm[i:i + chunk]
                frame = rtc.AudioFrame(data=seg, sample_rate=16000, num_channels=1,
                                       samples_per_channel=len(seg) // 2)
                await audio_source.capture_frame(frame)
                await asyncio.sleep(0.08)
                pushed += len(seg)
            push_s = pushed / 32000  # 客户音频时长(秒)
            # 等首声
            first_ms = None
            deadline = time.perf_counter() + 45
            while time.perf_counter() < deadline:
                step = 320
                while processed + step <= len(agent_audio):
                    if frame_rms(agent_audio[processed: processed + step]) >= 200:
                        first_ms = (time.perf_counter() - t0) * 1000
                        break
                    processed += step
                if first_ms:
                    break
                await asyncio.sleep(0.2)
            # 等静音 5s 收轮
            silent = 0.0
            deadline = time.perf_counter() + 60
            while time.perf_counter() < deadline:
                step = 320
                while processed + step <= len(agent_audio):
                    if frame_rms(agent_audio[processed: processed + step]) >= 200:
                        silent = 0.0
                    else:
                        silent += 0.02
                    processed += step
                if silent >= 5.0:
                    break
                await asyncio.sleep(0.5)
            # 真实口径 = 首声时刻 − 推音频耗时(说完之后)
            real_ms = (first_ms - push_s * 1000) if first_ms else None
            results.append({"road": idx, "turn": turn, "first_ms": first_ms, "real_ms": real_ms})
            print(f"[road{idx} t{turn}] first={first_ms and round(first_ms)}ms "
                  f"real_after_speech={real_ms and round(real_ms)}ms", flush=True)
    except Exception as exc:
        results.append({"road": idx, "error": repr(exc)})
        print(f"[road{idx}] ERROR {exc!r}", flush=True)
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{room_name}/hangup", timeout=5)


async def main() -> None:
    results: list = []
    t0 = time.perf_counter()
    await asyncio.gather(*(road(i, results) for i in range(ROADS)))
    wall = time.perf_counter() - t0
    ok = [r for r in results if "error" not in r]
    real = sorted(r["real_ms"] for r in ok if r.get("real_ms"))
    errs = [r for r in results if "error" in r]
    p50 = real[len(real) // 2] if real else 0
    p95 = real[int(len(real) * 0.95)] if real else 0
    print(
        f"AUDIO_LOAD {'PASS' if len(ok) == ROADS * TURNS else 'DEGRADED'} "
        f"roads={ROADS} turns={TURNS} ok={len(ok)}/{ROADS * TURNS} errors={len(errs)} "
        f"real_after_speech_ms p50={p50:.0f} p95={p95:.0f} wall={wall:.0f}s",
        flush=True,
    )
    if errs:
        for r in errs:
            print("  ERR:", r.get("error"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
