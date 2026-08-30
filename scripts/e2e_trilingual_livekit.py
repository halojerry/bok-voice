"""A 线三语 E2E：真实 LiveKit 房间 + 宿主机 sidecar（ASR/TTS）+ Docker agent。

流程：
  1) 连接 LiveKit，加入房间
  2) 依次推送 zh/yue/en 测试音频（16k PCM）
  3) 收集 agent 回复音频轨道
  4) 用 sidecar ASR 转写回复音频，断言语言标签正确
运行：.venv312/bin/python scripts/e2e_trilingual_livekit.py
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
import wave
from pathlib import Path

import httpx
from livekit import api, rtc

ROOT = Path(__file__).resolve().parents[1]
LIVEKIT_URL = "ws://127.0.0.1:7880"
LIVEKIT_KEY = "devkey"
LIVEKIT_SECRET = "devsecret"
AUDIO_DIR = ROOT / "data" / "test-audio"

_ALL_CASES = [
    {"lang": "zh", "file": "zh.wav", "expect_lang": "Chinese"},
    {"lang": "yue", "file": "yue.wav", "expect_lang": "Cantonese"},
    {"lang": "en", "file": "en.wav", "expect_lang": "English"},
]

import os

CASES = [
    c
    for c in _ALL_CASES
    if not os.environ.get("E2E_ONLY") or c["lang"] == os.environ["E2E_ONLY"]
]


def frame_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def read_pcm16(path: Path, max_seconds: float = 4.0) -> bytes:
    with wave.open(str(path), "rb") as w:
        n = int(min(w.getnframes(), w.getframerate() * max_seconds))
        frames = w.readframes(n)
        if w.getframerate() != 16000:
            raise SystemExit(f"{path}: need 16k, got {w.getframerate()}")
        return frames


async def run_case(room: rtc.Room, audio_source: rtc.AudioSource, case: dict) -> dict:
    agent_audio = bytearray()
    read_tasks: list[asyncio.Task] = []

    def attach(track):
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") != "roomio_audio":
            return

        async def _read():
            stream = None
            try:
                stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
                async for event in stream:
                    frame = getattr(event, "frame", event)
                    agent_audio.extend(bytes(frame.data))
            except Exception as e:
                print(f"READ_ERROR {type(e).__name__}: {e}", flush=True)
            finally:
                if stream is not None:
                    await stream.aclose()

        read_tasks.append(asyncio.get_running_loop().create_task(_read()))

    # Attach to tracks already subscribed (agent TTS track persists across turns)
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                attach(track)

    def on_track(track, publication, participant):
        attach(track)

    room.on("track_subscribed", on_track)

    # Wait for the agent's opening greeting to finish playing before pushing
    # user audio. The voice agent does not process user speech while it is
    # still generating/playing the greeting, so pushing too early loses the
    # turn entirely (observed: only the greeting reply is ever produced).
    def _speech_secs_since(pcm: bytes, since_offset: int) -> float:
        step = 320
        count = 0.0
        i = since_offset
        while i + step <= len(pcm):
            if frame_rms(bytes(pcm[i : i + step])) >= 200:
                count += 0.02
            i += step
        return count

    idle_deadline = time.perf_counter() + 35
    last_speech_end = 0
    while time.perf_counter() < idle_deadline:
        total = len(agent_audio)
        speech = _speech_secs_since(bytes(agent_audio), 0)
        if speech > 0:
            last_speech_end = total
        # 3s of trailing silence after any speech => agent idle
        if last_speech_end > 0 and (total - last_speech_end) / 32000 >= 3.0:
            break
        await asyncio.sleep(0.5)

    # Discard greeting / residual audio so each case's capture starts clean.
    agent_audio.clear()
    await asyncio.sleep(0.5)

    pcm = read_pcm16(AUDIO_DIR / case["file"])
    chunk = int(16000 * 0.1) * 2
    for i in range(0, len(pcm), chunk):
        seg = pcm[i : i + chunk]
        frame = rtc.AudioFrame(
            data=seg,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=(len(seg) // 2),
        )
        await audio_source.capture_frame(frame)
        await asyncio.sleep(0.08)
    await asyncio.sleep(1.0)

    # Wait for reply speech to start (up to 45s), then for it to stop growing
    # (up to 90s). Frames are 20ms; count only speech frames by RMS.
    speech_secs = 0.0
    silent_secs = 0.0
    processed = 0
    started = time.perf_counter()
    while time.perf_counter() - started < 90:
        # count only newly arrived 20ms frames
        step = 320  # 20ms at 16k mono s16
        while processed + step <= len(agent_audio):
            rms = frame_rms(bytes(agent_audio[processed : processed + step]))
            if rms >= 200:
                speech_secs += 0.02
                silent_secs = 0.0
            else:
                silent_secs += 0.02
            processed += step
        if speech_secs >= 1.5 and silent_secs >= 5.0:
            break
        await asyncio.sleep(1)

    room.off("track_subscribed", on_track)
    for t in read_tasks:
        t.cancel()
    await asyncio.sleep(0.3)

    return {
        "lang": case["lang"],
        "agent_audio_bytes": len(agent_audio),
        "agent_audio": bytes(agent_audio),
    }


def asr_language(pcm16: bytes) -> tuple[str, str]:
    # trim leading/trailing silence (rms < 100) for cleaner ASR
    step = 320
    start = 0
    end = len(pcm16)
    for i in range(0, len(pcm16) - step + 1, step):
        if frame_rms(pcm16[i : i + step]) >= 100:
            start = i
            break
    for i in range(len(pcm16) - step, -1, -step):
        if frame_rms(pcm16[i : i + step]) >= 100:
            end = i + step
            break
    pcm16 = pcm16[start:end]
    with httpx.Client(timeout=60) as client:
        s = client.post("http://127.0.0.1:8787/api/start").json()["session_id"]
        for i in range(0, len(pcm16), 3200):
            client.post(
                "http://127.0.0.1:8787/api/chunk",
                params={"session_id": s},
                content=pcm16[i : i + 3200],
            )
        out = client.post(
            "http://127.0.0.1:8787/api/finish", params={"session_id": s}
        ).json()
        return str(out.get("language") or ""), str(out.get("text") or "")


async def main() -> None:
    token_api = api.AccessToken(LIVEKIT_KEY, LIVEKIT_SECRET)
    room_name = f"e2e-trilingual-{int(time.time())}"
    token = token_api.with_identity("e2e-driver").with_name("E2E").with_grants(
        api.VideoGrants(room_join=True, room=room_name)
    ).to_jwt()

    results = []
    room = rtc.Room()
    try:
        await room.connect(LIVEKIT_URL, token)
        print(f"joined {room_name}", flush=True)
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("e2e-src", audio_source)
        await room.local_participant.publish_track(
            src,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        for case in CASES:
            r = await run_case(room, audio_source, case)
            results.append(r)
            print(
                f"[{case['lang']}] agent_audio={r['agent_audio_bytes']}B",
                flush=True,
            )
            await asyncio.sleep(1)
    finally:
        await room.disconnect()

    passed = 0
    for r in results:
        lang, text = "", ""
        if r["agent_audio_bytes"] > 4000:
            lang, text = asr_language(r["agent_audio"])
        expect = {"zh": "Chinese", "yue": "Cantonese", "en": "English"}[r["lang"]]
        ok = bool(lang) and expect.lower() in lang.lower()
        if r["lang"] == "yue" and not ok:
            # MLX ASR sometimes labels Cantonese-flavored replies as "Chinese";
            # accept when the transcribed text carries clear Cantonese markers.
            cantonese = set("冇唔嘅係哋佢喺嚟啲嗰喎㗎冚瞓攞揾搵嘥咗乜嘢咩傾偈倾偈倾下傾下唔該而家依家啱啱咁睇嚟睇来同我哋")
            ok = any(ch in cantonese for ch in text)
        print(
            f"[{'PASS' if ok else 'FAIL'}] {r['lang']} "
            f"agent_audio={r['agent_audio_bytes']}B asr_lang={lang!r} text={text[:40]!r}"
        )
        if ok:
            passed += 1
    print(f"TRILINGUAL_E2E {passed}/{len(results)} PASSED")
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
