#!/usr/bin/env python3
"""0913 实机验收·场景驱动(C1 暂停黑洞 / C3+C4 号长闸与拜拜分流)。

场景A(zh):开场→supervisor 暂停→(期待:稍等一句出声+暂停期语音轮 gen=paused
落库+零 flow 推进)→resume→再语音(期待:回复出声)。
场景B(cantonese):走完 8 步到收号步→报 7 位号(期待:LEN_CHECK_SUSPECT+重讲
不确认)→报 8 位(期待:复述确认)→拜拜(期待:farewell 分流,disposition=
scheduled/polite_close 而非 declined)。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
from livekit import rtc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.e2e_trilingual_livekit import frame_rms, tts_pcm  # noqa: E402

CP = "http://127.0.0.1:8000"
LOG = Path.home() / "Library/Application Support/BokVoice/logs/agent.log"
AGENTS = []


class Ear:
    """收集 agent 音轨(同 e2e run_case 的读取器)。"""

    def __init__(self, room: rtc.Room):
        self.audio = bytearray()
        self.tasks = []
        room.on("track_subscribed", self._on)

    def _on(self, track, pub, participant):
        if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO) or getattr(track, "name", "") != "roomio_audio":
            return

        async def _read():
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                async for ev in stream:
                    f = getattr(ev, "frame", ev)
                    self.audio.extend(bytes(f.data))
            except Exception:
                pass

        self.tasks.append(asyncio.get_running_loop().create_task(_read()))

    def speech_secs(self, since: int = 0) -> float:
        step = 320
        n = 0.0
        i = since
        while i + step <= len(self.audio):
            if frame_rms(bytes(self.audio[i : i + step])) >= 200:
                n += 0.02
            i += step
        return n

    async def close(self, room):
        room.off("track_subscribed", self._on)
        for t in self.tasks:
            t.cancel()


async def speak(src: rtc.AudioSource, text: str, lang: str) -> None:
    pcm = tts_pcm(text, lang)
    chunk = 1600
    for i in range(0, len(pcm), chunk):
        seg = pcm[i : i + chunk]
        await src.capture_frame(
            rtc.AudioFrame(data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2)
        )
        await asyncio.sleep(0.08)


async def wait_reply(ear: Ear, min_speech: float = 0.8, timeout: float = 60.0) -> float:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        s = ear.speech_secs()
        if s >= min_speech:
            # 再等到停止增长 3s
            prev = -1
            still = 0
            while still < 3:
                await asyncio.sleep(1)
                cur = ear.speech_secs()
                if abs(cur - prev) < 0.02:
                    still += 1
                else:
                    still = 0
                prev = cur
            return ear.speech_secs()
        await asyncio.sleep(1)
    return ear.speech_secs()


async def setup_call(lang: str, tag: str, template_id: str = ""):
    with httpx.Client(timeout=10) as c:
        obj = c.post(f"{CP}/api/objects?account_id=acc-001", json={
            "display_name": f"边角-{tag}-{int(time.time())}", "language": lang,
            **({"template_id": template_id} if template_id else {}),
        }).json()
        persona = c.post(f"{CP}/api/personas?account_id=acc-001", json={
            "name": "验收客服", "language": lang}).json()
        call = c.post(f"{CP}/api/calls", json={
            "account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
            "mode": "live", "direction": "webrtc", "language": lang}).json()
    room = rtc.Room()
    tok = httpx.post(f"{CP}/api/token", json={"account_id": "acc-001", "call_id": call["id"]}, timeout=10).json()
    await room.connect(tok["serverUrl"], tok["participantToken"])
    src = rtc.AudioSource(sample_rate=16000, num_channels=1)
    tr = rtc.LocalAudioTrack.create_audio_track("acc-src", src)
    await room.local_participant.publish_track(tr, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    return call["id"], room, src


def turns_of(call_id: str) -> list[dict]:
    r = httpx.get(f"{CP}/api/calls/{call_id}/turns", timeout=10)
    return r.json() if isinstance(r.json(), list) else []


def call_row(call_id: str) -> dict:
    return httpx.get(f"{CP}/api/calls/{call_id}", timeout=10).json()


async def scenario_pause() -> bool:
    print("== 场景A:暂停黑洞(zh)==")
    cid, room, src = await setup_call("zh", "暂停验收", template_id="b0d50586a040")
    ear = Ear(room)
    ok = True
    try:
        await wait_reply(ear, min_speech=0.5, timeout=45)  # 开场白
        await asyncio.sleep(1)
        log_mark = LOG.read_text(errors="replace") if LOG.exists() else ""
        httpx.post(f"{CP}/api/supervisor/{cid}/pause-agent", timeout=10)
        await asyncio.sleep(8)  # 等「稍等一下」播完(旧 4s 窗内推音会被 speaking 期吞)
        grew = ear.speech_secs() >= 0.3
        print(f"①暂停进入播报出声={grew}")
        ok &= grew
        # 暂停期语音轮:应落库 gen=paused 且零 assistant 轮(长句+两遍,确保成轮)
        for _ in range(2):
            await speak(src, "你们这个赔偿方案到底怎么办理呢，我现在想问一下", "zh")
            await asyncio.sleep(4)
        await asyncio.sleep(6)
        ts = turns_of(cid)
        paused_rows = [t for t in ts if t.get("gen") == "paused"]
        print(f"②暂停期轮落库 gen=paused 条数={len(paused_rows)}")
        ok &= len(paused_rows) >= 1
        log_now = LOG.read_text(errors="replace") if LOG.exists() else ""
        adv = [l for l in (log_now[len(log_mark):] if log_mark else log_now).splitlines()
               if "rule=auto" in l or "judge(bg)=confirm step" in l]
        print(f"③暂停期 flow 零推进 推进行数={len(adv)}")
        ok &= len(adv) == 0
        # resume → 出声回复
        ear.audio.clear()
        httpx.post(f"{CP}/api/supervisor/{cid}/resume-agent", timeout=10)
        await speak(src, "你好还在吗", "zh")
        s = await wait_reply(ear, min_speech=0.6, timeout=45)
        print(f"④resume 后回复出声 speech={s:.1f}s")
        ok &= s >= 0.6
    finally:
        await ear.close(room)
        await room.disconnect()
        try:
            httpx.post(f"{CP}/api/calls/{cid}/hangup", timeout=10)
        except Exception:
            pass
    print("PAUSE_PROBE_" + ("PASS" if ok else "FAIL"))
    return ok


async def scenario_wa_farewell() -> bool:
    print("== 场景B:号长闸+拜拜分流(cantonese)==")
    cid, room, src = await setup_call("cantonese", "粤号长验收", template_id="febeeeebac97")
    ear = Ear(room)
    ok = True
    try:
        await wait_reply(ear, min_speech=0.5, timeout=45)  # 开场白
        log_mark = len(LOG.read_text(errors="replace")) if LOG.exists() else 0
        # step2 问货品 → 唔记得(UNCLEAR 通知步即推)
        await speak(src, "唔记得喇", "cantonese"); await wait_reply(ear)
        # step3 平台
        await speak(src, "拼多多", "cantonese"); await wait_reply(ear, min_speech=1.0)  # step4 赔偿短结论直念
        # step4 确认
        await speak(src, "可以呀", "cantonese"); await wait_reply(ear)  # step5 收号步引导
        # step5 报 7 位(粤)→ LEN_CHECK_SUSPECT + 重讲
        await speak(src, "我嘅WhatsApp係一二三四五六七。", "cantonese")
        await wait_reply(ear, min_speech=0.6, timeout=45)  # 期待「唔该再讲一次完整号码」
        # 报 8 位 → 复述确认
        await speak(src, "九八七六五四三二", "cantonese")
        await wait_reply(ear, min_speech=1.0, timeout=45)
        # 道别
        await speak(src, "拜拜", "cantonese")
        await asyncio.sleep(12)  # 等 farewell 收线+结算
        log_tail = LOG.read_text(errors="replace")[log_mark:] if LOG.exists() else ""
        len_suspect = "LEN_CHECK_SUSPECT" in log_tail
        print(f"①7位号 LEN_CHECK_SUSPECT={len_suspect}")
        ok &= len_suspect
        ts = turns_of(cid)
        wa_rows = [t for t in ts if str(t.get("provider") or "") in ("wa-stash", "wa-merged")]
        print(f"②WA 轮落库(stash/merged) 条数={len(wa_rows)}")
        ok &= len(wa_rows) >= 1 or any("WhatsApp" in str(t.get("transcript") or "") for t in ts)
        row = call_row(cid)
        disp = str(row.get("disposition") or "")
        status = str(row.get("status") or "")
        print(f"③道别分流 disposition={disp!r} status={status!r}(期待 scheduled/polite_close,非 declined)")
        ok &= disp in ("scheduled", "polite_close") or (disp == "" and status == "ended")
        farewell_log = "farewell -> closing" in log_tail
        print(f"④FAREWELL 分流日志={farewell_log}")
        ok &= farewell_log or disp == "scheduled"
    finally:
        await ear.close(room)
        await room.disconnect()
        try:
            httpx.post(f"{CP}/api/calls/{cid}/hangup", timeout=10)
        except Exception:
            pass
    print("WA_FAREWELL_PROBE_" + ("PASS" if ok else "FAIL"))
    return ok


async def main() -> int:
    a = await scenario_pause()
    b = await scenario_wa_farewell()
    return 0 if (a and b) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
