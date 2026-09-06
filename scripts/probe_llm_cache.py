"""LLM 缓存命中探针：一通电话内连续多轮真实对话，配合 BOK_LLM_MSG_DEBUG=1
逐消息指纹定位「cached 钉死锚点」的分叉消息（2026-09-06 TTFT 回归调查工具）。

流程（复用 e2e_trilingual_livekit 的真实 token/音频链路）：
  1) 建 zh 对象+人设+通话 → /api/token 进房
  2) 等开场白播完（RMS 静音检测）
  3) 依次播放 /tmp/probe-audio/u1/u2/u3.wav（喂，你好/好啊/那怎么联系我），
     每轮等回复出声并静音后再进下一轮
  4) 离房；从 agent.log 提取 LLM_TTFT_MS/指纹序列分析

运行：.venv312/bin/python scripts/probe_llm_cache.py
前置：BOK_LLM_MSG_DEBUG=1 的 agent worker 已注册。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
LIVEKIT_URL = "ws://127.0.0.1:7880"
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
AUDIO_DIR = Path("/tmp/probe-audio")
UTTERANCES = ["u1", "u2", "u3"]


def frame_rms(pcm: bytes) -> float:
    import math
    import struct

    count = len(pcm) // 2
    if not count:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    return math.sqrt(sum(s * s for s in samples) / count)


def read_pcm16(path: Path, max_seconds: float = 6.0) -> bytes:
    import wave

    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        n = min(w.getnframes(), int(rate * max_seconds))
        return w.readframes(n)


async def main() -> None:
    # PROBE_OBJECT/PROBE_PERSONA 传入已有 id（绑了话术模板的对象才能复现「推进轮
    # 改写尾部」路径）；缺省则新建无模板对象（只测基线缓存链）。
    probe_obj = os.environ.get("PROBE_OBJECT", "")
    probe_persona = os.environ.get("PROBE_PERSONA", "")
    if probe_obj and probe_persona:
        obj = {"id": probe_obj}
        persona = {"id": probe_persona}
    else:
        obj = httpx.post(
            f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
            json={"display_name": f"CACHEPROBE-{int(time.time())}", "role_template": "buyer", "language": "zh", "background": "cache probe"},
            timeout=10,
        ).json()
        persona = httpx.post(
            f"{CONTROL_PLANE_URL}/api/personas?account_id=acc-001",
            json={"name": "E2E客服", "language": "zh", "tone": "礼貌专业"},
            timeout=10,
        ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"], "mode": "live", "direction": "webrtc", "language": "zh"},
        timeout=10,
    ).json()
    room_name = call["id"]
    tok = httpx.post(f"{CONTROL_PLANE_URL}/api/token", json={"account_id": "acc-001", "call_id": room_name}, timeout=10)
    tok.raise_for_status()
    data = tok.json()
    print(f"[probe] call={room_name}", flush=True)

    room = rtc.Room()
    agent_audio = bytearray()
    read_tasks = []

    def on_track(track, publication, participant):
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") != "roomio_audio":
            return

        async def _read():
            async for ev in rtc.AudioStream(track, sample_rate=16000, num_channels=1):
                agent_audio.extend(ev.frame.data)

        read_tasks.append(asyncio.create_task(_read()))

    room.on("track_subscribed", on_track)
    await room.connect(data["serverUrl"], data["participantToken"])
    print("[probe] joined", flush=True)
    for pub in room.remote_participants.values():
        for p in pub.track_publications.values():
            t = getattr(p, "track", None)
            if t is not None:
                on_track(t, p, pub)

    def speech_secs(pcm: bytes) -> float:
        step = 320
        return sum(0.02 for i in range(0, len(pcm) - step, step) if frame_rms(bytes(pcm[i : i + step])) >= 200)

    # 等开场白播完：有语音后连续 3s 静音
    deadline = time.perf_counter() + 35
    last_end = 0
    while time.perf_counter() < deadline:
        if speech_secs(bytes(agent_audio)) > 0:
            last_end = len(agent_audio)
        if last_end and (len(agent_audio) - last_end) / 32000 >= 3.0:
            break
        await asyncio.sleep(0.5)
    agent_audio.clear()
    await asyncio.sleep(0.5)
    print("[probe] greeting done, driving turns", flush=True)

    source = rtc.AudioSource(16000, 1)
    src = rtc.LocalAudioTrack.create_audio_track("probe-src", source)
    await room.local_participant.publish_track(
        src,
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )

    for name in UTTERANCES:
        pcm = read_pcm16(AUDIO_DIR / f"{name}.wav")
        chunk = int(16000 * 0.1) * 2
        for i in range(0, len(pcm), chunk):
            seg = pcm[i : i + chunk]
            frame = rtc.AudioFrame(data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2)
            await source.capture_frame(frame)
            await asyncio.sleep(0.08)
        print(f"[probe] played {name}", flush=True)
        # 等回复：语音出现后再静音 5s（或 45s 超时）
        started = time.perf_counter()
        speech = 0.0
        silent = 0.0
        processed = 0
        while time.perf_counter() - started < 45:
            step = 320
            while processed + step <= len(agent_audio):
                if frame_rms(bytes(agent_audio[processed : processed + step])) >= 200:
                    speech += 0.02
                    silent = 0.0
                else:
                    silent += 0.02
                processed += step
            if speech >= 1.0 and silent >= 5.0:
                break
            await asyncio.sleep(0.5)
        print(f"[probe] turn {name}: reply speech={speech:.1f}s", flush=True)
        agent_audio.clear()
        await asyncio.sleep(0.5)

    for t in read_tasks:
        t.cancel()
    await room.disconnect()
    print("[probe] done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
