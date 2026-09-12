#!/usr/bin/env python3
"""mock SIP 客户(模拟联调档):CP 派生的真语音被叫。

剧本四型:
- answer     进房后按台词逐句 TTS 轮播(模拟真人客户问答);
- no_answer  永不进房,睡满响铃窗后退出(agent 侧超时=no_answer);
- reject     进房即离(join 后 ~0.5s 离房,落 agent 1.5s 离房监听窗内);
- hangup_mid 只说 1 句后离房(中途挂断)。

日志打结构化行 `MOCK_CALLEE event=<name> at=<s> identity=<id>` 供 E2E 断言;
房间 disconnected(被删/agent 收线)立即收尾退出,绝不留殭尸进程。

音频发布复用 scripts/e2e_real_customer.py 的 rtc 姿势:本地 TTS sidecar
(:8788)合成客户话音 → AudioSource 逐帧推流。语言三态 zh/cantonese/en。

用法(一般由 CP `POST /api/sip/mock/callee` 派生,也可手起调试):
  <python> scripts/mock_callee.py --url ws://127.0.0.1:7880 --token <jwt> \
      --identity sip-mock-64320111 --scenario answer \
      --script-json '["你好","我個件未到"]' --language cantonese
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8788")
CUSTOMER_VOICE = os.environ.get("BOK_MOCK_CUSTOMER_VOICE", "Vivian")
SAMPLE_RATE = 16000

# 剧本时序常量(与 agent dialer._dial_mock 的 1.5s 离房监听窗配合):
REJECT_LEAVE_OFFSET_S = 0.5   # join→leave:必须 <1.5s(太早撞监听注册竞态,太晚超窗)
FIRST_SPEAK_OFFSET_S = 0.8    # join→首句:>1.5s 窗后才出声,不被误判 reject
SPEAK_INTERVAL_S = 6.0        # 句间隔(≈一轮问答)
TAIL_SILENCE_S = 2.0          # 末句后收尾静默

VALID_SCENARIOS = ("answer", "no_answer", "reject", "hangup_mid")
VALID_LANGUAGES = ("zh", "cantonese", "en")


def normalize_language(raw: str) -> str:
    """语言三态归一:zh/cantonese/en,未知/旧拼写一律回落 cantonese。"""
    value = str(raw or "").strip().lower()
    if value in ("zh", "cantonese", "en"):
        return value
    return "cantonese"


def event_line(name: str, at: float, identity: str) -> str:
    """结构化事件行(E2E 断言素材)。"""
    return f"MOCK_CALLEE event={name} at={at:.1f} identity={identity}"


def plan_timeline(scenario: str, *, ring_delay_s: float, lines: int) -> list[tuple[str, float]]:
    """纯函数:剧本 → [(event, at_s)] 时间线(event ∈ join/speak/leave/exit)。

    时间锚=进程启动时刻,ring_delay_s 模拟拨号到接通的响铃延迟。
    """
    t = max(0.0, float(ring_delay_s))
    if scenario == "no_answer":
        # 永不进房:等响铃窗耗尽(哨兵时刻)后退出。
        return [("exit", 1e9)]
    if scenario == "reject":
        return [("join", t), ("leave", t + REJECT_LEAVE_OFFSET_S)]
    if scenario == "hangup_mid":
        return [
            ("join", t),
            ("speak", t + FIRST_SPEAK_OFFSET_S),
            ("leave", t + SPEAK_INTERVAL_S),
        ]
    tl: list[tuple[str, float]] = [("join", t)]
    at = t + FIRST_SPEAK_OFFSET_S
    for _ in range(max(1, int(lines))):
        tl.append(("speak", at))
        at += SPEAK_INTERVAL_S
    tl.append(("leave", at + TAIL_SILENCE_S))
    return tl


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="mock SIP 客户(真语音被叫)")
    parser.add_argument("--url", required=True, help="LiveKit ws url")
    parser.add_argument("--token", required=True, help="participant JWT(CP 签发)")
    parser.add_argument("--identity", required=True, help="participant identity")
    parser.add_argument("--scenario", default="answer", choices=VALID_SCENARIOS)
    parser.add_argument("--script-json", default="[]", help="台词 JSON 数组")
    parser.add_argument("--language", default="cantonese", help="zh|cantonese|en")
    parser.add_argument("--ring-delay", type=float, default=3.0)
    parser.add_argument("--ringing-window", type=float, default=35.0)
    parser.add_argument("--hangup-after-turns", type=int, default=0,
                        help=">0 时说满 N 句后离房(覆盖剧本 speak 轮数)")
    args = parser.parse_args(argv)

    def _script() -> list[str]:
        try:
            data: Any = json.loads(args.script_json)
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [str(x) for x in data if str(x).strip()]

    args.script = _script
    args.language = normalize_language(args.language)
    return args


def tts_pcm(text: str, lang: str) -> bytes:
    """本地 TTS sidecar 合成客户话音(照搬 e2e_real_customer.tts_pcm)。"""
    import httpx

    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{TTS_URL}/v1/audio/speech",
            json={"input": text, "language": lang, "voice": CUSTOMER_VOICE,
                  "sample_rate": SAMPLE_RATE},
        )
        r.raise_for_status()
        return r.content


async def push_pcm(audio_source: Any, pcm: bytes) -> None:
    """逐帧推音频(100ms/帧,略快于实时保证 VAD 连续)。"""
    from livekit import rtc

    chunk = int(SAMPLE_RATE * 0.1) * 2
    for i in range(0, len(pcm), chunk):
        seg = pcm[i : i + chunk]
        frame = rtc.AudioFrame(
            data=seg, sample_rate=SAMPLE_RATE, num_channels=1,
            samples_per_channel=len(seg) // 2,
        )
        await audio_source.capture_frame(frame)
        await asyncio.sleep(0.08)


class MockCallee:
    """一次 mock 通话的生命周期(进房→按剧本说话→离房)。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.room: Any = None
        self.audio_source: Any = None
        self._left = asyncio.Event()
        self._start = time.monotonic()

    def _emit(self, name: str, at: float) -> None:
        print(event_line(name, at, self.args.identity), flush=True)

    def _on_disconnected(self, *_a: Any) -> None:
        """房间断开(被删/agent 收线)→ 立即收尾,不留殭尸。"""
        if not self._left.is_set():
            at = time.monotonic() - self._start
            self._emit("room_disconnected", at)
            self._left.set()

    async def _sleep_until(self, at_s: float) -> bool:
        """睡到时间线时刻;期间房间断开立即返回 False。"""
        while True:
            remain = at_s - (time.monotonic() - self._start)
            if remain <= 0:
                return not self._left.is_set()
            try:
                await asyncio.wait_for(self._left.wait(), timeout=remain)
                return False  # 已被 disconnect 唤醒
            except (TimeoutError, asyncio.TimeoutError):
                return True

    async def _join(self) -> None:
        from livekit import rtc

        self.room = rtc.Room()
        self.room.on("disconnected", self._on_disconnected)
        await self.room.connect(self.args.url, self.args.token)
        self.audio_source = rtc.AudioSource(sample_rate=SAMPLE_RATE, num_channels=1)
        track = rtc.LocalAudioTrack.create_audio_track("mock-callee-src", self.audio_source)
        await self.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

    async def _speak(self, idx: int) -> None:
        script: list[str] = self.args.script()
        if not script:
            return
        text = script[idx % len(script)]
        try:
            pcm = await asyncio.to_thread(tts_pcm, text, self.args.language)
        except Exception as exc:  # noqa: BLE001 - TTS 故障不该拖垮整条剧本
            print(f"MOCK_CALLEE tts_error={exc!r}", flush=True)
            return
        await push_pcm(self.audio_source, pcm)

    async def _leave(self) -> None:
        try:
            if self.room is not None:
                await self.room.disconnect()
        except Exception:  # noqa: BLE001 - 断开失败不阻塞退出
            pass

    async def run(self) -> int:
        args = self.args
        lines = len(args.script())
        if args.hangup_after_turns > 0:
            lines = min(lines or args.hangup_after_turns, args.hangup_after_turns)
        timeline = plan_timeline(args.scenario, ring_delay_s=args.ring_delay, lines=lines)

        if args.scenario == "no_answer":
            # 永不进房:睡满响铃窗(上限哨兵时刻)后退出。
            self._emit("exit", args.ringing_window)
            await asyncio.sleep(max(0.0, args.ringing_window))
            return 0

        speak_idx = 0
        joined = False
        for event, at in timeline:
            if not await self._sleep_until(at):
                return 0  # 房间已断开(被删/agent 收线)
            self._emit(event, at)
            if event == "join":
                try:
                    await self._join()
                except Exception as exc:  # noqa: BLE001 - 连接失败=直接退出
                    print(f"MOCK_CALLEE join_error={exc!r}", flush=True)
                    return 1
                joined = True
            elif event == "speak":
                await self._speak(speak_idx)
                speak_idx += 1
            elif event == "leave":
                await self._leave()
                self._left.set()
                return 0
        if joined:
            await self._leave()
        return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    callee = MockCallee(args)

    async def _runner() -> int:
        try:
            return await callee.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 顶层兜底:异常也保证断开离房
            print(f"MOCK_CALLEE fatal={exc!r}", flush=True)
            await callee._leave()
            return 1

    try:
        return asyncio.run(_runner())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
