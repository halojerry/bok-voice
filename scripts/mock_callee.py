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

`--narrowband`(8kHz 窄带档):每条台词先过 `downsample_to_narrowband`(16k→8k
→16k,电话频带损失)再推流,模拟运营商 PCMU/PCMA 8kHz 线路——spec 2026-09-13
§6 前置门的测试床(真中继上线前重验 16k ASR 栈在窄带话音下的表现)。

用法(一般由 CP `POST /api/sip/mock/callee` 派生,也可手起调试):
  <python> scripts/mock_callee.py --url ws://127.0.0.1:7880 --token <jwt> \
      --identity sip-mock-64320111 --scenario answer \
      --script-json '["你好","我個件未到"]' --language cantonese
      # 加 --narrowband 即 8kHz 窄带档
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

# ── 8kHz 窄带档（spec 2026-09-13 §6 前置门测试床）──────────────────────────
# 真中继上运营商音频是 PCMU/PCMA 8kHz（电话频带 ~300-3400Hz），我们全套栈跑
# 16kHz。窄带档把 TTS 合成的话音按「16k→8k→16k」走一遍，模拟那条链路上的
# 带宽损失，用来在真中继上线前重验 ASR 的句级提交/热词/数字保护/拆句组装。
NB_TARGET_RATE = 8000          # 电话侧采样率（G.711 PCMU/PCMA）
NB_CUTOFF_HZ = 3400.0          # 电话频带上沿（-3dB 点），过此即被削
NB_FILTER_ORDER = 6            # 抗混叠阶数（6 阶=3 个 biquad：3.4k 处 -3dB、4k 处 ~-11dB）

# 剧本时序常量(与 agent dialer._dial_mock 的 1.5s 离房监听窗配合):
REJECT_LEAVE_OFFSET_S = 0.5   # join→leave:必须 <1.5s(太早撞监听注册竞态,太晚超窗)
FIRST_SPEAK_OFFSET_S = 0.8    # join→首句:>1.5s 窗后才出声,不被误判 reject
SPEAK_INTERVAL_S = 6.0        # 句间隔(≈一轮问答)
TAIL_SILENCE_S = 2.0          # 末句后收尾静默
# 轮次对齐(2026-09-12):台词要等 AI 讲完再讲。固定时刻表会在 AI 还在念开场白时
# 插话,客户首句被 VAD 当插话丢掉 → 报号句变成首轮、落在收号步之外(外呼 E2E
# 实测:同一台词有时 captured 有时走漏,根因就是首句丢没丢)。
AGENT_VOICE_RMS = 150         # 对端帧能量门限(16000Hz int16 帧;低于=静音)
AGENT_QUIET_S = 1.0           # 对端静默满这么久才让下一句出声
AGENT_QUIET_TIMEOUT_S = 45.0  # 等不到静默的兜底上限(防死等;超时照讲)


def _rms(pcm: bytes) -> float:
    """16-bit PCM 帧的 RMS(能量门限判定用)。"""
    if len(pcm) < 2:
        return 0.0
    import struct

    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm[: n * 2])
    return (sum(x * x for x in frames) / n) ** 0.5


# ---- 窄带降采样（纯函数，spec 2026-09-13 §6 前置门）----

