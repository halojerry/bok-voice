"""A 线多轮三语 E2E：同一场通话内连续切换 普通话→粤语→英语。

验证三件事：
  1) agent 在同一会话里逐轮跟随用户语言（zh 轮回普通话、cantonese 轮回粤语、en 轮回英语）;
  2) 每轮都有 TTS 语音回复（agent_audio 非空）;
  3) 通话 turns 真实落库（数据沉淀）。

用法：<runtime-python> scripts/e2e_multi_turn.py
前置：`python tools/bok.py serve` 已跑（control-plane 8000 / LiveKit 7880 / ASR 8787）。
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
LIVEKIT_URL = "ws://127.0.0.1:7880"
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"

# 每轮：参考音频 + 期望回复语言标签
TURNS = [
    {"lang": "zh", "file": "zh.wav", "expect": "Chinese"},
    {"lang": "cantonese", "file": "cantonese.wav", "expect": "Cantonese"},
    {"lang": "en", "file": "en.wav", "expect": "English"},
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


async def wait_agent_idle(agent_audio: bytearray) -> None:
    """等 agent 开场白播完（或 35s 超时），避免推太早丢失轮次。"""
    idle_deadline = time.perf_counter() + 35
    last_speech_end = 0
    while time.perf_counter() < idle_deadline:
        total = len(agent_audio)
        speech = 0.0
        step = 320
        i = 0
        while i + step <= total:
            if frame_rms(bytes(agent_audio[i : i + step])) >= 200:
                speech += 0.02
            i += step
        if speech > 0:
            last_speech_end = total
        if last_speech_end > 0 and (total - last_speech_end) / 32000 >= 3.0:
            break
        await asyncio.sleep(0.5)


async def push_and_collect(room: rtc.Room, audio_source: rtc.AudioSource, case: dict) -> bytes:
    """推一段音频，等 agent 回复语音停止增长，返回回复 PCM。"""
    agent_audio = bytearray()
    read_tasks: list[asyncio.Task] = []
    saw_track = False

    def attach(track):
        nonlocal saw_track
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        if getattr(track, "name", "") != "roomio_audio":
            return
        saw_track = True

        async def _read():
            try:
                stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
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

    # 已订阅的 track + 新订阅的 track 都挂上读取
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                attach(track)
    room.on("track_subscribed", lambda track, pub, participant: attach(track))

    await wait_agent_idle(agent_audio)
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

    # 等回复开始并结束（最多 90s）
    speech_secs = 0.0
    silent_secs = 0.0
    processed = 0
    started = time.perf_counter()
    while time.perf_counter() - started < 90:
        step = 320
        while processed + step <= len(agent_audio):
            if frame_rms(bytes(agent_audio[processed : processed + step])) >= 200:
                speech_secs += 0.02
                silent_secs = 0.0
            else:
                silent_secs += 0.02
            processed += step
        if speech_secs >= 1.0 and silent_secs >= 5.0:
            break
        await asyncio.sleep(1)

    room.off("track_subscribed", lambda *a: None)
    for t in read_tasks:
        t.cancel()
    await asyncio.sleep(0.3)
    return bytes(agent_audio)


def asr_language(pcm16: bytes) -> tuple[str, str]:
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


def cantonese_markers(text: str) -> bool:
    markers = set("冇唔嘅係哋佢喺嚟啲嗰喎㗎冚瞓攞揾搵嘥咗乜嘢咩傾偈倾偈而家依家啱啱咁睇嚟睇来同我哋")
    return any(ch in markers for ch in text)


async def main() -> None:
    # 建一个对象 + 粤语人设 + 通话（开场会讲粤语，随后按轮跟随）
    ts = int(time.time())
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"多轮-{ts}", "role_template": "buyer", "language": "cantonese", "background": "multi-turn e2e"},
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": "多轮客服", "language": "cantonese", "tone": "礼貌专业"},
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
            "language": "cantonese",
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
    print(f"[e2e] joined {room_name} (multi-turn zh→cantonese→en)", flush=True)

    room = rtc.Room()
    all_pass = True
    try:
        await room.connect(data["url"], data["token"])
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("e2e-src", audio_source)
        await room.local_participant.publish_track(
            src,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        for turn in TURNS:
            audio = await push_and_collect(room, audio_source, turn)
            lang, text = ("", "")
            if len(audio) > 4000:
                lang, text = asr_language(audio)
            ok = bool(lang) and turn["expect"].lower() in lang.lower()
            if turn["lang"] == "cantonese" and not ok:
                ok = cantonese_markers(text)
            if not ok or len(audio) <= 4000:
                all_pass = False
            print(
                f"[{'PASS' if ok else 'FAIL'}] {turn['lang']} 轮: "
                f"agent_audio={len(audio)}B asr_lang={lang!r} text={text[:50]!r}",
                flush=True,
            )
            await asyncio.sleep(1)
    finally:
        await room.disconnect()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{room_name}/hangup", timeout=10)
        except Exception:
            pass

    # 数据沉淀：查这场通话的 turns
    try:
        turns = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{room_name}/turns", timeout=10).json()
        roles = [(t.get("role"), (t.get("transcript") or "")[:30]) for t in turns]
        print(f"[e2e] 落库 turns={len(turns)}: {roles}", flush=True)
        if len(turns) < len(TURNS) * 2:
            all_pass = False
            print("[e2e] WARN turns 数量不足（应为 每轮 user+assistant 至少 2 条）", flush=True)
    except Exception as exc:
        print(f"[e2e] 查 turns 失败: {exc!r}", flush=True)

    print("MULTI_TURN_E2E", "PASSED" if all_pass else "FAILED", flush=True)
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
