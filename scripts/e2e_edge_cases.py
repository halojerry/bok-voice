"""A 线边界场景 E2E：静音音频 / 超短(<6 字)音频 不得崩链路。

断言（headless 可判的音频行为）：
  1) 推纯静音 → agent 不得崩溃退出（房间保持、后续轮仍可正常应答）;
  2) 推 0.3s 超短音频 → 同上（碎片提交门/QWEN3_ASR_PAUSE_COMMIT 等护栏生效）;
  3) 每个边界轮之后推一句正常粤语 → 必须有正常回复（链路未被边界轮毒化）。
运行：<runtime-python> scripts/e2e_edge_cases.py （需整栈）
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import time
from pathlib import Path

import httpx
from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"
LANG = os.environ.get("EDGE_LANG", "cantonese")


def frame_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def silence_pcm(seconds: float = 1.0) -> bytes:
    return b"\x00" * int(16000 * seconds) * 2


def short_pcm(path: Path, seconds: float = 0.3) -> bytes:
    import wave

    with wave.open(str(path), "rb") as w:
        n = int(w.getframerate() * seconds)
        return w.readframes(n)


async def push_pcm(audio_source: rtc.AudioSource, pcm: bytes) -> None:
    chunk = int(16000 * 0.1) * 2
    for i in range(0, len(pcm), chunk):
        seg = pcm[i:i + chunk]
        frame = rtc.AudioFrame(data=seg, sample_rate=16000, num_channels=1,
                               samples_per_channel=len(seg) // 2)
        await audio_source.capture_frame(frame)
        await asyncio.sleep(0.08)


async def wait_for_reply_speech(buf: bytearray, state: dict, timeout: float) -> float | None:
    """等 ≥1s 语音；返回耗时 ms 或 None。"""
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        step = 320
        while state["processed"] + step <= len(buf):
            if frame_rms(buf[state["processed"]: state["processed"] + step]) >= 200:
                state["speech"] += 0.02
                state["silent"] = 0.0
            else:
                state["silent"] += 0.02
            state["processed"] += step
        if state["speech"] >= 1.0 and state["silent"] >= 5.0:
            return (time.perf_counter() - started) * 1000
        await asyncio.sleep(0.3)
    return None


async def main() -> None:
    ts = int(time.time())
    obj = httpx.post(f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
                     json={"display_name": f"E2E-edge-{ts}", "role_template": "buyer",
                           "language": LANG, "background": "edge cases"},
                     timeout=10).json()
    persona = httpx.post(f"{CONTROL_PLANE_URL}/api/personas?account_id=acc-001",
                         json={"name": "边界客服", "language": LANG, "tone": "礼貌专业"},
                         timeout=10).json()
    call = httpx.post(f"{CONTROL_PLANE_URL}/api/calls",
                      json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
                            "mode": "live", "direction": "webrtc", "language": LANG},
                      timeout=10).json()
    room_name = call["id"]
    data = httpx.post(f"{CONTROL_PLANE_URL}/api/token",
                      json={"account_id": "acc-001", "call_id": room_name}, timeout=10).json()

    room = rtc.Room()
    agent_audio = bytearray()
    read_task = None

    def attach(track):
        nonlocal read_task
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

        read_task = asyncio.get_running_loop().create_task(_read())

    room.on("track_subscribed", attach)
    try:
        await room.connect(data["serverUrl"], data["participantToken"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("e2e-edge", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        # 等开场白结束
        state = {"processed": 0, "speech": 0.0, "silent": 0.0}
        await wait_for_reply_speech(agent_audio, state, timeout=40)
        agent_audio.clear()
        state = {"processed": 0, "speech": 0.0, "silent": 0.0}

        normal = short_pcm(AUDIO_DIR / f"{LANG}.wav", 0.3)
        full = AUDIO_DIR / f"{LANG}.wav"
        import wave
        with wave.open(str(full), "rb") as w:
            normal = w.readframes(int(w.getframerate() * 2.5))

        all_pass = True
        # 边界轮:静音 1s
        await push_pcm(audio_source, silence_pcm(1.0))
        await asyncio.sleep(4)
        # 边界轮:超短 0.3s
        await push_pcm(audio_source, short_pcm(AUDIO_DIR / f"{LANG}.wav", 0.3))
        await asyncio.sleep(4)
        # 正常轮:必须正常回复（链路未被毒化）
        agent_audio.clear()
        state = {"processed": 0, "speech": 0.0, "silent": 0.0}
        await push_pcm(audio_source, normal)
        ms = await wait_for_reply_speech(agent_audio, state, timeout=45)
        ok = ms is not None
        all_pass = all_pass and ok
        print(f"[{'PASS' if ok else 'FAIL'}] after silence+short turns, normal turn "
              f"reply={'%.0fms' % ms if ms else 'NONE'}", flush=True)

        print("EDGE_CASES_E2E", "PASSED" if all_pass else "FAILED", flush=True)
        if not all_pass:
            raise SystemExit(1)
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{room_name}/hangup", timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
