"""A 线长稳 soak（2026-09-07 QA 新增）——渐进泄漏检测。

连续 N 通短通话（默认 40，每通 1 轮），每 5 通采样一次进程资源：
  RSS / 线程数（ps）、fd 数（lsof）——对象：agent worker、interp-fwd/rev、
  CP、ASR/TTS sidecar、mlx_lm（按进程名聚合）。
输出 /tmp/qa-soak-samples.tsv 与趋势判定（首末对比，RSS 涨幅 >40% 且单调
上升的进程名记为嫌疑）。

用法：<venv-python> scripts/soak_test.py [ROUNDS]
"""
from __future__ import annotations

import asyncio
import math
import os
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"
WATCH = ("agent_runtime.main", "agent_runtime.interpret", "uvicorn control_plane", "app:app", "mlx_lm")


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


def sample_processes() -> dict[str, dict]:
    """按进程名聚合 RSS(KB)/线程数/fd 数。"""
    out: dict[str, dict] = {}
    try:
        ps = subprocess.run(
            ["ps", "-axo", "pid,rss,nlwp,command"], capture_output=True, text=True, timeout=15
        ).stdout
    except Exception:
        return out
    for line in ps.splitlines()[1:]:
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, rss, nlwp, cmd = parts
        tag = next((w for w in WATCH if w in cmd), None)
        if not tag:
            continue
        d = out.setdefault(tag, {"rss": 0, "threads": 0, "fds": 0, "pids": []})
        try:
            d["rss"] += int(rss)
            d["threads"] += int(nlwp)
            d["pids"].append(pid)
        except ValueError:
            continue
    for tag, d in out.items():
        for pid in d["pids"]:
            try:
                fds = subprocess.run(["lsof", "-p", pid], capture_output=True, text=True, timeout=20).stdout
                d["fds"] += sum(1 for _ in fds.splitlines()) - 1
            except Exception:
                pass
    return out


async def one_round(idx: int, pcm: bytes) -> bool:
    ts = int(time.time() * 1000) % 1000000
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"soak{idx}-{ts}", "role_template": "buyer", "language": "cantonese"},
        timeout=15,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": "soak客服", "language": "cantonese"}, timeout=15,
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

    async def _read(stream: rtc.AudioStream):
        try:
            async for event in stream:
                frame = getattr(event, "frame", event)
                agent_audio.extend(bytes(frame.data))
        except Exception:
            pass

    def on_track(track, pub, participant):
        if int(track.kind) == int(rtc.TrackKind.KIND_AUDIO) and getattr(track, "name", "") == "roomio_audio":
            asyncio.get_running_loop().create_task(_read(rtc.AudioStream(track, sample_rate=16000, num_channels=1)))

    room.on("track_subscribed", on_track)
    ok = True
    try:
        await asyncio.sleep(10)  # 开场白
        mark = len(agent_audio)
        chunk = int(16000 * 0.1) * 2
        for i in range(0, len(pcm), chunk):
            seg = pcm[i : i + chunk]
            frame = rtc.AudioFrame(
                data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2
            )
            await audio_source.capture_frame(frame)
            await asyncio.sleep(0.08)
        deadline = time.perf_counter() + 40
        processed, speech = mark, 0.0
        while time.perf_counter() < deadline:
            step = 320
            while processed + step <= len(agent_audio):
                if frame_rms(bytes(agent_audio[processed : processed + step])) >= 200:
                    speech += 0.02
                processed += step
            if speech >= 0.6:
                break
            await asyncio.sleep(0.1)
        ok = speech >= 0.6
    except Exception:
        ok = False
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10)
        except Exception:
            pass
    return ok


async def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    pcm = read_wav_pcm(AUDIO_DIR / "cantonese.wav")
    samples: list[dict] = []
    ok_count = 0
    t0 = time.perf_counter()
    for i in range(rounds):
        ok = await one_round(i, pcm)
        ok_count += int(ok)
        print(f"[soak] round {i + 1}/{rounds} reply={'ok' if ok else 'MISSING'} elapsed={time.perf_counter() - t0:.0f}s", flush=True)
        if (i + 1) % 5 == 0:
            snap = sample_processes()
            samples.append({"round": i + 1, "elapsed_s": round(time.perf_counter() - t0), "procs": snap})
            print("  [sample] " + " | ".join(
                f"{tag}: rss={d['rss'] // 1024}MB thr={d['threads']} fd={d['fds']}" for tag, d in sorted(snap.items())
            ), flush=True)

    # 趋势判定：首末样本对比
    print("\n[soak] 趋势（首末样本）:", flush=True)
    suspect = []
    if len(samples) >= 2:
        first, last = samples[0]["procs"], samples[-1]["procs"]
        for tag in sorted(set(first) | set(last)):
            r0 = first.get(tag, {}).get("rss", 0)
            r1 = last.get(tag, {}).get("rss", 0)
            if r0 and r1:
                pct = (r1 - r0) / r0 * 100
                mark = " <-- SUSPECT" if pct > 40 else ""
                if pct > 40:
                    suspect.append(tag)
                print(f"  {tag}: rss {r0 // 1024}MB -> {r1 // 1024}MB ({pct:+.0f}%){mark}", flush=True)
    out = Path("/tmp/qa-soak-samples.tsv")
    with out.open("w") as f:
        f.write("round\telapsed_s\ttag\trss_kb\tthreads\tfds\n")
        for s in samples:
            for tag, d in s["procs"].items():
                f.write(f"{s['round']}\t{s['elapsed_s']}\t{tag}\t{d['rss']}\t{d['threads']}\t{d['fds']}\n")
    print(f"\nSOAK {ok_count}/{rounds} replies ok; samples -> {out}; suspects={suspect or '无'}", flush=True)
    return 0 if ok_count >= rounds * 0.9 and not suspect else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
