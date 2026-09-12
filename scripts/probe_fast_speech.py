"""快语速吃字/回声守卫探针（2026-09-12 Task 2 验收）。

三个修复的实机验收（栈在跑、无其它通话时——GPU 竞态规矩）：
  P1 首字存活:START pre-roll 喂会话——「好的，淘宝，京东」1.4× 速推流,
     用户轮转写应含首字「好」(修复前 1.4× 速首 1-3 字结构性缺失)。
  P2 纯热词答案存活:停嘴层剥尾保头——对象 courier=拼多多(词表含之),
     客户答「拼多多」不再被词表回声守卫整条丢弃。
  P3 平台词不被误伤:同句「淘宝/京东」(淘宝不在词表,守卫零触碰)。

用法:.venv312/bin/python scripts/probe_fast_speech.py
结果落 scripts/.probe_fast_speech.json;退出码非 0=有 case 未过。
"""

from __future__ import annotations

import asyncio
import audioop
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_brand_words as pb  # noqa: E402  复用 make_call/tts_pcm/turns_of/wait_settled

CONTROL_PLANE_URL = pb.CONTROL_PLANE_URL


def speedup_pcm(pcm: bytes, factor: float) -> bytes:
    """时长压缩(factor>1=更快):audioop 重采样到低采样率再按 16k 读——音调上移,
    词时长缩短,模拟快语速(本地 TTS sidecar 无 speed 参数)。"""
    out_rate = int(16000 / factor)
    conv, _ = audioop.ratecv(pcm, 2, 1, 16000, out_rate, None)
    return conv


async def run_case(case: dict) -> dict:
    ts = int(time.time() * 1000) % 100000
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"探针-快语速-{ts}", "role_template": "buyer",
              "language": case.get("lang", "zh"), "background": "probe",
              "courier": case.get("courier", "")},
        timeout=10,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": f"探针客服快语速{ts}", "language": case.get("lang", "zh"), "tone": "礼貌专业"},
        timeout=10,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
              "mode": "live", "direction": "webrtc", "language": case.get("lang", "zh")},
        timeout=10,
    ).json()
    call_id = call["id"]
    data = httpx.post(f"{CONTROL_PLANE_URL}/api/token",
                      json={"account_id": "acc-001", "call_id": call_id}, timeout=10).json()
    from livekit import rtc

    room = rtc.Room()
    await room.connect(data["serverUrl"], data["participantToken"])
    audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
    src = rtc.LocalAudioTrack.create_audio_track("probe-src", audio_source)
    await room.local_participant.publish_track(
        src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    agent_audio = bytearray()
    started = asyncio.Event()

    def on_track(track, *_a):
        async def _read():
            started.set()
            stream = rtc.AudioStream(track)
            async for ev in stream:
                agent_audio.extend(bytes(ev.frame.data))

        asyncio.get_running_loop().create_task(_read())

    room.on("track_subscribed", on_track)
    await asyncio.wait_for(started.wait(), timeout=20)
    await asyncio.sleep(2.5)  # 开场白起播

    pcm = pb.tts_pcm(case["text"], lang=case.get("tts_lang", case.get("lang", "zh")))
    if case.get("speed", 1.0) != 1.0:
        pcm = speedup_pcm(pcm, case["speed"])
    await pb.push_pcm(audio_source, pcm)
    # 尾静音必须推:VAD END_OF_SPEECH 依赖后续静音帧(不推=永不停嘴=无 FINAL)
    await pb.push_pcm(audio_source, pb.silence_pcm(1.5))
    mark = len(agent_audio)
    await pb.wait_settled(agent_audio, mark, quiet_s=4.0, timeout_s=40)
    turns = pb.turns_of(call_id)
    user_texts = [t.get("transcript", "") for t in turns if t.get("role") == "user"]
    joined = " ".join(user_texts)
    hits = {k: (k in joined) for k in case["expect"]}
    ok = all(hits.values())
    await room.disconnect()
    return {"name": case["name"], "ok": ok, "hits": hits, "user_texts": user_texts,
            "speed": case.get("speed", 1.0), "text": case["text"]}


CASES = [
    # P1+P3:1.4× 快语速整答——首字「好」存活 + 平台词「淘宝」在场(不在词表,零误伤)
    {"name": "fast-head-survival", "text": "好的，淘宝，京东。", "lang": "zh",
     "speed": 1.4, "courier": "顺丰物流", "expect": ["好", "淘宝"]},
    # P2:纯热词答案(courier=拼多多→词表含拼多多)不再被整条丢
    {"name": "pdd-answer-survival", "text": "拼多多。", "lang": "zh",
     "speed": 1.0, "courier": "拼多多", "expect": ["拼多多"]},
]


async def _main() -> int:
    results, failed = [], 0
    for case in CASES:
        r = await run_case(case)
        failed += 0 if r["ok"] else 1
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['name']}: hits={r['hits']} "
              f"user_texts={r['user_texts']!r}")
        results.append(r)
        await asyncio.sleep(2.0)
    Path(__file__).parent.joinpath(".probe_fast_speech.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2))
    print("ALL PASS" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