def _butterworth_qs(order: int) -> list[float]:
    """N 阶 Butterworth 的 biquad Q 值表（1/(2·sin((2k+1)π/2N))）。"""
    import math

    qs = []
    for k in range(order // 2):
        angle = (2 * k + 1) * math.pi / (2 * order)
        qs.append(1.0 / (2.0 * math.sin(angle)))
    return qs


def _biquad_lowpass(cutoff_hz: float, q: float, sample_rate: int) -> tuple[float, ...]:
    """RBJ cookbook 低通 biquad 系数（已按 a0 归一，返回 b0,b1,b2,a1,a2）。"""
    import math

    w0 = 2.0 * math.pi * cutoff_hz / sample_rate
    cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
    alpha = sin_w0 / (2.0 * q)
    a0 = 1.0 + alpha
    return (
        (1.0 - cos_w0) / 2.0 / a0,
        (1.0 - cos_w0) / a0,
        (1.0 - cos_w0) / 2.0 / a0,
        -2.0 * cos_w0 / a0,
        (1.0 - alpha) / a0,
    )


def _lowpass(samples: list[float], coeffs: list[tuple[float, ...]]) -> list[float]:
    """级联 biquad 低通（Direct Form I）；coeffs = 各段 (b0,b1,b2,a1,a2)。

    纯 Python 逐样本推进：TTS 台词一条 1-2s（16k 采样）→ 每段百万级乘加，
    实测毫秒量级，不值得为它引 numpy 依赖。
    """
    out = samples
    for b0, b1, b2, a1, a2 in coeffs:
        x1 = x2 = y1 = y2 = 0.0
        cur: list[float] = []
        append = cur.append
        for x0 in out:
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1 = x1, x0
            y2, y1 = y1, y0
            append(y0)
        out = cur
    return out


def downsample_to_narrowband(pcm16: bytes, src_rate: int = SAMPLE_RATE) -> bytes:
    """模拟电话窄带：16k→8k→16k，保留 ~0-3.4kHz 电话频带特征。

    链路：3.4kHz 6 阶 Butterworth 抗混叠低通 → 抽 2 降采样到 8k（G.711 那条
    线的带宽上限）→ 零阶保持升回 16k（rtc AudioSource 恒 16k，不改）→ 再低通
    一次抹掉零阶保持的 4-8kHz 镜像（否则会凭空多出真实电话线没有的高频镜像，
    让重验偏悲观）。

    为什么不用计划里的「相邻两样本均值」：2 抽头均值在 4kHz 只有 -3dB（能量
    剩 50%），既过不了 §6 的 4kHz 能量断言，也不是电话线的真实频响（运营商
    在 3.4kHz 处 -3dB、4kHz 已进阻带）。故按电话频带设计抗混叠滤波器。

    真电话线还有 8bit 对数量化噪声（G.711）——本函数不模拟：重验关心的是
    带宽损失对 ASR 的影响，量化噪声是另一个变量（真中继样本回填时再看）。
    """
    import array

    body = pcm16[: len(pcm16) // 2 * 2]
    if not body:
        return b""
    src = array.array("h", body)
    coeffs = [_biquad_lowpass(NB_CUTOFF_HZ, q, src_rate)
              for q in _butterworth_qs(NB_FILTER_ORDER)]
    anti_aliased = _lowpass([float(x) for x in src], coeffs)
    # 抽 2 到 8k → 零阶保持回 16k。
    held: list[float] = []
    for v in anti_aliased[::2]:
        held.append(v)
        held.append(v)
    smoothed = _lowpass(held, coeffs)
    out = array.array("h", (max(-32768, min(32767, int(round(v)))) for v in smoothed))
    return out.tobytes()

# 离房**锚定实际 connect 完成时刻**的剧本(而非计划 join 时刻)——rtc connect
# 耗时若 ≥REJECT_LEAVE_OFFSET_S,按计划时刻离房会抢在 agent 侧 participant 监听
# 注册之前到达(join 事件与 agent 认领之间仍有 connect 间隙),reject 被误判
# answered。锚 connect 完成时刻后,join→leave 间隙恒为 0.5s 落 1.5s 窗内。
# 键=scenario,值=connect 完成后停留秒数(leave 事件的计划时刻仅作纯函数序列占位)。
DWELL_AFTER_CONNECT_S: dict[str, float] = {"reject": REJECT_LEAVE_OFFSET_S}

VALID_SCENARIOS = ("answer", "no_answer", "reject", "hangup_mid")
VALID_LANGUAGES = ("zh", "cantonese", "en")

# 台词兜底：answer 剧本没带台词时用语言相关默认 2 句。没有这层兜底，campaign
# dial 块漏传 script 的 mock 客户会进房后静坐无声（E2E 里表现为「接通但零转写」，
# 不是链路故障却像链路故障）。句子均为通用客户应承语（不含具体业务事实），
# 单句 <10 字单口气口吻——vad-pause 劈轮保护（≥10 字 + 长停顿会被切轮）。
DEFAULT_SCRIPTS: dict[str, list[str]] = {
    "zh": ["你好", "好的我知道啦"],
    "cantonese": ["你好呀", "好嘅我知啦"],
    "en": ["hello", "yes okay"],
}


def default_script(language: str) -> list[str]:
    """语言相关默认台词（answer 剧本兜底）；未知语言回落 cantonese。"""
    return list(DEFAULT_SCRIPTS.get(normalize_language(language), DEFAULT_SCRIPTS["cantonese"]))


def normalize_language(raw: str) -> str:
    """语言三态归一:zh/cantonese/en,未知/旧拼写一律回落 cantonese。"""
    value = str(raw or "").strip().lower()
    if value in ("zh", "cantonese", "en"):
        return value
    return "cantonese"


def event_line(name: str, at: float, identity: str) -> str:
    """结构化事件行(E2E 断言素材)。"""
    return f"MOCK_CALLEE event={name} at={at:.1f} identity={identity}"


def plan_timeline(scenario: str, *, ring_delay_s: float, lines: int,
                  speak_interval_s: float = SPEAK_INTERVAL_S) -> list[tuple[str, float]]:
    """纯函数:剧本 → [(event, at_s)] 时间线(event ∈ join/speak/leave/exit)。

    时间锚=进程启动时刻,ring_delay_s 模拟拨号到接通的响铃延迟。
    reject 的 leave 时刻仅是「事件序列占位」——run() 对 DWELL_AFTER_CONNECT_S
    命中的剧本改为锚**实际 connect 完成时刻**+dwell(见该常量注释),因为真正的
    接通耗时不可能在纯函数里预知。
    speak_interval_s:句间隔(默认 6s≈一轮问答);E2E 要把客户报号句对齐到 AI 的
    收号步时,AI 每轮处理+播报可能要 8-12s,间隔太短会令号码句在 AI 还在念
    开场白/上一步时就到了(号码句落在收号步之外 → 捕获门失效),由调用方按
    `--speak-interval` 调大。
    """
    t = max(0.0, float(ring_delay_s))
    interval = max(0.1, float(speak_interval_s))
    if scenario == "no_answer":
        # 永不进房:等响铃窗耗尽(哨兵时刻)后退出。
        return [("exit", 1e9)]
    if scenario == "reject":
        return [("join", t), ("leave", t + REJECT_LEAVE_OFFSET_S)]
    if scenario == "hangup_mid":
        return [
            ("join", t),
            ("speak", t + FIRST_SPEAK_OFFSET_S),
            ("leave", t + interval),
        ]
    tl: list[tuple[str, float]] = [("join", t)]
    at = t + FIRST_SPEAK_OFFSET_S
    for _ in range(max(1, int(lines))):
        tl.append(("speak", at))
        at += interval
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
    parser.add_argument("--speak-interval", type=float, default=SPEAK_INTERVAL_S,
                        help="句间隔秒(默认 6≈一轮问答;E2E 对齐 AI 步进可调大)")
    parser.add_argument("--hangup-after-turns", type=int, default=0,
                        help=">0 时说满 N 句后离房(覆盖剧本 speak 轮数)")
    parser.add_argument("--narrowband", action="store_true",
                        help="8kHz 窄带档:台词先过 16k→8k→16k(电话频带损失)再推流")
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
        # 对端(AI)语音活动账本:最后一次听到对端有声的时刻(monotonic)。
        # 台词要等 AI 讲完再讲——固定时刻表会在 AI 还在念开场白时插话,那一段
        # 客户话音要么被 VAD 当插话丢掉、要么把流程推进时机打乱(实测客户首句
        # 被吞 → 报号句变成首轮,落在收号步之外)。
        self._agent_last_voice = 0.0
        self._agent_tasks: list[Any] = []

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
        # 订阅对端音轨,维护「AI 是否正在讲话」——台词节奏要跟 AI 的轮次走。
        self.room.on("track_subscribed", self._on_remote_track)
        for participant in self.room.remote_participants.values():
            for pub in participant.track_publications.values():
                tr = getattr(pub, "track", None)
                if tr is not None:
                    self._on_remote_track(tr, pub, participant)

    def _on_remote_track(self, track: Any, _pub: Any = None, _p: Any = None) -> None:
        """对端音轨订阅回调:按帧能量更新「AI 最后出声时刻」。"""
        from livekit import rtc

        if int(getattr(track, "kind", 0)) != int(rtc.TrackKind.KIND_AUDIO):
            return

        async def _read() -> None:
            try:
                stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
                async for ev in stream:
                    frame = getattr(ev, "frame", ev)
                    pcm = bytes(getattr(frame, "data", b"") or b"")
                    if _rms(pcm) >= AGENT_VOICE_RMS:
                        self._agent_last_voice = time.monotonic()
            except Exception:  # noqa: BLE001 - 对端音轨读取失败不影响剧本
                return

        try:
            self._agent_tasks.append(asyncio.get_running_loop().create_task(_read()))
        except RuntimeError:  # 无运行中事件循环(理论不可达)
            return

    async def _wait_agent_quiet(self, quiet_s: float, timeout_s: float,
                                require_voice: bool = False) -> None:
        """等到对端(AI)静默 quiet_s 秒再让台词出声(超时兜底防死等)。

        require_voice=True: 必须**先听到对端出过声**再等静默——首句用。首句旧版
        按「未听到 AI 就直接出声」放行,而 mock 客户 join+0.8s 出声时 agent 侧
        session 往往还没起来(实测 session_started +5s)或正在念开场白,整句落在
        ASR 监听之前 → 零转写(8kHz 探针实测:16 腿里首句丢失在宽/窄两档同样发生,
        宽档基线一起被污染)。真客户是「听到对面开口才回话」,等对端先出声更贴近
        真实;AI 全程不出声时由 timeout 兜底放行,不会死等。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not self._left.is_set():
            if self._agent_last_voice <= 0.0:
                if not require_voice:
                    return
            elif time.monotonic() - self._agent_last_voice >= quiet_s:
                return
            await asyncio.sleep(0.1)

    async def _speak(self, idx: int) -> None:
        script: list[str] = self.args.script()
        if not script:
            return
        # 轮次对齐:等 AI 讲完(静默 ≥AGENT_QUIET_S)再出声——首句也不例外(见
        # `_wait_agent_quiet` 的 require_voice 注释:不等就会在 agent 会话/开场白
        # 起来前抢话,整句被吞)。时间线的 FIRST_SPEAK_OFFSET_S 只作最早时刻。
        await self._wait_agent_quiet(AGENT_QUIET_S, AGENT_QUIET_TIMEOUT_S,
                                     require_voice=True)
        text = script[idx % len(script)]
        try:
            pcm = await asyncio.to_thread(tts_pcm, text, self.args.language)
        except Exception as exc:  # noqa: BLE001 - TTS 故障不该拖垮整条剧本
            print(f"MOCK_CALLEE tts_error={exc!r}", flush=True)
            return
        if getattr(self.args, "narrowband", False):
            # 8kHz 窄带档：送进房间前过一遍电话频带（滤波器是 CPU 活，扔线程池，
            # 别卡事件循环——推流那一路还要按时喂帧）。
            pcm = await asyncio.to_thread(downsample_to_narrowband, pcm)
        await push_pcm(self.audio_source, pcm)

    async def _leave(self) -> None:
        # 先收掉对端音轨读取任务(房间断开后它们会自然结束,这里主动 cancel 免挂)。
        for t in self._agent_tasks:
            try:
                t.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._agent_tasks.clear()
        try:
            if self.room is not None:
                await self.room.disconnect()
        except Exception:  # noqa: BLE001 - 断开失败不阻塞退出
            pass

    async def run(self) -> int:
        args = self.args
        # 台词兜底：answer 剧本空台词 → 语言相关默认 2 句（否则进房静坐无声）。
        if args.scenario == "answer" and not args.script():
            args.script = lambda: default_script(args.language)
            print(f"MOCK_CALLEE default_script language={args.language}", flush=True)
        if getattr(args, "narrowband", False):
            print(f"MOCK_CALLEE narrowband=1 cutoff_hz={NB_CUTOFF_HZ:g} "
                  f"target_rate={NB_TARGET_RATE}", flush=True)
        lines = len(args.script())
        if args.hangup_after_turns > 0:
            lines = min(lines or args.hangup_after_turns, args.hangup_after_turns)
        timeline = plan_timeline(args.scenario, ring_delay_s=args.ring_delay, lines=lines,
                                 speak_interval_s=args.speak_interval)

        if args.scenario == "no_answer":
            # 永不进房:睡满响铃窗(上限哨兵时刻)后退出。
            self._emit("exit", args.ringing_window)
            await asyncio.sleep(max(0.0, args.ringing_window))
            return 0

        speak_idx = 0
        joined = False
        for event, at in timeline:
            if event == "leave" and args.scenario in DWELL_AFTER_CONNECT_S:
                # reject 档:leave 锚实际 connect 完成时刻(见 DWELL_AFTER_CONNECT_S),
                # 不按计划 join 时刻——connect 耗时 ≥0.5s 时计划锚会抢在 agent 监听
                # 注册前离房,reject 被误判 answered。
                return await self._leave_after_dwell(DWELL_AFTER_CONNECT_S[args.scenario])
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

    async def _leave_after_dwell(self, dwell_s: float) -> int:
        """connect 完成后停留 dwell_s 再离房(时序窗锚实际接通时刻,恒定)。"""
        await asyncio.sleep(max(0.0, dwell_s))
        self._emit("leave", time.monotonic() - self._start)
        await self._leave()
        self._left.set()
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
