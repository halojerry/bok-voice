"""抢跑（preemptive generation）诊断探针——推长句喂饱 speculative 链。

E2E 短句（<10 字）+ 开场白期 partial 抑制令 PREFLIGHT 一次都发不出，抢跑
比较块从未跑过（探针实机 2026-09-10）。真实通话长句才有 PREFLIGHT→speculate
→EOS commit→四条件比较全链。本探针推一句长话（默认 ~18s 无标点长句），
产生真实 speculation 流量；诊断读数看 agent.log 的 BOK_PREEMPTIVE_DEBUG 行
（须 BOK_PREEMPTIVE_DEBUG=1 起 worker）。主判据=回复出声（链路活）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import httpx
from livekit import rtc

from e2e_barge_in import push_pcm, speech_stats, tts_pcm, wait_speech_then_silence  # noqa: E402

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
LANG = os.environ.get("PREEMPTIVE_LANG", "cantonese")
TEXT = os.environ.get(
    "PREEMPTIVE_TEXT",
    "你好我想查下我上個禮拜寄出去嘅一個集運件而家仲未送到門口"
    "想麻煩你幫我查下件嘢而家去咗邊度同埋想問下如果真係丟咗"
    "你哋會點賠係唔係可以賠返個貨值",
)
# 第二轮短问(投机预热主战场:快照已有、LLM 空闲、稳定前缀持续增长)
TEXT2 = os.environ.get(
    "PREEMPTIVE_TEXT2",
    "好的麻煩你幫我查下我個單號係三七七八九零",
)


async def main() -> None:
    lang = LANG
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={
            "display_name": f"E2E-pmpre-{int(time.time())}",
            "role_template": "buyer",
            "language": lang,
            "background": "preemptive probe",
        },
        timeout=10,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={
            "account_id": "acc-001",
            "object_id": obj["id"],
            "mode": "live",
            "direction": "webrtc",
            "language": lang,
        },
        timeout=10,
    ).json()
    room_name = call["id"]
    data = httpx.post(
        f"{CONTROL_PLANE_URL}/api/token",
        json={"account_id": "acc-001", "call_id": room_name},
        timeout=10,
    ).raise_for_status().json()

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

        read_tasks.append(asyncio.create_task(_read()))

    def on_track(track, publication, participant):
        attach(track)

    room.on("track_subscribed", on_track)

    ok = False
    try:
        await room.connect(data["serverUrl"], data["participantToken"])
        print(f"[pmpre] joined {room_name} (lang={lang}) text_chars={len(TEXT)}", flush=True)
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("e2e-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        state = {"processed": 0, "speech": 0.0, "silent": 0.0}
        for _ in range(3):
            state.update(processed=len(agent_audio) if state["speech"] < 1.0 else state["processed"])
            await wait_speech_then_silence(
                agent_audio, state, need_speech=1.0, need_silence=3.0, timeout=40
            )
            if state["speech"] >= 1.0:
                break
        assert state["speech"] >= 1.0, "开场白 40s 内没出声(agent 未就绪?)"
        state.update(processed=len(agent_audio), speech=0.0, silent=0.0)
        await asyncio.sleep(0.5)

        pcm = tts_pcm(TEXT, lang)
        await push_pcm(audio_source, pcm)
        t_pushed = time.perf_counter()
        print(f"[pmpre] pushed long utterance ({len(pcm) / 32000:.1f}s)", flush=True)

        onset_ms = None
        speech_total = 0.0
        probe = len(agent_audio)
        while time.perf_counter() - t_pushed < 45:
            s, _, probe = speech_stats(bytes(agent_audio), probe)
            speech_total += s
            if onset_ms is None and speech_total >= 0.12:
                onset_ms = (time.perf_counter() - t_pushed) * 1000
            if onset_ms is not None and speech_total >= 1.5:
                break
            await asyncio.sleep(0.1)

        ok = onset_ms is not None
        if onset_ms is not None:
            print(f"[pmpre] turn1 first_audio_ms={onset_ms:.0f} speech_total={speech_total:.2f}s")
        else:
            print("[pmpre] turn1 NO reply audio within 45s")

        # 第二轮:等第一轮回复播完(尾静音),再推短问——投机预热主战场。
        if ok:
            state.update(processed=len(agent_audio), speech=0.0, silent=0.0)
            await wait_speech_then_silence(
                agent_audio, state, need_speech=0.5, need_silence=2.5, timeout=45
            )
            await asyncio.sleep(0.5)
            probe = len(agent_audio)
            pcm2 = tts_pcm(TEXT2, lang)
            await push_pcm(audio_source, pcm2)
            t2 = time.perf_counter()
            onset2 = None
            speech2 = 0.0
            while time.perf_counter() - t2 < 45:
                s, _, probe = speech_stats(bytes(agent_audio), probe)
                speech2 += s
                if onset2 is None and speech2 >= 0.12:
                    onset2 = (time.perf_counter() - t2) * 1000
                if onset2 is not None and speech2 >= 1.2:
                    break
                await asyncio.sleep(0.1)
            if onset2 is not None:
                print(f"[pmpre] turn2 first_audio_ms={onset2:.0f} speech_total={speech2:.2f}s")
                ok = True
            else:
                print("[pmpre] turn2 NO reply audio within 45s")
                ok = False
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
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
