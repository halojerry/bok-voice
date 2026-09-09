"""品牌词轮次完整性探针（S3 拼多多漏字专项）。

背景：真实通话把「拼多多」说成「多多」——定性为轮次切分问题（整句解码
4/4 全对），不是 ASR 识别错误。流式滑窗微停顿把品牌词拦腰切成两轮时，
语义被破坏。本探针用渲染音频量化「品牌词完整率」作为基线/实验对比：

  用例 = 品牌句（粤/普）× 推流形态（连续 / 品牌词前 0.5s 微停顿(跨 VAD 0.45s 门) /
         品牌词后 0.5s 微停顿）
  判定 = 提交的某个 user 轮文本完整包含品牌词（单轮内不拆）

用法：
  .venv312/bin/python scripts/probe_brand_words.py            # 基线
  结果落 JSON：scripts/.probe_brand_words.<tag>.json
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

import httpx
from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
TAG = os.environ.get("PROBE_TAG", "baseline")

# 品牌句：词内含三字品牌词，粤/普各两条；品牌词放句中（最易被切的位置）。
CASES: list[dict] = [
    {"lang": "zh", "text": "我在拼多多买的东西还没有到货", "brand": "拼多多"},
    {"lang": "zh", "text": "我上一单就是淘宝买的", "brand": "淘宝"},
    {"lang": "cantonese", "text": "我喺拼多多買嘅嘢仲未到", "brand": "拼多多"},
    {"lang": "cantonese", "text": "我件貨係京東買的", "brand": "京東"},
]


def tts_pcm(text: str, lang: str = "cantonese") -> bytes:
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{TTS_URL}/v1/audio/speech",
            json={"input": text, "language": lang, "voice": "Vivian", "sample_rate": 16000},
        )
        r.raise_for_status()
        return r.content


def silence_pcm(seconds: float, sr: int = 16000) -> bytes:
    return b"\x00\x00" * int(sr * seconds)


def frame_rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if not n:
        return 0.0
    frames = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(x * x for x in frames) / n)


async def push_pcm(audio_source: rtc.AudioSource, pcm: bytes) -> None:
    chunk = int(16000 * 0.1) * 2
    for i in range(0, len(pcm), chunk):
        seg = pcm[i : i + chunk]
        frame = rtc.AudioFrame(
            data=seg, sample_rate=16000, num_channels=1, samples_per_channel=len(seg) // 2
        )
        await audio_source.capture_frame(frame)
        await asyncio.sleep(0.08)


def turns_of(call_id: str) -> list[dict]:
    return httpx.get(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/turns", timeout=10).json()


async def make_call(courier: str = "") -> tuple[str, rtc.Room, rtc.AudioSource, bytearray]:
    """courier 进对象档案 → asr_hotword_context 词表 → join-hold 词表前缀门可见。"""
    ts = int(time.time() * 1000) % 100000
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"探针-品牌-{ts}", "role_template": "buyer", "language": "cantonese", "background": "probe", "courier": courier},
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": f"探针客服品牌{ts}", "language": "cantonese", "tone": "礼貌专业"},
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
    src = rtc.LocalAudioTrack.create_audio_track("probe-src", audio_source)
    await room.local_participant.publish_track(
        src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    agent_audio = bytearray()
    started = asyncio.Event()

    def on_track(track, *_args):
        async def _read():
            started.set()
            stream = rtc.AudioStream(track)
            async for event in stream:
                agent_audio.extend(bytes(event.frame.data))

        asyncio.get_running_loop().create_task(_read())

    room.on("track_subscribed", on_track)
    for p in room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.track is not None:
                on_track(pub.track)
    await asyncio.wait_for(started.wait(), timeout=20)
    await asyncio.sleep(2.0)
    return call_id, room, audio_source, agent_audio


async def wait_settled(agent_audio: bytearray, mark: int, quiet_s: float = 4.0, timeout_s: float = 45.0) -> None:
    """等 agent 音频静默（回复播完）。"""
    start = time.monotonic()
    processed = mark
    silent = 0.0
    while time.monotonic() - start < timeout_s:
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


async def run_case(call_id: str, audio_source: rtc.AudioSource, agent_audio: bytearray,
                   case: dict, shape: str) -> dict:
    """shape: continuous | pause_before_brand | pause_after_brand"""
    text, brand, lang = case["text"], case["brand"], case["lang"]
    idx = text.find(brand)
    n_user_before = len([t for t in turns_of(call_id) if t["role"] == "user"])
    mark = len(agent_audio)
    if shape == "continuous":
        pcm = tts_pcm(text, lang)
    elif shape == "pause_before_brand":
        pcm = (
            tts_pcm(text[:idx], lang)
            + silence_pcm(0.5)
            + tts_pcm(text[idx:], lang)
        )
    elif shape == "pause_inside_brand":
        # 用户原诉形态：微停顿落在品牌词内部（拼|多多）——「拼」被吞的分裂点
        pcm = (
            tts_pcm(text[: idx + 1], lang)
            + silence_pcm(0.5)
            + tts_pcm(text[idx + 1 :], lang)
        )
    else:  # pause_after_brand
        tail = idx + len(brand)
        pcm = (
            tts_pcm(text[:tail], lang)
            + silence_pcm(0.5)
            + tts_pcm(text[tail:], lang)
        )
    await push_pcm(audio_source, pcm)
    await wait_settled(agent_audio, mark)
    # 轮次再宽限一拍（FINAL 兜底路径）
    await asyncio.sleep(2.5)
    users = [t["transcript"] for t in turns_of(call_id) if t["role"] == "user"][n_user_before:]
    # ASR 输出常为简体；判活按繁->简归一后比对
    _SIMP = str.maketrans({"東": "东", "寶": "宝", "買": "买"})
    brand_s = brand.translate(_SIMP)
    users_s = [u.translate(_SIMP) for u in users]
    joined = "".join(users_s).replace("，", "").replace("。", "")
    intact_single = any(brand_s in u for u in users_s)
    # 拆轮也算丢：品牌词只以拼接形式出现=两轮各拿一半
    intact_any = brand_s in joined
    return {
        "lang": lang, "text": text, "brand": brand, "shape": shape,
        "turns": users, "intact_single": intact_single, "intact_any": intact_any,
    }


async def main() -> int:
    results: list[dict] = []
    # 每个品牌独立通话:courier=品牌字面量进热词词表(行业词表无品牌词)
    for brand in dict.fromkeys(c["brand"] for c in CASES):
        call_id, room, audio_source, agent_audio = await make_call(courier=brand)
        for case in [c for c in CASES if c["brand"] == brand]:
            for shape in ("continuous", "pause_before_brand", "pause_inside_brand", "pause_after_brand"):
                r = await run_case(call_id, audio_source, agent_audio, case, shape)
                results.append(r)
                print(
                    f"[{'OK ' if r['intact_single'] else 'MISS'}] {r['lang']:9} {r['shape']:18} "
                    f"brand={r['brand']} turns={[(u[:22]) for u in r['turns']]}"
                )
        await room.disconnect()
        try:
            httpx.post(f"{CONTROL_PLANE_URL}/api/calls/{call_id}/hangup", timeout=10)
        except Exception:
            pass
    single = sum(1 for r in results if r["intact_single"])
    print(f"SUMMARY intact_single={single}/{len(results)} tag={TAG}")
    out = ROOT / "scripts" / f".probe_brand_words.{TAG}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"saved -> {out.name}")
    return 0 if single == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
