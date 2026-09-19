"""B 线同传延迟探针（P1 评测体系,2026-09-16）——逐句感知 lag,版本回归用。

与 e2e_interpret.py 的区别:e2e 只断言「翻没翻对」(语言标签),本探针度量
「多快翻出来」——对标业界 Average Lagging 口径的工程化简版:

  lag_i = 译文语音起点_i − 源句语音结束_i   (逐句;avg/max 汇总)

真实链路:createCall(kind=interpret) → me/other join → me 分两段推源话音
(段间 0.7s 真静音,触发 VAD 句边界/句级提交) → other 侧带时间戳捕获
trans-<lang> 音轨 → 语音起点检测 + 可选 ASR 回读。

主判据:avg_lag ≤ 预算(BOK_PROBE_LAG_BUDGET_MS,默认 3500——停嘴确认 0.45s
+ ASR finish ~0.2s + MT ~0.3s + TTS 首包 ~0.3s + 播放缓冲,余量见 AGENTS.md
延迟预算表)。ASR 回读仅信息位(人看译文对不对),不参与 PASS/FAIL。

用法:
  .venv312/bin/python scripts/probe_interpret_latency.py
env:
  BOK_PROBE_LAG_BUDGET_MS   逐句/平均 lag 预算(默认 3500)
  BOK_PROBE_LANG_PAIR       默认 "zh,en"
  BOK_PROBE_GLOSSARY        透传建单 glossary(量术语槽前缀的延迟代价,默认空)
前置:python tools/bok.py serve(含 interp-fwd/rev 与 MT :1236)。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import e2e_interpret as e2e  # noqa: E402  (复用 Side/推流/ASR 回读/fleet 常量)


class TimedSide(e2e.Side):
    """在 Side 捕获上叠加 (wall_time, byte_offset) 日志,供语音起点检测。"""

    def __init__(self, call_id: str, identity: str):
        super().__init__(call_id, identity)
        self.timelog: list[tuple[float, int, int]] = []  # (t, start, end) in captured

    async def connect(self) -> None:
        import httpx
        from livekit import rtc

        data = httpx.post(
            f"{e2e.CONTROL_PLANE_URL}/api/token",
            json={"account_id": "acc-001", "call_id": self.call_id, "participant_identity": self.identity},
            headers=e2e._CP_HEADERS,
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
            if not getattr(track, "name", "").startswith("trans-"):
                return
            print(f"  [track] {self.identity} <- {track.name}", flush=True)

            async def _read():
                try:
                    stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
                    async for event in stream:
                        frame = getattr(event, "frame", event)
                        data = bytes(frame.data)
                        start = len(self.captured)
                        self.captured.extend(data)
                        self.timelog.append((time.monotonic(), start, len(self.captured)))
                except Exception:
                    pass

            self._tasks.append(asyncio.get_running_loop().create_task(_read()))

        self.room.on("track_subscribed", on_track)
        for participant in self.room.remote_participants.values():
            for pub in participant.track_publications.values():
                track = getattr(pub, "track", None)
                if track is not None:
                    on_track(track, pub, participant)


def speech_onset_after(timelog, captured: bytearray, t_after: float) -> float | None:
    """timelog 里 t_after 之后第一段语音(RMS≥150,累计 ≥150ms)的墙钟时刻。"""
    step = 320
    run = 0.0
    for t, start, end in timelog:
        if t <= t_after:
            continue
        chunk = bytes(captured[start:end])
        speech = sum(
            1
            for i in range(0, len(chunk) - step + 1, step)
            if e2e.frame_rms(chunk[i : i + step]) >= 150
        )
        if speech:
            run += speech * 0.02
            if run >= 0.15:
                return t
        else:
            run = 0.0
    return None


async def main() -> int:
    budget_ms = int(os.environ.get("BOK_PROBE_LAG_BUDGET_MS", "3500"))
    src_lang, tgt_lang = os.environ.get("BOK_PROBE_LANG_PAIR", "zh,en").split(",")
    glossary = os.environ.get("BOK_PROBE_GLOSSARY", "")

    seg_seconds = 4.0
    gap_seconds = 0.7
    pcm = e2e.read_wav_pcm(e2e.AUDIO_DIR / f"{src_lang}.wav")[: int(16000 * seg_seconds * 2) * 2]

    created = e2e.httpx.post(
        f"{e2e.CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "kind": "interpret", "mode": "live", "direction": "interpret",
              "language": src_lang, "target_lang": tgt_lang, "object_id": "", "glossary": glossary},
        headers=e2e._CP_HEADERS,
        timeout=15,
    ).json()
    call_id = created["id"]
    me = TimedSide(call_id, f"me-{call_id}")
    other = TimedSide(call_id, f"other-{call_id}")
    await me.connect()
    await other.connect()
    await asyncio.sleep(6)  # 等 RoomAgentDispatch 拉起解释器

    segs: list[dict] = []
    try:
        for i in range(2):
            t0 = time.monotonic()
            await me.push(pcm)
            src_end = t0 + seg_seconds  # 实时说话人口径(推流 0.8× 速只令 lag 偏保守)
            segs.append({"src_end": src_end, "onset": None})
            if i == 0:
                await asyncio.sleep(gap_seconds)

        # 等译文音频收敛:captured 长度 4s 无增长或总窗 60s
        deadline = time.monotonic() + 60
        last_len, last_change = len(other.captured), time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            if len(other.captured) != last_len:
                last_len, last_change = len(other.captured), time.monotonic()
            elif time.monotonic() - last_change > 4:
                break

        for seg in segs:
            seg["onset"] = speech_onset_after(other.timelog, other.captured, seg["src_end"])
    finally:
        await me.close()
        await other.close()
        try:
            e2e.httpx.post(f"{e2e.CONTROL_PLANE_URL}/api/calls/{call_id}/hangup",
                           headers=e2e._CP_HEADERS, timeout=10)
        except Exception:
            pass

    lags: list[float] = []
    readbacks: list[str] = []
    for i, seg in enumerate(segs):
        if seg["onset"] is None:
            readbacks.append("-")
            continue
        lag_ms = (seg["onset"] - seg["src_end"]) * 1000
        lags.append(lag_ms)
        # 信息位:该句译文窗口 ASR 回读(不参与判定);窗口=本句起点到下句起点
        next_onset = segs[i + 1]["onset"] if i + 1 < len(segs) and segs[i + 1]["onset"] else None
        lo = min((s for t, s, _ in other.timelog if t >= seg["onset"] - 0.1), default=None)
        hi_cutoff = (
            min((s for t, s, _ in other.timelog if next_onset and t >= next_onset - 0.1), default=None)
            or len(other.captured)
        )
        if lo is not None:
            _, text = e2e.asr_transcribe(bytes(other.captured[lo:hi_cutoff]))
            readbacks.append(text[:40])
        else:
            readbacks.append("-")

    ok = len(lags) == len(segs) and all(l <= budget_ms for l in lags)
    avg = sum(lags) / len(lags) if lags else -1
    print(f"readbacks={readbacks}", flush=True)
    print(
        f"INTERPRET_LATENCY_PROBE segs={len(segs)} lag_ms={[round(l) for l in lags]} "
        f"avg_ms={round(avg)} budget_ms={budget_ms} "
        f"{'PASS' if ok else 'FAIL'}",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
