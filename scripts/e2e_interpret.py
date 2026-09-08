"""B 线同传 E2E（2026-09-07 全链路回归新增）——v2 双 AgentSession 解释器首次端到端。

真实链路：createCall(kind=interpret) → me/other 双 rtc 客户端 join（官方 token，
identity=me-<room>/other-<room>，me 侧 token 携带 RoomAgentDispatch 自动拉起
fwd/rev 两个解释器）→ me 推源语言话音 → other 捕获 trans-<target> 音轨 →
ASR 回读断言目标语 → turns 双语落库 → hangup → settle 蒸馏落知识。

场景：
  I1 fwd 主链路（me 说 zh → other 听 en）
  I2 rev 反向（other 说 en → me 听 zh）
  I3 连续启停 ×3（worker 复用无崩）
  I4 长流 45s 连续讲话（断句稳定、有输出）

前置：`python tools/bok.py serve`（含 interp-fwd/interp-rev 8082/8083、MT :1236）。
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
ASR_URL = "http://127.0.0.1:8787"
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"
RESULTS: list[tuple[str, bool, str]] = []


def frame_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def read_wav_pcm(path: Path, max_seconds: float = 600.0) -> bytes:
    with wave.open(str(path), "rb") as w:
        n = int(min(w.getnframes(), w.getframerate() * max_seconds))
        return w.readframes(n)


def asr_transcribe(pcm16: bytes) -> tuple[str, str]:
    step = 320
    start, end = 0, len(pcm16)
    for i in range(0, len(pcm16) - step + 1, step):
        if frame_rms(pcm16[i : i + step]) >= 100:
            start = i
            break
    for i in range(len(pcm16) - step, -1, -step):
        if frame_rms(pcm16[i : i + step]) >= 100:
            end = i + step
            break
    body = pcm16[start:end]
    if len(body) < 3200:
        return "", ""
    with httpx.Client(timeout=60) as client:
        s = client.post(f"{ASR_URL}/api/start").json()["session_id"]
        for i in range(0, len(body), 3200):
            client.post(f"{ASR_URL}/api/chunk", params={"session_id": s}, content=body[i : i + 3200])
        out = client.post(f"{ASR_URL}/api/finish", params={"session_id": s}).json()
        return str(out.get("language") or ""), str(out.get("text") or "")


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {note}", flush=True)


class Side:
    """一个同传参与端：房间 + 麦克风推流 + 捕获所有远端音轨。"""

    def __init__(self, call_id: str, identity: str):
        self.call_id = call_id
        self.identity = identity
        self.room = rtc.Room()
        self.audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        self.captured = bytearray()
        self._tasks: list[asyncio.Task] = []

    async def connect(self) -> None:
        data = httpx.post(
            f"{CONTROL_PLANE_URL}/api/token",
            json={"account_id": "acc-001", "call_id": self.call_id, "participant_identity": self.identity},
            timeout=15,
        ).json()
        await self.room.connect(data["serverUrl"], data["participantToken"])
        src = rtc.LocalAudioTrack.create_audio_track("qa-src", self.audio_source)
        await self.room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        def on_track(track, pub, participant):
            if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
                return
            # 只捕获解释器发布的翻译音轨 trans-<lang>;不然会把自己/对端的
            # 麦克风原声也收进来,语言断言全被原声污染(2026-09-07 首跑实证)。
            if not getattr(track, "name", "").startswith("trans-"):
                return
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


async def wait_translated(captured: bytearray, mark: int, want_tag: str, timeout_s: float) -> tuple[bool, str, str]:
    """等 captured 自 mark 起出现语音，收够后 ASR 回读断言目标语。"""
    deadline = time.perf_counter() + timeout_s
    speech = 0.0
    processed = mark
    while time.perf_counter() < deadline:
        step = 320
        while processed + step <= len(captured):
            if frame_rms(bytes(captured[processed : processed + step])) >= 150:
                speech += 0.02
            processed += step
        if speech >= 1.2:
            break
        await asyncio.sleep(0.2)
    # 语音出现后再收 3s（翻译音频可能分句到达）
    await asyncio.sleep(3.0)
    lang, text = asr_transcribe(bytes(captured[mark:]))
    # 严格语言断言（len 兜底会让原声泄漏蒙混过关,已删）
    ok = bool(text) and want_tag.lower() in (lang or "").lower()
    return ok, lang, text


async def run_one(name: str, src_pcm: bytes, rev_pcm: bytes | None, timeout_s: float = 75.0) -> dict:
    ts = int(time.time() * 1000) % 1000000
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": "zh", "target_lang": "en", "object_id": ""},
        timeout=15,
    ).json()
    call_id = call["id"]
    me = Side(call_id, f"me-{call_id}")
    other = Side(call_id, f"other-{call_id}")
    await me.connect()
    await other.connect()
    # 等 RoomAgentDispatch 拉起解释器（worker 注册/派发有秒级延迟;首通易竞态）
    await asyncio.sleep(6)
    info = {"call_id": call_id, "fwd_ok": False, "fwd_text": "", "rev_ok": False, "rev_text": ""}
    try:
        # fwd：me 说 zh → other 听 en
        mark_other = len(other.captured)
        await me.push(src_pcm)
        ok, lang, text = await wait_translated(other.captured, mark_other, "English", timeout_s)
        info["fwd_ok"], info["fwd_text"] = ok, text[:60]
        # rev：other 说 en → me 听 zh
        if rev_pcm is not None:
            mark_me = len(me.captured)
            await other.push(rev_pcm)
            ok2, lang2, text2 = await wait_translated(me.captured, mark_me, "Chinese", timeout_s)
            info["rev_ok"], info["rev_text"] = ok2, text2[:60]
    finally:
        await me.close()
        await other.close()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10)
        except Exception:
            pass
    return info


async def main() -> int:
    # 单句切片(~8s):135s 全长直推会把一轮拖成 ~108s(0.1s 音频/0.08s sleep 节奏),
    # 双向+启停×3 全套变 10 分钟级;断句稳定性归 I4 长流专门验。
    zh_pcm = read_wav_pcm(AUDIO_DIR / "zh.wav")[: int(16000 * 8) * 2]
    en_pcm = read_wav_pcm(AUDIO_DIR / "en.wav")[: int(16000 * 8) * 2]
    long_pcm = read_wav_pcm(AUDIO_DIR / "zh.wav") + b"".join(
        read_wav_pcm(AUDIO_DIR / "zh.wav") for _ in range(8)
    )

    # I1+I2 fwd/rev 双向
    info = await run_one("dual", zh_pcm, en_pcm)
    record("I1 fwd: me(zh)→other 听到英文输出", info["fwd_ok"], info["fwd_text"])
    record("I2 rev: other(en)→me 听到中文输出", info["rev_ok"], info["rev_text"])
    # turns 双语落库(2026-09-07 审计闭环起原文/译文拆成两条,language 字段区分
    # ——旧断言查单行同含「原文：译文：」会永久假红)
    turns = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{info['call_id']}/turns", timeout=10).json()
    orig = [t for t in turns if str(t.get("transcript") or "").startswith("原文：")]
    tran = [t for t in turns if str(t.get("transcript") or "").startswith("译文：")]
    record("I1b turns 原文/译文分行落库", len(orig) >= 1 and len(tran) >= 1,
           f"orig={len(orig)} tran={len(tran)} turns={len(turns)}")

    # I3 连续启停 ×3
    ok_all = True
    note = ""
    for i in range(3):
        info = await run_one(f"churn{i}", zh_pcm, None, timeout_s=50.0)
        if not info["fwd_ok"]:
            ok_all = False
            note = f"第 {i + 1} 通失败 {info['fwd_text']!r}"
            break
    record("I3 连续启停×3 worker 复用", ok_all, note)

    # I4 长流 45s 连续讲话
    info = await run_one("long", long_pcm[: int(16000 * 45) * 2], None, timeout_s=110.0)
    record("I4 长流 45s 断句有输出", info["fwd_ok"], info["fwd_text"])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, note in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name} {note}", flush=True)
    print(f"INTERPRET_E2E {passed}/{len(RESULTS)} PASSED", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
