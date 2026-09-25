"""W2b 思考态键盘环境音探针(2026-09-24)。

官方 BackgroundAudioPlayer thinking_sound 实弹验收。**bg 轨归因的死结**:垫话
人声与键盘 burst 同走 `background_audio` 轨,filler 开着时 bg 能量无法归因——
所以腿设计成对关垫话:

  main 腿(默认,filler 开):只验「通道无破坏」——bg 轨有能量 + 回复轮正常
    (垫话照发即证 BackgroundAudioPlayer 构造带 thinking_sound 后整链路健在;
    键盘归因不在此腿)。
  归因腿(--expect-keyboard,worker 以 BOK_FILLER=0 起好):bg 轨只剩键盘
    burst——断言 ≥0.05s 非静音(5 轮×0.60 抽签,全 miss ~1%)。
  kill 腿(--expect-off,worker 以 BOK_FILLER=0 BOK_AMBIENT_KEYBOARD=0 起好):
    断言 bg 轨零非静音能量(轨照发,thinking_sound=None 状态机空转)。

按轨归因:agent 语音轨与 bg 轨是两条独立 published track,track.name 区分
(`background_audio` = 官方组件常量)。worker 换 env 姿势=停 monitor +
bok._start_proc(_worker_specs) 带 env(AGENTS.md 共享栈纪律),跑完还原。

用法:.venv312/bin/python scripts/probe_ambient_keyboard.py [--expect-keyboard|--expect-off]
栈在跑、无其它通话(GPU 竞态规矩);auth-on 栈须带 BOK_CP_TOKEN env。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_brand_words as pb  # noqa: E402  复用 CP_HEADERS/tts_pcm/push_pcm/silence_pcm/frame_rms

CONTROL_PLANE_URL = pb.CONTROL_PLANE_URL
BG_TRACK_NAME = "background_audio"  # 官方组件 _TRACK_NAME,升级时同步
# 语音判定阈值略低于 pb.wait_settled 的 200:键盘 burst(0.6 音量)比人声低。
RMS_THRESHOLD = 120.0
USER_TEXT = "我想查一下订单"  # 7 字单口气句(E2E 句形铁律:<10 字免疫 vad-pause 劈轮)
ROUNDS = 5


def non_silent_seconds(pcm: bytes) -> tuple[float, float]:
    """(非静音秒数, 最大 RMS)——20ms 窗逐窗判定。"""
    step = 320
    nonsilent = 0.0
    max_rms = 0.0
    for i in range(0, len(pcm) - step + 1, step):
        rms = pb.frame_rms(pcm[i : i + step])
        max_rms = max(max_rms, rms)
        if rms >= RMS_THRESHOLD:
            nonsilent += 0.02
    return nonsilent, max_rms


async def wait_speech_then_quiet(buf: bytearray, *, need_speech_s: float = 0.5,
                                 quiet_s: float = 3.5, timeout_s: float = 40.0) -> bool:
    """语音轨:等本轮回复出声(≥need_speech_s)再等静默(quiet_s)。"""
    start = time.monotonic()
    speech = 0.0
    silent = 0.0
    processed = 0
    while time.monotonic() - start < timeout_s:
        step = 320
        while processed + step <= len(buf):
            if pb.frame_rms(bytes(buf[processed : processed + step])) >= 200:
                speech += 0.02
                silent = 0.0
            else:
                silent += 0.02
            processed += step
        if speech >= need_speech_s and silent >= quiet_s:
            return True
        await asyncio.sleep(0.1)
    return speech >= need_speech_s  # 静默超时但出过声=半过(返回给上层判)


async def run_leg(mode: str) -> tuple[bool, str]:
    ts = int(time.time() * 1000) % 100000
    obj = httpx.post(
        f"{CONTROL_PLANE_URL}/api/objects?account_id=acc-001",
        json={"display_name": f"probe-键盘音{ts}", "role_template": "buyer",
              "language": "zh", "background": "probe", "courier": "顺丰物流"},
        timeout=10, headers=pb.CP_HEADERS,
    ).json()
    persona = httpx.post(
        f"{CONTROL_PLANE_URL}/api/personas",
        json={"name": f"probe-客服键盘音{ts}", "language": "zh", "tone": "礼貌专业"},
        timeout=10, headers=pb.CP_HEADERS,
    ).json()
    call = httpx.post(
        f"{CONTROL_PLANE_URL}/api/calls",
        json={"account_id": "acc-001", "object_id": obj["id"], "persona_id": persona["id"],
              "mode": "live", "direction": "webrtc", "language": "zh"},
        timeout=10, headers=pb.CP_HEADERS,
    ).json()
    call_id = call["id"]
    data = httpx.post(f"{CONTROL_PLANE_URL}/api/token",
                      json={"account_id": "acc-001", "call_id": call_id}, timeout=10,
                      headers=pb.CP_HEADERS).json()
    from livekit import rtc

    room = rtc.Room()
    audio_source = rtc.AudioSource(sample_rate=16000, num_channels=1)
    speech_buf: dict[str, bytearray] = {"v": bytearray()}  # agent 语音轨(每轮清)
    bg_buf = bytearray()  # bg 轨全程累计(断言面)
    got_bg_track = asyncio.Event()
    got_speech_track = asyncio.Event()

    def on_track(track, *_a):
        async def _read():
            name = getattr(track, "name", "") or ""
            (got_bg_track if name == BG_TRACK_NAME else got_speech_track).set()
            stream = rtc.AudioStream(track)
            async for ev in stream:
                data = bytes(ev.frame.data)
                if name == BG_TRACK_NAME:
                    bg_buf.extend(data)
                else:
                    speech_buf["v"].extend(data)

        asyncio.get_running_loop().create_task(_read())

    room.on("track_subscribed", on_track)
    await room.connect(data["serverUrl"], data["participantToken"])
    src = rtc.LocalAudioTrack.create_audio_track("probe-src", audio_source)
    await room.local_participant.publish_track(
        src, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    try:
        await asyncio.wait_for(asyncio.gather(got_speech_track.wait(), got_bg_track.wait()),
                               timeout=20)
    except asyncio.TimeoutError:
        await room.disconnect()
        print(f"[FAIL] ambient-keyboard {mode}: track 缺席 speech={got_speech_track.is_set()} "
              f"bg={got_bg_track.is_set()}(worker 未接单?)", flush=True)
        return False, "tracks missing"

    await asyncio.sleep(2.5)  # 开场白起播(语音轨)
    pcm = pb.tts_pcm(USER_TEXT, lang="zh")
    ok_rounds = 0
    for i in range(ROUNDS):
        speech_buf["v"].clear()
        await pb.push_pcm(audio_source, pcm)
        await pb.push_pcm(audio_source, pb.silence_pcm(1.5))
        heard = await wait_speech_then_quiet(speech_buf["v"])
        if heard:
            ok_rounds += 1
        await asyncio.sleep(1.0)
    await room.disconnect()

    bg_s, bg_max = non_silent_seconds(bytes(bg_buf))
    if mode == "expect_off":
        ok = bg_s == 0.0
        why = f"kill 腿 bg 轨非静音 {bg_s:.2f}s(max rms {bg_max:.0f}) 应为零"
    elif mode == "expect_keyboard":
        ok = bg_s >= 0.05 and ok_rounds >= 3
        why = (f"归因腿 bg 非静音 {bg_s:.2f}s(max rms {bg_max:.0f}) 应 ≥0.05s(纯键盘;"
               f"若近零=键盘未响,查 worker env BOK_FILLER 是否真为 0)")
    else:  # main
        ok = bg_s >= 0.05 and ok_rounds >= 3
        why = (f"main 腿 bg 非静音 {bg_s:.2f}s(max rms {bg_max:.0f}) ≥0.05s 且 "
               f"回复轮 {ok_rounds}/{ROUNDS} ≥3")
    print(f"[{'PASS' if ok else 'FAIL'}] ambient-keyboard {mode}: "
          f"bg_non_silent={bg_s:.2f}s bg_max_rms={bg_max:.0f} reply_rounds={ok_rounds}/{ROUNDS}")
    if not ok:
        print(f"  {why}")
    return ok, call_id


async def _main(mode: str) -> int:
    ok, _ = await run_leg(mode)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-keyboard", action="store_true",
                    help="归因腿:worker 须先以 BOK_FILLER=0(键盘开)起好")
    ap.add_argument("--expect-off", action="store_true",
                    help="kill 腿:worker 须先以 BOK_FILLER=0 BOK_AMBIENT_KEYBOARD=0 起好")
    args = ap.parse_args()
    mode = ("expect_off" if args.expect_off
            else "expect_keyboard" if args.expect_keyboard else "main")
    return asyncio.run(_main(mode))


if __name__ == "__main__":
    raise SystemExit(main())
