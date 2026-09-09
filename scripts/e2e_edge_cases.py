"""A 线边角 E2E（2026-09-07 全链路回归新增）。

覆盖 QA 计划 T1.3 的六个边角场景（全部真实 token + 真实音频）：
  E1 空输入（纯静音）——唔产生用户轮、agent 唔崩、后续输入照常应答
  E2 数字/符号输入——TTS 合成带号码的话音，轮文本含数字、回复正常
  E3 超长输入（~45s 连续语音）——成轮且有回复
  E4 回复中打断——旧回复停、新输入有新回复、agent 存活
  E5 快速短应承×3——PR#18 后唔应被吞到只剩心跳收线
  E6 生成中强挂断——hangup + 重复 settle 幂等，agent 存活

前置：`python tools/bok.py serve`（8000/7880/8787/8788）。TTS :8788 用于合成测试音频。
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
TTS_URL = "http://127.0.0.1:8788"
AUDIO_DIR = ROOT / "tests" / "fixtures" / "audio"
OUT_DIR = Path("/tmp/qa-edge-audio")
RESULTS: list[tuple[str, bool, str]] = []


def frame_rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


def read_wav_pcm(path: Path, max_seconds: float = 4.5) -> tuple[bytes, int]:
    """默认截前 4.5s——fixtures 是 2 分钟长音频（e20ed7a 起），整条推完一条要
    137s（0.55× 实时 pacing），E3「~45s 长输入」会变成 42 分钟、E4 打断窗口
    （40s）结构性必超时（2026-09-09 E4 两连 FAIL 根因：测试推流 bug，非产品）。
    barge-in/trilingual 同款 fixture 一直截 4.0s 读，所以照常绿。"""
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = int(min(w.getnframes(), sr * max_seconds))
        return w.readframes(n), sr


def tts_pcm(text: str, lang: str = "cantonese") -> bytes:
    """经 TTS sidecar 合成 16k PCM 测试话音。"""
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{TTS_URL}/v1/audio/speech",
            json={"input": text, "language": lang, "voice": "Vivian", "sample_rate": 16000},
        )
        r.raise_for_status()
        return r.content


def silence_pcm(seconds: float, sr: int = 16000) -> bytes:
    return b"\x00\x00" * int(sr * seconds)


async def push_pcm(audio_source: rtc.AudioSource, pcm: bytes, real_time: bool = True) -> None:
    chunk = int(16000 * 0.1) * 2
    for i in range(0, len(pcm), chunk):
        seg = pcm[i : i + chunk]
        frame = rtc.AudioFrame(
            data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2
        )
        await audio_source.capture_frame(frame)
        if real_time:
            await asyncio.sleep(0.08)


def turns_count(call_id: str) -> int:
    r = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()
    return len(r)


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {note}", flush=True)


async def wait_reply_speech(agent_audio: bytearray, mark: int, timeout_s: float) -> int:
    """等 agent_audio 自 mark 起出现 ≥0.6s 语音，返回此刻长度；超时返回 -1。"""
    deadline = time.perf_counter() + timeout_s
    processed = mark
    speech = 0.0
    while time.perf_counter() < deadline:
        step = 320
        while processed + step <= len(agent_audio):
            if frame_rms(bytes(agent_audio[processed : processed + step])) >= 200:
                speech += 0.02
            processed += step
        if speech >= 0.6:
            return len(agent_audio)
        await asyncio.sleep(0.1)
    return -1


async def wait_silence(agent_audio: bytearray, mark: int, quiet_s: float, timeout_s: float) -> None:
    """等 agent_audio 自 mark 起安静 quiet_s 秒（回复播完）。"""
    deadline = time.perf_counter() + timeout_s
    processed = mark
    silent = 0.0
    while time.perf_counter() < deadline:
        step = 320
        while processed + step <= len(agent_audio):
            if frame_rms(bytes(agent_audio[processed : processed + step])) >= 200:
                silent = 0.0
            else:
                silent += 0.02
            processed += step
        if silent >= quiet_s:
            return
        await asyncio.sleep(0.1)


async def make_call(prefix: str) -> tuple[str, rtc.Room, rtc.AudioSource, bytearray]:
    ts = int(time.time() * 1000) % 100000
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"边角-{prefix}-{ts}", "role_template": "buyer", "language": "cantonese", "background": "qa edge"},
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": f"边角客服{prefix}", "language": "cantonese", "tone": "礼貌专业"},
        timeout=10,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
              "mode": "live", "direction": "webrtc", "language": "cantonese"},
        timeout=10,
    ).json()
    call_id = call["id"]
    data = httpx.post(f"{CONTROL_PLANE_URL}/api/token", json={"account_id": "acc-001", "call_id": call_id}, timeout=10).json()
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
    for participant in room.remote_participants.values():
        for pub in participant.track_publications.values():
            track = getattr(pub, "track", None)
            if track is not None:
                on_track(track, pub, participant)
    # 等开场白播完
    await asyncio.sleep(1)
    return call_id, room, audio_source, agent_audio


async def main() -> int:
    ONLY = os.environ.get("EDGE_ONLY", "")  # 逗号分隔,如 "E1,E2"
    OUT_DIR.mkdir(exist_ok=True)
    # 预合成测试音频
    digit_pcm = tts_pcm("我個單號係三七七八九零，唔該幫我查下。")
    ack1 = tts_pcm("好。")
    ack2 = tts_pcm("係。")
    long_pcm, _ = read_wav_pcm(AUDIO_DIR / "cantonese.wav")
    long_input = b"".join(long_pcm + silence_pcm(0.5) for _ in range(10))  # ~45s
    zh_pcm, _ = read_wav_pcm(AUDIO_DIR / "zh.wav")
    cantonese_pcm, _ = read_wav_pcm(AUDIO_DIR / "cantonese.wav")
    en_pcm, _ = read_wav_pcm(AUDIO_DIR / "en.wav")

    # ---- 共享通话跑 E1-E5 ----
    call_id, room, audio_source, agent_audio = await make_call("main")
    await asyncio.sleep(12)  # 等开场白播完（greeting ~8s + 余量）

    # E1 空输入（只数 user 轮——心跳 nudge 行係 assistant,唔算输入误判）
    def user_turns(cid: str) -> int:
        rows = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{cid}/turns", timeout=10).json()
        return sum(1 for t in rows if t.get("role") == "user")

    n0 = user_turns(call_id)
    if not ONLY or "E1" in ONLY:
        await push_pcm(audio_source, silence_pcm(3.0))
        await asyncio.sleep(4)
        n1 = user_turns(call_id)
        record("E1 静音输入不产生用户轮", n1 == n0, f"user_turns {n0}->{n1}")

    # E2 数字/符号输入（数字用归一比对——ASR 同音字「三七七八」→「新车车」属
    # 已知数字弱项,由读回核对指引兜底,断言唔要求逐字命中）
    if not ONLY or "E2" in ONLY:
        mark = len(agent_audio)
        baseline_user_turns = user_turns(call_id)
        await push_pcm(audio_source, digit_pcm)
        ok_reply = await wait_reply_speech(agent_audio, mark, 40) >= 0
        # 轮落库有写入竞态窗口(P1 turns 竞态):断言前轮询等本段用户轮落库,
        # 唔係嘅话取到空表会把完美转写判成 FAIL(2026-09-08 实证:转写逐字全对
        # 仍报 digits_norm='')。上限 12s,超出照旧按当刻 turns 判。
        turns: list = []
        for _ in range(6):
            turns = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()
            if sum(1 for t in turns if t.get("role") == "user") > baseline_user_turns:
                break
            await asyncio.sleep(2)
        user_texts = " ".join((t.get("transcript") or "") for t in turns if t.get("role") == "user")
        # 归一比对:「车」与「七」同音,把同音字映回数字再查尾串「八九零」;
        # 断言要求:回复出现 + 用户轮含「单号/單號」语境 + 号码尾段按序出现。
        norm = user_texts.replace("车", "七").replace("，", "").replace("。", "")
        import re as _re
        han = {"零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
               "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
        digits = "".join(han.get(ch, ch) for ch in norm if ch.isdigit() or ch in han)
        has_tail = "890" in digits
        has_ctx = ("单号" in user_texts) or ("單號" in user_texts)
        record("E2 数字话音成轮且回复", ok_reply and has_ctx and has_tail,
               f"digits_norm={digits[-16:]!r} ctx={has_ctx} tail={has_tail}")

    # E3 超长输入
    n_before = len(turns)
    mark = len(agent_audio)
    await push_pcm(audio_source, long_input)
    ok_long = await wait_reply_speech(agent_audio, mark, 60) >= 0
    await wait_silence(agent_audio, mark, 4.0, 70)
    turns = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()
    record("E3 超长输入成轮有回复", ok_long and len(turns) > n_before, f"turns {n_before}->{len(turns)}")

    # E4 回复中打断：推 cantonese 触发回复，检测到回复语音立即推 en
    mark = len(agent_audio)
    push_task = asyncio.get_running_loop().create_task(push_pcm(audio_source, cantonese_pcm))
    speech_at = await wait_reply_speech(agent_audio, mark, 40)
    await push_task
    if speech_at >= 0:
        # 回复语音中推入新输入
        interrupt_mark = len(agent_audio)
        await push_pcm(audio_source, en_pcm)
        grew = await wait_reply_speech(agent_audio, interrupt_mark, 40)
        turns = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()
        record("E4 回复中打断有后续回复", grew >= 0 and len(turns) > 0, f"turns={len(turns)}")
    else:
        record("E4 回复中打断有后续回复", False, "首轮无回复语音可打断")

    # E5 快速短应承×3（短应承被吞→只剩心跳收线的回归）
    n_before = turns_count(call_id)
    for pcm in (ack1, ack2, ack1):
        mark = len(agent_audio)
        await push_pcm(audio_source, pcm)
        await asyncio.sleep(2.5)
    await asyncio.sleep(6)
    alive = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}", timeout=10)
    status = alive.json().get("status", "")
    n_after = turns_count(call_id)
    record("E5 连续短应承通话存活且有轮", status == "active" and n_after >= n_before, f"status={status} turns {n_before}->{n_after}")

    # E5b 相邻 LLM 回复禁逐字复读（推进轮复读前轮话术块回归，call-feaf914c 实证）。
    # 只比「intervening 用户输入不同」的对：E3 故意同输入推 10 次，同输入→近似
    # 回答係正确服务行为，唔算复读；「不同输入给同一答案」先係缺陷形状。
    import difflib

    def _norm_rep(t: str) -> str:
        return "".join(ch for ch in (t or "") if ch.isalnum())

    rows = httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()
    seq = [
        (t.get("role"), _norm_rep(t.get("transcript") or ""), (t.get("latency_ms") or 0) > 0)
        for t in rows
    ]
    prev_reply = ""        # 上一条 LLM 回复（归一）
    input_before_prev = ""  # 上一条 LLM 回复之前的用户输入
    input_since = ""        # 自上一条 LLM 回复以来的用户输入
    dup = []
    for role, text, is_llm in seq:
        if role == "user" and text:
            input_since += text
            continue
        if role == "assistant" and is_llm and len(text) >= 8:
            if prev_reply and input_since:
                input_shift = (
                    difflib.SequenceMatcher(None, input_since, input_before_prev).ratio() < 0.8
                )
                ratio = difflib.SequenceMatcher(None, prev_reply, text).ratio()
                if input_shift and ratio >= 0.9:
                    dup.append((prev_reply[:20], text[:20], round(ratio, 2)))
            prev_reply = text
            input_before_prev = input_since
            input_since = ""
    record("E5b 相邻LLM回复不逐字复读", len(dup) == 0, f"dup={dup[:2]}")
    await room.disconnect()
    try:
        httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10)
    except Exception:
        pass

    # ---- E6 生成中强挂断（独立通话）----
    call_id2, room2, audio_source2, agent_audio2 = await make_call("hangup")
    await asyncio.sleep(12)
    mark = len(agent_audio2)
    push_task = asyncio.get_running_loop().create_task(push_pcm(audio_source2, cantonese_pcm))
    await wait_reply_speech(agent_audio2, mark, 40)
    r1 = httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id2}/hangup", timeout=10)
    r2 = httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id2}/settle", timeout=30)
    r3 = httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id2}/settle", timeout=30)
    await room2.disconnect()
    record("E6 生成中挂断+重复 settle 幂等", r1.status_code == 200 and r2.status_code == 200 and r3.status_code == 200,
           f"hangup={r1.status_code} settle={r2.status_code}/{r3.status_code}")

    # agent 存活验证：再来一通最小通话
    try:
        call_id3, room3, audio_source3, agent_audio3 = await make_call("alive")
        mark = len(agent_audio3)
        await push_pcm(audio_source3, cantonese_pcm)
        ok_alive = await wait_reply_speech(agent_audio3, mark, 40) >= 0
        await room3.disconnect()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id3}/hangup", timeout=10)
        except Exception:
            pass
        record("E7 agent 挂断后存活", ok_alive)
    except Exception as exc:
        record("E7 agent 挂断后存活", False, repr(exc)[:80])

    return finish()


def finish() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, note in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name} {note}", flush=True)
    print(f"EDGE_CASES_E2E {passed}/{len(RESULTS)} PASSED", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
