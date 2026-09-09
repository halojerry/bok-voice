"""垫话时序探针:用户讲完一句后,agent 出声(垫话或回复)必须 <2s。

背景(2026-09-09):垫话改走 BackgroundAudioPlayer out-of-band 音轨(独立 track
`background_audio`,本探针两条音轨都收)。改版前垫话被 speech 队列堵在回复后面,
慢轮纯静音 2.5-4s;改版后垫话 ~700ms 出声,感知首声由回复 TTFT 决定变为垫话
定时器决定。断言:用户推完音频到 agent 首声 <2000ms(垫话 ~0.7-1.2s,回复
warm TTFT+TTFB ~2.5-4s,阈值两边都分开);随后累计 ≥1.5s 语音(回复真来了,
唔係误触噪声)。复用 e2e_barge_in 的语音统计/推流工具。

用法:python3 scripts/probe_filler_timing.py(默认 cantonese,需 dev 栈在跑;
可用 runtime python 或 .venv312,需 livekit+httpx)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
import livekit.rtc as rtc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_barge_in import push_pcm, speech_stats, tts_pcm, wait_speech_then_silence  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
LANG = os.environ.get("FILLER_LANG", "cantonese")
# 垫话=eou(0.5-0.9s)+hook+700ms 定时+出声检测 ≈1.6-2.3s;旧通道(纯等回复)
# 实测 2.3-8s+。预算 2.5s + 日志硬判据(BOK_FILLER fired)双保险。
ONSET_BUDGET_MS = int(os.environ.get("FILLER_ONSET_BUDGET_MS", "2500"))
LOG_PATH = Path.home() / "Library/Application Support/BokVoice/logs/agent.log"
TEXT = os.environ.get("FILLER_TEXT", "唔該幫我查下張單到邊度喇。")
# 垫话缓存按 voice+model 做 key:探针人设显式钉 GentleLady(缓存已预合成该音色),
# 音色唔一致时垫话会静默跳过(宁勿出声都唔换声——正确行为,但探针就测唔到)。
PERSONA_VOICE = os.environ.get("FILLER_PERSONA_VOICE", "Cantonese_GentleLady")


async def main() -> None:
    lang = LANG
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={
            "display_name": f"E2E-fillerprobe-{int(time.time())}",
            "role_template": "buyer",
            "language": lang,
            "background": "filler timing probe",
        },
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas?account_id=acc-001",
        json={
            "name": "E2E客服",
            "language": lang,
            "tone": "礼貌专业",
            "reference_audio": PERSONA_VOICE,
        },
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
    # 日志增量基线:BOK_FILLER fired 行不带 call_id,用文件偏移做「本通之后」判定
    log_size_before = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0

    def attach(track):
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
            return
        # 主音轨 + 垫话 out-of-band 音轨都收(垫话在独立 background_audio track 上)
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

    try:
        await room.connect(data["serverUrl"], data["participantToken"])
        print(f"[filler-probe] joined {room_name} (lang={lang})", flush=True)
        audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
        src = rtc.LocalAudioTrack.create_audio_track("e2e-src", audio_source)
        await room.local_participant.publish_track(
            src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        # 等开场白播完(≥1s 语音 + 3s 尾静音)——复用 barge-in 战斗测试口径。
        # 冷 worker 首通 greeting 可能晚到:没等到语音就再来一轮(最多 3 轮)。
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
        probe = len(agent_audio)

        # 推一句用户音频 → 计时 agent 首声(垫话或回复,先到先算)
        pcm = tts_pcm(TEXT, lang)
        await push_pcm(audio_source, pcm)
        t_pushed = time.perf_counter()

        onset_ms = None
        speech_total = 0.0
        while time.perf_counter() - t_pushed < 20:
            s, _, probe = speech_stats(bytes(agent_audio), probe)
            speech_total += s
            if onset_ms is None and speech_total >= 0.12:  # 累计 ≥120ms 语音=真出声
                onset_ms = (time.perf_counter() - t_pushed) * 1000
            if onset_ms is not None and speech_total >= 1.5:
                break
            await asyncio.sleep(0.1)

        filler_fired = False
        if LOG_PATH.exists():
            try:
                # 按字节切片(st_size 是字节):中文日志按字符切会跳过头过的最新行
                new_bytes = LOG_PATH.read_bytes()[log_size_before:]
                filler_fired = b"BOK_FILLER fired" in new_bytes
            except Exception:
                pass
        # 主判据=首声预算(快轮回复自己快/慢轮垫话顶上,两条路都要 <2.5s);
        # filler_fired 是信息位:回复首音频 <700ms 时垫话按设计作废(快轮不垫)。
        # FILLER_REQUIRE=1 强制要求开火(验证垫话通道本身,慢轮场景)。
        require_fired = os.environ.get("FILLER_REQUIRE", "0") == "1"
        ok = (
            onset_ms is not None
            and onset_ms < ONSET_BUDGET_MS
            and speech_total >= 1.5
            and (filler_fired or not require_fired)
        )
        print(
            f"FILLER-PROBE {'PASS' if ok else 'FAIL'} "
            f"first_audio_ms={onset_ms:.0f} budget={ONSET_BUDGET_MS} "
            f"speech_total={speech_total:.2f}s filler_fired={filler_fired}",
            flush=True,
        )
        await asyncio.sleep(2)
        if not ok:
            raise SystemExit(1)
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
