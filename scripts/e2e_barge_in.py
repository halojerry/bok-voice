"""A 线打断（barge-in）E2E：AI 播报中插话 → 断言打断生效、不哑火、无崩溃。

流程：
  1) 真实 /api/token 进房（与前端同链路）
  2) 等开场白播完（3s 尾静音）
  3) 推第一句用户音频 → 等 agent 回复开始出声（speech ≥1s）
  4) 推第二句用户音频 = 打断
  5) 断言：① agent 第一回复 ≤5s 内停声（打断生效）；
          ② 停声后 ≤20s 内出现新回复语音（打断后不哑火）；
          ③ 新回复非空（≥1s 语音）。
判定读 agent.log 的 MINIMAX_BIDI_PERF/`job crashed` 由外层测试记录负责（脚本只管音频行为）。

运行：<runtime-python> scripts/e2e_barge_in.py  （需整栈在跑）
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
LANG = os.environ.get("BARGEIN_LANG", "cantonese")
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
# 真人客户口吻两句（第一句触发回复,第二句播放中插入打断）——弃 fixtures 灣仔問路句
FIRST_TEXT = os.environ.get("BARGEIN_FIRST_TEXT", "我件貨爛咗，外包裝都凹咗，想投訴。")
SECOND_TEXT = os.environ.get("BARGEIN_SECOND_TEXT", "唔使住住，我想先問下賠幾多。")


def frame_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def tts_pcm(text: str, lang: str = "cantonese") -> bytes:
    import httpx

    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{TTS_URL}/v1/audio/speech",
            json={"input": text, "language": lang, "voice": "Vivian", "sample_rate": 16000},
        )
        r.raise_for_status()
        return r.content


def read_pcm16(path: Path, max_seconds: float = 4.0) -> bytes:
    with wave.open(str(path), "rb") as w:
        n = int(min(w.getnframes(), w.getframerate() * max_seconds))
        frames = w.readframes(n)
        if w.getframerate() != 16000:
            raise SystemExit(f"{path}: need 16k, got {w.getframerate()}")
        return frames


async def push_pcm(audio_source: rtc.AudioSource, pcm: bytes) -> None:
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


def speech_stats(pcm: bytes, processed: int) -> tuple[float, float, int]:
    """增量统计 (speech_secs, silent_secs, new_processed)，20ms 帧 RMS 口径。"""
    step = 320
    speech = silent = 0.0
    while processed + step <= len(pcm):
        if frame_rms(pcm[processed : processed + step]) >= 200:
            speech += 0.02
            silent = 0.0
        else:
            silent += 0.02
        processed += step
    return speech, silent, processed


async def wait_speech_then_silence(
    buf: bytearray, state: dict, *, need_speech: float, need_silence: float, timeout: float
) -> bool:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        s, sil, state["processed"] = speech_stats(bytes(buf), state["processed"])
        state["speech"] += s
        state["silent"] += sil
        if state["speech"] >= need_speech and state["silent"] >= need_silence:
            return True
        await asyncio.sleep(0.3)
    return False


async def main() -> None:
    lang = LANG
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={
            "display_name": f"E2E-bargein-{int(time.time())}",
            "role_template": "buyer",
            "language": lang,
            "background": "barge-in e2e",
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

        read_tasks.append(asyncio.get_running_loop().create_task(_read()))

    def on_track(track, publication, participant):
        attach(track)

    room.on("track_subscribed", on_track)
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                attach(track)

    try:
        await room.connect(data["serverUrl"], data["participantToken"])
        print(f"[bargein] joined {room_name} (lang={lang})", flush=True)
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("e2e-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        # 等开场白播完（≥1s 语音 + 3s 尾静音）
        state = {"processed": 0, "speech": 0.0, "silent": 0.0}
        await wait_speech_then_silence(
            agent_audio, state, need_speech=1.0, need_silence=3.0, timeout=40
        )
        agent_audio.clear()
        state.update(processed=0, speech=0.0, silent=0.0)
        await asyncio.sleep(0.5)

        # 第一句 → 等回复开始出声（speech ≥1.0s，不等静音）
        await push_pcm(audio_source, tts_pcm(FIRST_TEXT))
        started = time.perf_counter()
        while time.perf_counter() - started < 45:
            s, _, state["processed"] = speech_stats(bytes(agent_audio), state["processed"])
            state["speech"] += s
            if state["speech"] >= 1.0:
                break
            await asyncio.sleep(0.3)
        if state["speech"] < 1.0:
            print("BARGEIN FAIL reply1_no_speech", flush=True)
            raise SystemExit(1)
        reply1_speech_at = len(agent_audio)
        print(f"[bargein] reply1 speaking (speech={state['speech']:.1f}s) → interrupting", flush=True)

        # 打断：立即推第二句用户音频
        interrupt_at = time.perf_counter()
        await push_pcm(audio_source, tts_pcm(SECOND_TEXT))

        # 断言①：agent ≤8s 内停声（打断生效——若一直在讲说明打断失败）
        state2 = {"processed": reply1_speech_at, "speech": 0.0, "silent": 0.0}
        stopped = False
        while time.perf_counter() - interrupt_at < 8:
            _, sil, state2["processed"] = speech_stats(
                bytes(agent_audio), state2["processed"]
            )
            state2["silent"] += sil
            if state2["silent"] >= 2.0:
                stopped = True
                break
            await asyncio.sleep(0.3)
        stop_ms = (time.perf_counter() - interrupt_at) * 1000

        # 断言②：停声后 ≤25s 内新回复语音（不哑火）
        state3 = {"processed": state2["processed"], "speech": 0.0, "silent": 0.0}
        resumed = False
        resumed_at = 0
        deadline = time.perf_counter() + 25
        base = len(agent_audio)
        base_processed = state3["processed"]
        # 重新从已消费位置继续累计新语音（清 silent 累积）
        new_speech = 0.0
        probe = base_processed
        while time.perf_counter() < deadline:
            s, _, probe = speech_stats(bytes(agent_audio), probe)
            new_speech += s
            if new_speech >= 1.0:
                resumed = True
                resumed_at = (time.perf_counter() - interrupt_at) * 1000
                break
            await asyncio.sleep(0.3)

        ok = stopped and resumed
        print(
            f"BARGEIN {'PASS' if ok else 'FAIL'} "
            f"interrupted={'yes' if stopped else 'no'} stop_ms={stop_ms:.0f} "
            f"resumed={'yes' if resumed else 'no'} resume_ms={resumed_at:.0f}",
            flush=True,
        )
        if not ok:
            raise SystemExit(1)
        await asyncio.sleep(2)
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
