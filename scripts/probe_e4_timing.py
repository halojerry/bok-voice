"""E4 时序探针：复现「回复中打断」场景并逐步打点用户轮提交/回复语音时延。

目的：把 e2e_edge_cases E4 两连 FAIL 拆成可归因的时间轴——
  ①cantonese 输入 → 用户轮提交耗时 / 回复首声耗时
  ②en 输入（回复播放中推入）→ 用户轮提交耗时（含中文部分解码+英文整句重解码
    双提交观测）/ 打断后新回复首声耗时
用法：.venv312/bin/python scripts/probe_e4_timing.py
"""
from __future__ import annotations

import asyncio
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

import httpx
from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"


def read_wav_pcm(path: Path, max_seconds: float = 4.5) -> bytes:
    with wave.open(str(path), "rb") as w:
        n = int(min(w.getnframes(), w.getframerate() * max_seconds))
        return w.readframes(n)


def frame_rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if not n:
        return 0.0
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


async def push_pcm(audio_source: rtc.AudioSource, pcm: bytes, real_time: bool = True) -> None:
    chunk = int(16000 * 0.1) * 2
    for i in range(0, len(pcm), chunk):
        seg = pcm[i : i + chunk]
        frame = rtc.AudioFrame(
            data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2
        )
        await audio_source.capture_frame(frame)
        if real_time:
            await asyncio.sleep(0.08)


async def wait_reply_speech(agent_audio: bytearray, mark: int, timeout_s: float) -> float:
    """等 agent 出声（mark 之后新出现 ≥0.5s 有效音频），返回耗时秒数。"""
    start = time.monotonic()
    processed = mark
    while time.monotonic() - start < timeout_s:
        step = 320
        hits = 0
        while processed + step <= len(agent_audio):
            if frame_rms(bytes(agent_audio[processed : processed + step])) >= 200:
                hits += 1
            processed += step
        if hits >= 25:  # ≥0.5s 连续有声
            return time.monotonic() - start
        await asyncio.sleep(0.1)
    return -1.0


def turns_of(call_id: str) -> list[dict]:
    return httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()


async def main() -> int:
    ts = int(time.time() * 1000) % 100000
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"探针-E4-{ts}", "role_template": "buyer", "language": "cantonese", "background": "probe"},
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": f"探针客服E4{ts}", "language": "cantonese", "tone": "礼貌专业"},
        timeout=10,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
              "mode": "live", "direction": "webrtc", "language": "cantonese"},
        timeout=10,
    ).json()
    call_id = call["id"]
    data = httpx.post(f"{CONTROL_PLANE_URL}/api/token", json={"account_id": "acc-001", "call_id": call_id}, timeout=10).json()

    room = rtc.Room()
    await room.connect(data["serverUrl"], data["participantToken"])
    audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
    src = rtc.LocalAudioTrack.create_audio_track("qa-src", audio_source)
    await room.local_participant.publish_track(
        src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    agent_audio = bytearray()
    audio_started = asyncio.Event()

    def on_track(track, *_args):
        async def _read():
            audio_started.set()
            stream = rtc.AudioStream(track)
            async for event in stream:
                agent_audio.extend(bytes(event.frame.data))

        asyncio.get_running_loop().create_task(_read())

    room.on("track_subscribed", on_track)
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            if pub.track is not None:
                on_track(pub.track)
    await asyncio.wait_for(audio_started.wait(), timeout=20)
    await asyncio.sleep(2.0)  # 开场白

    cantonese_pcm = read_wav_pcm(AUDIO_DIR / "cantonese.wav")
    en_pcm = read_wav_pcm(AUDIO_DIR / "en.wav")
    print(f"cantonese.wav={len(cantonese_pcm)/32000:.1f}s en.wav={len(en_pcm)/32000:.1f}s")

    # ---- ① cantonese 输入 ----
    t0 = time.monotonic()
    push_task = asyncio.get_running_loop().create_task(push_pcm(audio_source, cantonese_pcm))
    mark = len(agent_audio)
    speech1 = await wait_reply_speech(agent_audio, mark, 40)
    await push_task
    print(f"[cantonese] reply_speech_at=+{speech1:.1f}s (push wall {time.monotonic()-t0:.1f}s)")
    await asyncio.sleep(6)
    n_user = len([t for t in turns_of(call_id) if t["role"] == "user"])

    # ---- ② en 输入（E4 打断）----
    t1 = time.monotonic()
    push_task2 = asyncio.get_running_loop().create_task(push_pcm(audio_source, en_pcm))
    en_turn_at = -1.0
    mark2 = -1
    deadline = time.monotonic() + 90
    seen_texts: list[str] = []
    while time.monotonic() < deadline:
        users = [t for t in turns_of(call_id) if t["role"] == "user"]
        if len(users) > n_user:
            en_turn_at = time.monotonic() - t1
            mark2 = len(agent_audio)  # 打断基准=用户轮提交落库那刻（回复在其后）
            seen_texts = [u["transcript"][:28] for u in users[n_user:]]
            print(f"[en] user_turn_committed_at=+{en_turn_at:.1f}s texts={seen_texts}")
            break
        await asyncio.sleep(0.5)
    await push_task2
    print(f"[en] push_wall={time.monotonic()-t1:.1f}s")
    speech2 = await wait_reply_speech(agent_audio, mark2, 40)
    print(f"[en] first_new_reply_speech={'NONE' if speech2 < 0 else f'+{speech2:.1f}s'}")
    await asyncio.sleep(8)
    turns = turns_of(call_id)
    print(f"[final] turns={len(turns)}")
    for t in turns[-8:]:
        print(f"   {t['role']:9} {t['created_at'][11:23]} {t['transcript'][:36]!r}")
    await room.disconnect()
    try:
        httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10)
    except Exception:
        pass
    ok = en_turn_at > 0 and speech2 >= 0
    print("PROBE_RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
