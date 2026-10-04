#!/usr/bin/env python3
"""B 线差分探针:延迟麦克风发布 vs 即时发布(call-72112fd7 复现器)。

现场形态(2026-09-30 call-72112fd7 取证):
- 双浏览器(me-/other-)21:57:53 进房,传译开关默认关 → 不采麦;
- 21:58:10(进房 +16s)开关打开,setMicrophoneEnabled 发布麦克风(服务器
  mediaTrack published 实锤双轨都在);
- rev(other- 方向)**订阅成功**——ASR/MT 全链跑通;
- fwd(me- 方向)零订阅、零 ASR、零译文,静默 3 分钟 = 客户听不到翻译。

本探针按同一时间形态复刻:两端进房 → 等 delay-s → 发布麦克风 → me- 推
zh 音 → 断言 other- 收到 trans-en 译文音轨。基线臂 delay=0 应通(=既有
e2e_interpret I1);延迟臂复刻现场。跑完看 interp-fwd.log 有无 QWEN3_ASR_TEXT。

用法:
  .venv312/bin/python scripts/probe_interp_late_mic.py --delay-s 0
  .venv312/bin/python scripts/probe_interp_late_mic.py --delay-s 16
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import argparse
import asyncio
import os
import sys
import time
import wave
from pathlib import Path

import httpx
from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"

_CP_HEADERS = {}
_tok = os.environ.get("BOK_CP_TOKEN", "")
if _tok:
    _CP_HEADERS["Authorization"] = f"Bearer {_tok}"


def read_wav_pcm(path: Path, max_seconds: float = 600.0) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, path
        return w.readframes(min(int(w.getframerate() * max_seconds), w.getnframes()))


def frame_rms(pcm: bytes) -> float:
    import struct

    step = 320
    acc = 0
    n = 0
    for i in range(0, min(len(pcm), step * 50), step):
        seg = pcm[i : i + step]
        if len(seg) < step:
            break
        v = struct.unpack(f"<{len(seg)//2}h", seg)
        acc += sum(x * x for x in v) / len(v)
        n += 1
    return (acc / n) ** 0.5 if n else 0.0


class Side:
    """同传参与端:进房(可选暂不采麦) → 延迟发布麦克风 → 推流/捕获翻译轨。"""

    def __init__(self, call_id: str, identity: str):
        self.call_id = call_id
        self.identity = identity
        self.room = rtc.Room()
        self.audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        self.captured = bytearray()
        self.trans_names: set[str] = set()
        self.published_at: float | None = None
        self._tasks: list[asyncio.Task] = []
        self._pub_event = asyncio.Event()

    async def connect(self, publish_mic: bool) -> None:
        data = httpx.post(
            f"{CONTROL_PLANE_URL}/api/token",
            headers=_CP_HEADERS,
            json={"account_id": "acc-001", "call_id": self.call_id, "participant_identity": self.identity},
            timeout=15,
        ).json()
        await self.room.connect(data["serverUrl"], data["participantToken"])
        self._register_capture()
        if publish_mic:
            await self.publish_mic()

    def _register_capture(self) -> None:
        def on_track(track, pub, participant):
            if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
                return
            if not getattr(track, "name", "").startswith("trans-"):
                return
            self.trans_names.add(track.name)
            print(f"  [track] {self.identity} <- {track.name}", flush=True)

            async def _read():
                try:
                    stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
                    async for event in stream:
                        frame = getattr(event, "frame", event)
                        self.captured.extend(bytes(frame.data))
                except Exception:
                    pass

            self._tasks.append(asyncio.get_running_loop().create_task(_read()))

        self.room.on("track_subscribed", on_track)
        for participant in self.room.remote_participants.values():
            for pub in participant.track_publications.values():
                track = getattr(pub, "track", None)
                if track is not None:
                    on_track(track, pub, participant)

    async def publish_mic(self) -> None:
        """复刻浏览器 setMicrophoneEnabled(true):连接后发布 source=MICROPHONE 轨。"""
        src = rtc.LocalAudioTrack.create_audio_track("qa-src", self.audio_source)
        await self.room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        self.published_at = time.time()
        self._pub_event.set()
        print(f"  [mic] {self.identity} published at +{(self.published_at - _T0):.1f}s", flush=True)

    async def push(self, pcm: bytes) -> None:
        chunk = int(16000 * 0.1) * 2
        for i in range(0, len(pcm), chunk):
            seg = pcm[i : i + chunk]
            frame = rtc.AudioFrame(
                data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2
            )
            await self.audio_source.capture_frame(frame)
            await asyncio.sleep(0.08)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        try:
            await self.room.disconnect()
        except Exception:
            pass


_T0 = time.time()


async def wait_audio(captured: bytearray, mark: int, timeout_s: float) -> float:
    """等译文音轨出现语音,返回累计语音秒数。"""
    deadline = time.perf_counter() + timeout_s
    speech = 0.0
    processed = mark
    while time.perf_counter() < deadline:
        step = 320
        while processed + step <= len(captured):
            if frame_rms(bytes(captured[processed : processed + step])) >= 150:
                speech += 0.02
            processed += step
        if speech >= 1.0:
            break
        await asyncio.sleep(0.2)
    return speech


async def run(delay_s: float, timeout_s: float) -> int:
    global _T0
    _T0 = time.time()
    zh_pcm = read_wav_pcm(AUDIO_DIR / "zh.wav")[: int(16000 * 8) * 2]
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        headers=_CP_HEADERS,
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": "zh", "target_lang": "en", "object_id": ""},
        timeout=15,
    ).json()
    call_id = call["id"]
    print(f"[probe] call={call_id} delay_s={delay_s}", flush=True)
    me = Side(call_id, f"me-{call_id}")
    other = Side(call_id, f"other-{call_id}")
    # 复刻现场:两端进房都不采麦,等 delay_s 后双双发布(传译开关打开)。
    await me.connect(publish_mic=False)
    await other.connect(publish_mic=False)
    await asyncio.sleep(6)  # 解释器派发窗(与 e2e 同款)
    if delay_s > 0:
        await asyncio.sleep(delay_s)
    await me.publish_mic()
    await other.publish_mic()
    ok = False
    note = ""
    try:
        mark = len(other.captured)
        await me.push(zh_pcm)
        speech = await wait_audio(other.captured, mark, timeout_s)
        ok = speech >= 1.0 and any("trans-en" in n.lower() for n in other.trans_names)
        note = f"speech={speech:.1f}s trans={sorted(other.trans_names) or 'NONE'}"
    finally:
        await me.close()
        await other.close()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", headers=_CP_HEADERS, timeout=10)
        except Exception:
            pass
    print(f"[{'PASS' if ok else 'FAIL'}] late-mic delay={delay_s}s {note}", flush=True)
    print(f"[probe] 对照 interp-fwd.log: grep '{call_id}' QWEN3_ASR_TEXT(有=订阅成功)", flush=True)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay-s", type=float, default=16.0, help="进房到麦克风发布的延迟秒数(现场=16)")
    ap.add_argument("--timeout-s", type=float, default=45.0)
    args = ap.parse_args()
    return asyncio.run(run(args.delay_s, args.timeout_s))


if __name__ == "__main__":
    raise SystemExit(main())
