#!/usr/bin/env python3
"""W7-P4 终极可行性探针·driver 侧：真房间驱动官方 AgentSession 形状的同传 agent。

被测 agent=scripts/probes/w7p4_official_agent.py（agent_name=bok-w7p4，本脚本以
子进程方式拉起，官方 cli.run_app worker 形状；:8085 健康端点就绪后开始）。

场景（W7 probe P4，含追加的 S4 追嘴档臂）：
  S1 单轮 EVS×3：实时节奏推一条语料 →「音频推完→参与者收到 agent 首帧译文
     音频」；同时从 agent JSONL 事件对齐 definite final / LLM 请求发出时刻
     （preemptive 是否在 FINAL 前发请求）。
  S2 连续语音：两条语料背靠背（中间 600ms 静音）一次推完 → 观察轮切分
     （几段译文音频爆发/几条 final）与两轮译文间隔。
  S3 打断：agent 译文播报中开始推第二条语料 → agent 音频是否停（interruption
     生效）、新轮译文是否正常出。
  S4 追嘴档（Ethan 追问臂）：新房间 dispatch metadata 带 preemptive_tts=1，
     AgentSession 抢跑直通 TTS（说话中投机译文开播）——同 S1 语料重跑，量
     ①说话中出声（EVS 相对停嘴可为负=边讲边出声）②投机重启发生率
     （llm_req 数 vs llm/tts metrics cancelled 数）③投机播出被掐断的音频量
     （burst 字节截止点）④与默认档同语料 EVS 并排。

测量（全部 wall_ms=time.time()*1000，driver 与 agent 同机对齐）：
  - 订阅 agent 译文轨逐帧打时间戳+RMS+字节；「译文首帧」=能量门（RMS≥SPEECH_RMS）
    的首个语音帧（agent 不说话时 LiveKit 不发帧/发静音帧都不影响判定）；
  - 爆发（burst）分段=语音 run 之间 ≥BURST_GAP_MS 的低能量间隙；
  - agent 侧事件（STT final、LLM_REQ、metrics、agent 状态机）由 worker 写
    JSONL（W7P4_EVENTS），driver 按窗口切读。

token：不走 CP /api/token——CP 对 interpret 房间 role=me 会挂产品
bok-interp-fwd/rev 的 RoomAgentDispatch（产品解释器进房=测量噪声）、无记录房间
则 subscribe-only 不能推流；这里直接用 livekit.api.AccessToken 签发
（与 CP 同一原语），只挂本探针 agent_name 的显式分发。LiveKit 凭据
env 优先，缺省 devkey/devsecret（services/livekit-server/livekit.yaml keys 段，
bok serve 无 env 覆盖时的既定值）。

出站域（driver 侧只碰这两个）：ws://127.0.0.1:7880（livekit）+
http://127.0.0.1:8085（worker 健康端点）。云端腿（openspeech.bytedance.com /
api.deepseek.com / api.minimax.*）由 agent 进程的现役 provider 固定端点承担。
sink 全部包在专用守卫函数门后（守卫+sink 同函数）：_open_report_log（写盘）+
_health_get（环回探活）。

语料：主树 reports/asr-whisper-bench/corpus 的 zh_01/zh_02/zh_03.wav
（16k mono PCM16，P2 同源）。

用法：
  .venv312/bin/python scripts/probes/probe_official_session.py \
      [--report reports/w7probe/p4-session.md] [--skip s1|s2|s3|s4 ...]
前置：`bok serve`（livekit :7880）；不需要 CP 之外的任何本地模型。
"""
from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))

import argparse
import asyncio
import datetime
import json
import math
import os
import signal
import statistics
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import wave
from pathlib import Path

from livekit import rtc
from livekit.api import RoomAgentDispatch, RoomConfiguration
from livekit import api as lk_api

ROOT = Path(__file__).resolve().parents[2]
# 语料根（常量+resolve+前缀断言，Mimosa 路径穿越门禁形状；reports/ 已 gitignore，
# 主树才有语料，worktree 里只有本波报告）
CORPUS_ROOT = Path("/Users/halo/Documents/bok/voice-assistant/reports/asr-whisper-bench/corpus")
REPORT_DIR = ROOT / "reports" / "w7probe"

LIVEKIT_URL = os.environ.get("W7P4_LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_SECRET = os.environ.get("LIVEKIT_API_SECRET", "devsecret")
WORKER_PORT = int(os.environ.get("W7P4_PORT", "8085"))
# 健康探活 URL=模块级常量（host 字面量环回+常量端口拼接；不做调用点动态拼 URL）
WORKER_HEALTH_URL = "http://127.0.0.1:" + str(WORKER_PORT) + "/worker"
AGENT_NAME = os.environ.get("W7P4_AGENT_NAME", "bok-w7p4")
PY = sys.executable

# 出站白名单（Mimosa SSRF 门禁形状；本脚本 driver 侧只碰本地 livekit + worker 健康
# 端点，云端腿由 agent 进程的现役 provider 固定端点承担）
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _out_ok(url: str, schemes: tuple[str, ...]) -> bool:
    """出站校验：钉死 scheme 族 + 本地 host 精确匹配，拒端口外/凭据注入。"""
    parts = urllib.parse.urlsplit(str(url or ""))
    return (
        parts.scheme in schemes
        and (parts.hostname or "").lower() in _ALLOWED_HOSTS
        and not parts.username
        and not parts.password
    )


def _open_report_log(p: Path):
    """常量根围栏守卫（resolve+前缀断言；越界即拒）。"""
    rp = Path(p).resolve()
    if not str(rp).startswith(str(REPORT_DIR)):
        raise PermissionError(f"worker 路径越界 REPORT_DIR: {p}")
    return open(rp, "w", encoding="utf-8")


def _health_get(url: str):
    """环回健康探活守卫（scheme+host 白名单；越界即拒）。"""
    u = urllib.parse.urlsplit(str(url or ""))
    if u.scheme != "http" or (u.hostname or "").lower() not in _ALLOWED_HOSTS:
        raise PermissionError(f"非环回探活端点: {url}")
    return urllib.request.urlopen(url, timeout=1.5)


SPEECH_RMS = 400.0        # 译文语音能量门（MiniMax 干净人声；静音帧/房间底噪远低于此）
ONSET_FRAMES = 2          # 连续 N 帧过门=起声（防咔哒）
SPEECH_OFF_MS = 300       # 低能量持续=Burst 内句间停
BURST_GAP_MS = 500        # 爆发分段=run 间低能量 ≥500ms（轮间隙）
PUSH_CHUNK_MS = 100       # 实时节奏推流（e2e_interpret 同款 0.10s 背压纪律）
PUSH_SLEEP_S = 0.10

UTTERANCES = ["zh_01", "zh_02", "zh_03"]


def wall_ms() -> int:
    return int(time.time() * 1000)


def frame_rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    n = len(pcm) // 2
    frames = struct.unpack(f"<{n}h", pcm[: n * 2])
    return math.sqrt(sum(x * x for x in frames) / n)


def read_wav_pcm(name: str) -> tuple[bytes, int]:
    """常量根 + resolve + 前缀断言后才 open（路径穿越门禁形状）。"""
    p = (CORPUS_ROOT / f"{name}.wav").resolve()
    assert str(p).startswith(str(CORPUS_ROOT)), f"corpus path escapes root: {p}"
    with wave.open(str(p), "rb") as w:
        rate = w.getframerate()
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, f"expect mono pcm16: {p}"
        return w.readframes(w.getnframes()), rate


def load_refs() -> dict[str, str]:
    mpath = (CORPUS_ROOT / "manifest.json").resolve()
    assert str(mpath).startswith(str(CORPUS_ROOT)), f"corpus path escapes root: {mpath}"
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    refs: dict[str, str] = {}
    items = manifest if isinstance(manifest, list) else manifest.get("items") or manifest.get("files") or []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("file") or it.get("name") or "").strip()
        if name:
            refs[name.removesuffix(".wav")] = str(it.get("text") or it.get("ref") or "")
    return refs


class WorkerProc:
    """agent worker 子进程（官方 cli.run_app 形状；就绪=/worker HTTP 200）。"""

    def __init__(self, events_path: Path, log_path: Path):
        self.events_path = events_path
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.log_fh = None

    def start(self) -> None:
        env = dict(os.environ)
        env["W7P4_EVENTS"] = str(self.events_path)
        env.setdefault("LIVEKIT_URL", LIVEKIT_URL)
        env.setdefault("LIVEKIT_API_KEY", LIVEKIT_KEY)
        env.setdefault("LIVEKIT_API_SECRET", LIVEKIT_SECRET)
        self.log_fh = _open_report_log(self.log_path)
        self.proc = subprocess.Popen(
            [PY, str(Path(__file__).resolve().parent / "w7p4_official_agent.py"), "start"],
            cwd=str(ROOT),
            env=env,
            stdout=self.log_fh,
            stderr=subprocess.STDOUT,
        )

    async def wait_ready(self, timeout_s: float = 30.0) -> bool:
        url = WORKER_HEALTH_URL
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                print(f"[driver] worker exited rc={self.proc.returncode} — see {self.log_path}")
                return False
            try:
                with _health_get(url) as resp:
                    if resp.status == 200:
                        # HTTP 先起、livekit 注册后到（实测 ~1s 窗）——注册前建房
                        # 的显式分发会丢 job（首跑实证），必须再等 registered 行。
                        return await self._wait_registered(10.0)
            except (urllib.error.URLError, OSError):
                pass
            await asyncio.sleep(0.4)
        return False

    async def _wait_registered(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                text = self.log_path.read_text(encoding="utf-8", errors="replace")
                if '"registered worker"' in text or "registered worker" in text:
                    return True
                if self.proc is not None and self.proc.poll() is not None:
                    return False
            except OSError:
                pass
            await asyncio.sleep(0.2)
        return False

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        if self.log_fh:
            self.log_fh.close()


class AgentAudioWatcher:
    """订阅 agent 译文轨：逐帧 (wall_ms, rms, dur_ms, bytes)；能量门实时起声/收声。"""

    def __init__(self):
        self.frames: list[tuple[int, float, int, int]] = []
        self._speeching = False
        self._onset_streak = 0
        self._last_loud_ms = 0

    async def attach(self, room: rtc.Room, timeout_s: float = 40.0) -> bool:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def _on_track(track, pub, participant):
            if fut.done():
                return
            if int(track.kind) != int(rtc.TrackKind.KIND_AUDIO):
                return
            fut.set_result(track)

        room.on("track_subscribed", _on_track)
        for p in room.remote_participants.values():
            for pub in p.track_publications.values():
                t = getattr(pub, "track", None)
                if t is not None and int(t.kind) == int(rtc.TrackKind.KIND_AUDIO) and not fut.done():
                    fut.set_result(t)
        try:
            track = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            return False
        self.reader_task = asyncio.get_running_loop().create_task(self._read(track))
        return True

    async def _read(self, track) -> None:
        stream = rtc.AudioStream(track)
        try:
            async for ev in stream:
                frame = getattr(ev, "frame", ev)
                pcm = bytes(frame.data)
                rms = frame_rms(pcm)
                rate = int(frame.sample_rate) or 24000
                dur_ms = max(1, int(frame.samples_per_channel) * 1000 // rate)
                now = wall_ms()
                self.frames.append((now, rms, dur_ms, len(pcm)))
                if rms >= SPEECH_RMS:
                    self._onset_streak += 1
                    self._last_loud_ms = now
                    if not self._speeching and self._onset_streak >= ONSET_FRAMES:
                        self._speeching = True
                else:
                    self._onset_streak = 0
                    if self._speeching and now - self._last_loud_ms >= SPEECH_OFF_MS:
                        self._speeching = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读轨异常必须可见（吞掉=S3 类假阴性）
            print(f"[watcher] reader crashed: {exc!r}", flush=True)

    # —— 场景等待/分段原语 ——

    def first_speech_after(self, after_ms: int) -> int:
        """after_ms 之后第一个语音 run 起点 wall_ms（无则 0）。"""
        streak = 0
        run_start = 0
        for wall, rms, _d, _b in self.frames:
            if wall < after_ms:
                continue
            if rms >= SPEECH_RMS:
                streak += 1
                if streak >= ONSET_FRAMES:
                    return run_start or wall
                if streak == 1:
                    run_start = wall
            else:
                streak = 0
                run_start = 0
        return 0

    async def wait_speech_onset(self, after_ms: int, timeout_s: float) -> int:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            onset = self.first_speech_after(after_ms)
            if onset:
                return onset
            await asyncio.sleep(0.02)
        return 0

    def bursts(self, after_ms: int, gap_ms: int = BURST_GAP_MS) -> list[dict]:
        """after_ms 之后的语音爆发分段（run 间低能量 ≥gap_ms 切开）。"""
        loud = [(w, d, b) for (w, r, d, b) in self.frames if w >= after_ms and r >= SPEECH_RMS]
        if not loud:
            return []
        runs: list[list] = [[loud[0][0], loud[0][0] + loud[0][1], loud[0][2]]]
        for w, d, b in loud[1:]:
            if w - runs[-1][1] >= gap_ms:
                runs.append([w, w + d, b])
            else:
                runs[-1][1] = w + d
                runs[-1][2] += b
        out = []
        for s, e, nb in runs:
            seg = [r for (w, r, _d, _b) in self.frames if s <= w <= e]
            out.append({
                "start_ms": s,
                "end_ms": e,
                "audio_ms": e - s,
                "bytes": nb,
                "mean_rms": round(statistics.fmean(seg), 1) if seg else 0.0,
            })
        return out

    async def wait_bursts_settle(self, after_ms: int, min_bursts: int, quiet_ms: int, timeout_s: float) -> list[dict]:
        deadline = time.monotonic() + timeout_s
        last_count = -1
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            b = self.bursts(after_ms)
            now = time.monotonic()
            if len(b) != last_count:
                last_count = len(b)
                last_change = now
            if (
                len(b) >= min_bursts
                and (now - last_change) * 1000 >= quiet_ms
                and (not b or b[-1]["end_ms"] <= wall_ms() - quiet_ms)
            ):
                return b
            await asyncio.sleep(0.05)
        return self.bursts(after_ms)


async def wait_agent_idle(
    watcher: AgentAudioWatcher, evlog: EventsLog, quiet_ms: int = 2500, timeout_s: float = 30.0
) -> bool:
    """等 agent 完全空闲：近期无译文语音帧 + 状态机 listening + 无在途 final。

    轮与轮之间的真闸——上轮译文还在播时推下一轮会触发打断语义/吃句头
    （首跑实证 zh_02 头 1.5s 无转写），S1 计量必须整轮隔离。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        now = wall_ms()
        recent_speech = [
            f for f in watcher.frames if f[0] >= now - quiet_ms and f[1] >= SPEECH_RMS
        ]
        rows = evlog.refresh()
        states = [r for r in rows if r.get("ev") == "agent_state"]
        last_state = states[-1].get("state") if states else ""
        user_rows = [r for r in rows if r.get("ev") == "user_input"]
        last_user_ms = user_rows[-1].get("wall_ms", 0) if user_rows else 0
        if not recent_speech and last_state == "listening" and now - last_user_ms >= 1200:
            return True
        await asyncio.sleep(0.05)
    return False


class EventsLog:
    """agent JSONL 事件切窗读取。"""

    def __init__(self, path: Path):
        self.path = path
        self._pos = 0
        self.rows: list[dict] = []

    def refresh(self) -> list[dict]:
        if not self.path.exists():
            return self.rows
        with open(self.path, "r", encoding="utf-8") as fh:
            fh.seek(self._pos)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
            self._pos = fh.tell()
        return self.rows

    def window(self, since_ms: int) -> list[dict]:
        self.refresh()
        return [r for r in self.rows if int(r.get("wall_ms") or 0) >= since_ms]


async def push_pcm(source: rtc.AudioSource, pcm: bytes, rate: int = 16000) -> tuple[int, int]:
    """实时节奏推流（e2e_interpret 同款 0.10s/块背压纪律）。返回 (t_start, t_end)。"""
    t_start = wall_ms()
    chunk = int(rate * PUSH_CHUNK_MS / 1000) * 2
    for i in range(0, len(pcm), chunk):
        seg = pcm[i : i + chunk]
        frame = rtc.AudioFrame(
            data=seg, sample_rate=rate, num_channels=1, samples_per_channel=len(seg) // 2
        )
        await source.capture_frame(frame)
        await asyncio.sleep(PUSH_SLEEP_S)
    return t_start, wall_ms()


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[k]


async def connect_session(
    room_name: str, metadata: dict
) -> tuple[rtc.Room, rtc.AudioSource, AgentAudioWatcher, bool]:
    """建房+签 token（挂本探针 agent 的显式分发）+推流轨+订阅 agent 译文轨。"""
    at = (
        lk_api.AccessToken(LIVEKIT_KEY, LIVEKIT_SECRET)
        .with_identity(f"driver-{room_name}")
        .with_name("w7p4 driver")
        .with_grants(
            lk_api.VideoGrants(
                room_join=True, room=room_name, can_publish=True, can_subscribe=True, can_publish_data=True
            )
        )
        .with_ttl(datetime.timedelta(seconds=1800))
    )
    at = at.with_room_config(
        RoomConfiguration(agents=[RoomAgentDispatch(agent_name=AGENT_NAME, metadata=json.dumps(metadata))])
    )
    if not _out_ok(LIVEKIT_URL, ("ws", "wss")):
        raise SystemExit(f"livekit url not in local allowlist: {LIVEKIT_URL!r}")
    room = rtc.Room()
    await room.connect(LIVEKIT_URL, at.to_jwt())
    print(f"[driver] room={room_name} connected (metadata keys={sorted(metadata)})", flush=True)
    source = rtc.AudioSource(sample_rate=16000, num_channels=1)
    track = rtc.LocalAudioTrack.create_audio_track("w7p4-src", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    watcher = AgentAudioWatcher()
    ok = await watcher.attach(room)
    return room, source, watcher, ok


def turn_stats(ev_rows: list[dict]) -> dict:
    """单轮窗口的 agent 侧统计：final/llm_req/cancelled 计数与时刻。"""
    finals = [r for r in ev_rows if r.get("ev") == "user_input" and r.get("is_final")]
    llmreqs = [r for r in ev_rows if r.get("ev") == "llm_req"]
    llm_cancelled = [
        r for r in ev_rows if r.get("ev") == "metrics" and r.get("metric") == "llm_metrics" and r.get("cancelled")
    ]
    tts_cancelled = [
        r for r in ev_rows if r.get("ev") == "metrics" and r.get("metric") == "tts_metrics" and r.get("cancelled")
    ]
    asst_items = [r for r in ev_rows if r.get("ev") == "item_added" and r.get("role") == "assistant"]
    return {
        "finals": finals,
        "llmreqs": llmreqs,
        "llm_cancelled": llm_cancelled,
        "tts_cancelled": tts_cancelled,
        "asst_items": asst_items,
    }


async def run_s1_round(
    name: str,
    pcm: bytes,
    ref: str,
    source: rtc.AudioSource,
    watcher: AgentAudioWatcher,
    evlog: EventsLog,
    *,
    search_from_push_start: bool,
) -> dict:
    """单轮：推语料→等译文→对齐事件。search_from_push_start=True 时译文首帧
    搜索自推流起点（EVS 相对停嘴可为负=边讲边出声，S4 追嘴档用）。"""
    idle = await wait_agent_idle(watcher, evlog)
    if not idle:
        print(f"[round] {name} WARN: agent not fully idle before push", flush=True)
    cursor_before = wall_ms()
    t_start, t_end = await push_pcm(source, pcm)
    search_from = t_start if search_from_push_start else t_end
    onset = await watcher.wait_speech_onset(after_ms=search_from, timeout_s=25.0)
    evs_ms = (onset - t_end) if onset else -1  # 相对停嘴（负=停嘴前已出声）
    evs_from_push_start = (onset - t_start) if onset else -1
    # 说话人视角语音起点（推流里的起声）
    speech_start = wall_ms()
    for w, rms, _d, _b in watcher.frames:
        if rms >= SPEECH_RMS and w >= t_start:
            speech_start = w
            break
    bursts = await watcher.wait_bursts_settle(after_ms=t_end, min_bursts=1, quiet_ms=1500, timeout_s=35.0)
    ev_rows = evlog.window(since_ms=cursor_before)
    ts = turn_stats(ev_rows)
    finals, llmreqs = ts["finals"], ts["llmreqs"]
    llm_ttft = next(
        (
            r.get("ttft_ms")
            for r in ev_rows
            if r.get("ev") == "metrics" and r.get("metric") == "llm_metrics" and not r.get("cancelled")
        ),
        -1,
    )
    tts_ttfb = next(
        (
            r.get("ttfb_ms")
            for r in ev_rows
            if r.get("ev") == "metrics" and r.get("metric") == "tts_metrics" and not r.get("cancelled")
        ),
        -1,
    )
    states = [f"{r.get('state')}" for r in ev_rows if r.get("ev") == "agent_state"]
    # 投机采纳判据：final 提交后若仍有新 chat() 请求 = 投机产物没接上（重启/重生成）
    llm_after_final = [
        r for r in llmreqs if finals and r["wall_ms"] >= finals[-1]["wall_ms"]
    ]
    n_preflights = max(0, len(llmreqs) - len(llm_after_final))
    n_regen_after_final = len(llm_after_final) if finals else 0
    row = {
        "utterance": name,
        "ref": ref,
        "push_ms": t_end - t_start,
        "evs_ms": evs_ms,
        "evs_from_push_start_ms": evs_from_push_start,
        "evs_from_speech_onset_ms": (onset - speech_start) if onset else -1,
        "onset_before_push_end": bool(onset and onset < t_end),
        "final_lag_ms": (finals[-1]["wall_ms"] - t_end) if finals else -1,
        "final_text": (finals[-1].get("text") or "") if finals else "",
        "llm_req_lag_ms": (llmreqs[0]["wall_ms"] - t_end) if llmreqs else -1,
        "n_llm_req": len(llmreqs),
        "n_preflight_req": n_preflights,
        "n_regen_after_final": n_regen_after_final,
        "spec_adopted": bool(finals) and not llm_after_final,
        "llm_req_before_final": bool(llmreqs and finals and llmreqs[0]["wall_ms"] < finals[-1]["wall_ms"]),
        "preemptive_lead_ms": (
            (finals[-1]["wall_ms"] - llmreqs[0]["wall_ms"])
            if (llmreqs and finals and llmreqs[0]["wall_ms"] < finals[-1]["wall_ms"])
            else 0
        ),
        "n_llm_cancelled": len(ts["llm_cancelled"]),
        "n_tts_cancelled": len(ts["tts_cancelled"]),
        "n_assistant_items": len(ts["asst_items"]),
        "llm_ttft_ms": llm_ttft,
        "tts_ttfb_ms": tts_ttfb,
        "n_bursts": len(bursts),
        "trans_audio_ms": sum(b["audio_ms"] for b in bursts),
        "trans_bytes": sum(b["bytes"] for b in bursts),
        "bursts": bursts,
        "states": states,
    }
    print(
        f"[round] {name} evs_rel_end={evs_ms}ms evs_rel_start={evs_from_push_start}ms "
        f"final_lag={row['final_lag_ms']}ms llm_req#={row['n_llm_req']} preflight#={n_preflights} "
        f"adopted={row['spec_adopted']} preempted={row['llm_req_before_final']} "
        f"llm_cancelled={row['n_llm_cancelled']} tts_cancelled={row['n_tts_cancelled']} "
        f"ttft={llm_ttft} ttfb={tts_ttfb} bursts={row['n_bursts']} audio={row['trans_audio_ms']}ms",
        flush=True,
    )
    return row


async def run_driver(skip: set[str], report_path: Path) -> int:
    refs = load_refs()
    wavs: dict[str, bytes] = {}
    for name in UTTERANCES:
        pcm, rate = read_wav_pcm(name)
        assert rate == 16000, f"{name}: rate={rate}"
        wavs[name] = pcm
    print(
        f"[driver] corpus={CORPUS_ROOT} utterances={list(wavs)} "
        f"refs={[refs.get(u, '')[:14] for u in UTTERANCES]}",
        flush=True,
    )

    events_path = REPORT_DIR / "p4-agent-events.jsonl"
    events_path.unlink(missing_ok=True)
    log_path = REPORT_DIR / "p4-agent-worker.log"
    worker = WorkerProc(events_path, log_path)
    worker.start()
    if not await worker.wait_ready():
        print("[driver] worker not ready — abort")
        return 2
    print(f"[driver] worker ready on :{WORKER_PORT}", flush=True)

    evlog = EventsLog(events_path)
    results: dict = {"room_default": "", "room_s4": "", "s1": [], "s2": {}, "s3": {}, "s4": []}

    # ---------- 默认档房间（S1/S2/S3） ----------
    # 派发竞速守卫（跑 4 实测）：worker 注册后 ~0.5s 内建房会撞「failed to assign
    # job … no workers with sufficient capacity」瞬时不可用窗（产品侧 M-27 看门狗
    # 同题）——agent 轨 40s 没到就换新房间重连（worker 常驻，换房即可重派）。
    base = f"w7p4-{int(time.time()) % 1000000}"
    results["room_default"] = base
    room = None
    try:
        room, source, watcher, ok = None, None, None, False
        for attempt in range(1, 4):
            room, source, watcher, ok = await connect_session(
                base, {"listen_identity": f"driver-{base}", "source_lang": "zh", "target_lang": "cantonese"}
            )
            if ok:
                break
            print(f"[driver] attempt {attempt}: agent track never arrived — retrying with fresh room", flush=True)
            try:
                await room.disconnect()
            except Exception:  # noqa: BLE001
                pass
            base = f"w7p4-{int(time.time()) % 1000000}"
            results["room_default"] = base
            room = None
            await asyncio.sleep(2.0)
        if not ok:
            print("[driver] agent audio track never arrived — abort")
            return 3
        evlog.refresh()
        _n_started = len([r for r in evlog.rows if r.get("ev") == "session_started"])
        print(f"[driver] session_started rows={_n_started}", flush=True)
        # 等 TTS bidi 预热落地（跑 3 实测：预热没接上时首轮 ttfb 4.6s 冷握手污染 S1）
        await asyncio.sleep(3.0)

        # S1 单轮 EVS ×3（默认档：EVS 相对停嘴，恒为正口径）
        if "s1" not in skip:
            for name in UTTERANCES:
                row = await run_s1_round(
                    name, wavs[name], refs.get(name, ""), source, watcher, evlog,
                    search_from_push_start=False,
                )
                results["s1"].append(row)
                await wait_agent_idle(watcher, evlog)

        # S2 连续语音（两条背靠背，600ms 间隙）
        if "s2" not in skip:
            silence = b"\x00\x00" * int(16000 * 0.6)
            combo = wavs[UTTERANCES[0]] + silence + wavs[UTTERANCES[1]]
            cursor_before = wall_ms()
            t_start, t_end = await push_pcm(source, combo)
            # 轮切分判据=finals≥2（音频 burst 可能连播合并）；assistant item 在播完后落
            bursts = await watcher.wait_bursts_settle(after_ms=t_end, min_bursts=1, quiet_ms=2000, timeout_s=20.0)
            await asyncio.sleep(2.5)
            ts = turn_stats(evlog.window(since_ms=cursor_before))
            gaps = [bursts[i + 1]["start_ms"] - bursts[i]["end_ms"] for i in range(len(bursts) - 1)]
            results["s2"] = {
                "n_bursts": len(bursts),
                "bursts": bursts,
                "gaps_ms": gaps,
                "n_finals": len(ts["finals"]),
                "finals": [r.get("text", "") for r in ts["finals"]],
                "n_assistant_items": len(ts["asst_items"]),
                "assistant_texts": [r.get("text", "") for r in ts["asst_items"]],
                "rel_start_ms": [b["start_ms"] - t_end for b in bursts],
            }
            print(
                f"[S2] bursts={len(bursts)} finals={len(ts['finals'])} assistant_items={len(ts['asst_items'])} "
                f"rel_starts={results['s2']['rel_start_ms']} gaps={gaps}",
                flush=True,
            )
            await wait_agent_idle(watcher, evlog)

        # S3 播报中打断
        if "s3" not in skip:
            await wait_agent_idle(watcher, evlog)
            cursor_before = wall_ms()
            _, t_end1 = await push_pcm(source, wavs[UTTERANCES[1]])
            onset1 = await watcher.wait_speech_onset(after_ms=t_end1, timeout_s=20.0)
            if not onset1:
                results["s3"] = {"error": "first translation never started"}
                print("[S3] first translation never started", flush=True)
            else:
                # 让译文先播 ~600ms 再抢话：真·播报中打断（也稳过 min_duration=0.6）
                await asyncio.sleep(0.6)
                t_barge_start, t_barge_end = await push_pcm(source, wavs[UTTERANCES[2]])
                barge_lead_ms = t_barge_start - onset1
                deadline = time.monotonic() + 14.0
                while time.monotonic() < deadline:
                    b = watcher.bursts(after_ms=t_end1)
                    if len(b) >= 2 and b[-1]["end_ms"] <= wall_ms() - 1500:
                        break
                    await asyncio.sleep(0.05)
                bursts = watcher.bursts(after_ms=t_end1)
                ts = turn_stats(evlog.window(since_ms=cursor_before))
                states = [
                    (r.get("state"), r.get("wall_ms"))
                    for r in evlog.window(since_ms=cursor_before)
                    if r.get("ev") == "agent_state"
                ]
                post_barge_state = [s for s, w in states if w and w >= t_barge_start]
                results["s3"] = {
                    "barge_lead_ms": barge_lead_ms,
                    "bursts": bursts,
                    "burst1_audio_ms": bursts[0]["audio_ms"] if bursts else -1,
                    "burst1_bytes": bursts[0]["bytes"] if bursts else -1,
                    "new_translation_started": len(bursts) >= 2,
                    "new_burst_rel_ms": (bursts[1]["start_ms"] - t_barge_end) if len(bursts) >= 2 else -1,
                    "n_finals": len(ts["finals"]),
                    "final_texts": [r.get("text", "") for r in ts["finals"]],
                    "assistant_texts": [r.get("text", "") for r in ts["asst_items"]],
                    "cancelled_tts_count": len(ts["tts_cancelled"]),
                    "post_barge_states": post_barge_state[:8],
                }
                print(
                    f"[S3] barge_lead={barge_lead_ms}ms burst1_audio={results['s3']['burst1_audio_ms']}ms "
                    f"new_translation={results['s3']['new_translation_started']} "
                    f"new_burst_rel={results['s3']['new_burst_rel_ms']}ms cancelled_tts={len(ts['tts_cancelled'])}",
                    flush=True,
                )
        await room.disconnect()
        room = None
    finally:
        if room is not None:
            try:
                await room.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ---------- S4 追嘴档（preemptive_tts=True，新房间，同语料） ----------
    if "s4" not in skip:
        s4base = f"{base}-s4"
        results["room_s4"] = s4base
        room = None
        try:
            room, source4, watcher4, ok4 = await connect_session(
                s4base,
                {
                    "listen_identity": f"driver-{s4base}",
                    "source_lang": "zh",
                    "target_lang": "cantonese",
                    "preemptive_tts": "1",
                },
            )
            if not ok4:
                results["s4"] = [{"error": "agent track never arrived (s4 room)"}]
                print("[S4] agent track never arrived", flush=True)
            else:
                evlog.refresh()
                arm_rows = [r for r in evlog.rows if r.get("ev") == "arm_configured"]
                print(f"[S4] arm rows={arm_rows[-1] if arm_rows else 'none'}", flush=True)
                # 同上：等追嘴档房间的 bidi 预热落地再开轮
                await asyncio.sleep(3.0)
                for name in UTTERANCES:
                    row = await run_s1_round(
                        name, wavs[name], refs.get(name, ""), source4, watcher4, evlog,
                        search_from_push_start=True,
                    )
                    results["s4"].append(row)
                    await wait_agent_idle(watcher4, evlog)
        finally:
            if room is not None:
                try:
                    await room.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    worker.stop()

    # ---------- 判定 + 报告 ----------
    s1 = results["s1"]
    evs_list = [r["evs_ms"] for r in s1 if r["evs_ms"] >= 0]
    s1_pass = bool(evs_list) and statistics.fmean(evs_list) <= 2000.0
    _s2_txt = "".join(results["s2"].get("assistant_texts") or [])
    s2_pass = results["s2"].get("n_finals", 0) >= 2 and _s2_txt.count("。") + _s2_txt.count("？") >= 2
    s3_pass = bool(results["s3"]) and results["s3"].get("new_translation_started", False)
    s4 = results["s4"]
    s4_rows = [r for r in s4 if "error" not in r]
    s4_speech_time = [r for r in s4_rows if r.get("onset_before_push_end")]
    s4_pass = bool(s4_rows) and len(s4_speech_time) >= 1
    write_report(report_path, results, s1_pass, s2_pass, s3_pass, s4_pass)
    print(f"[driver] report -> {report_path}")
    print(
        f"[driver] GATES: S1({'PASS' if s1_pass else 'FAIL'}"
        f" mean={statistics.fmean(evs_list) if evs_list else -1:.0f}ms) "
        f"S2({'PASS' if s2_pass else 'FAIL'}) S3({'PASS' if s3_pass else 'FAIL'}) "
        f"S4({'PASS' if s4_pass else 'FAIL'} speech-time-onset {len(s4_speech_time)}/{len(s4_rows)})",
        flush=True,
    )
    return 0 if (s1_pass and s2_pass and s3_pass) else 1


def write_report(path: Path, results: dict, s1_pass: bool, s2_pass: bool, s3_pass: bool, s4_pass: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    s1 = results["s1"]
    lines: list[str] = []
    ap = lines.append
    ap("# W7 probe P4: 现役 provider × 官方 AgentSession 形状（真房间同传）")
    ap("")
    ap(f"- time: {datetime.datetime.now().isoformat(timespec='seconds')}")
    ap(
        f"- livekit: {LIVEKIT_URL} | worker: agent_name={AGENT_NAME} :{WORKER_PORT}"
        "（livekit-agents 1.8.2 官方 cli.run_app）"
    )
    ap(
        "- 形状: AgentSession(turn_detection=stt, preemptive_generation=on(max_retries=3),"
        " interruption=on(min_duration=0.6)) + Agent(instructions=_translation_instructions zh→cantonese 镜像)"
    )
    ap(
        "- provider: STT=DoubaoSTT(+end_window_size=500 注入, 凭据=设置库 asr 段) /"
        " LLM=官方 openai 插件→mt 车道(deepseek-flash, thinking off) /"
        " TTS=MiniMaxTTS(bidi, Cantonese_GentleLady, boost=Chinese,Yue)"
    )
    ap("- VAD: min_silence 0.45 / min_speech 0.15 / threshold 0.75（interpret 装配点基线）；endpointing 0.25/0.6")
    ap(
        "- token: 直签 AccessToken+RoomAgentDispatch（不走 CP /api/token——产品解释器进房噪声/"
        "recordless subscribe-only，见脚本头注）"
    )
    ap(f"- 语料: {CORPUS_ROOT}（zh_01/02/03, 16k mono PCM16, 实时节奏推流）")
    ap(
        "- 房间: 默认档(preemptive_tts=off)={room_default} / 追嘴档(preemptive_tts=on)={room_s4}".format(
            room_default=results.get("room_default", ""), room_s4=results.get("room_s4", "")
        )
    )
    ap(
        f"- gates: S1 mean EVS ≤2000ms {'PASS' if s1_pass else 'FAIL'}"
        f" | S2 轮切分(≥2 finals 且两句都译) {'PASS' if s2_pass else 'FAIL'}"
        f" | S3 打断后新译文 {'PASS' if s3_pass else 'FAIL'}"
        f" | S4 追嘴档说话中出声 ≥1/3 {'PASS' if s4_pass else 'FAIL'}"
    )
    ap("")
    ap("## S1 单轮 EVS（默认档 preemptive_tts=off；音频推完 → 译文首帧，含 SAUC 判停）")
    ap("")
    ap(
        "| # | utterance | ref | 推流 ms | EVS ms | final lag ms | final 文本 | LLM req lag ms"
        " | preempted(FINAL前) | 抢跑 lead ms | llm_req# | LLM ttft ms | TTS ttfb ms | bursts | 译文音频 ms |"
    )
    ap("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(s1, 1):
        ap(
            f"| {i} | {r['utterance']} | {r['ref'][:14]} | {r['push_ms']} | {r['evs_ms']} | "
            f"{r['final_lag_ms']} | {r['final_text'][:18]} | {r['llm_req_lag_ms']} | {r['llm_req_before_final']} | "
            f"{r['preemptive_lead_ms']} | {r['n_llm_req']} | {r['llm_ttft_ms']} | {r['tts_ttfb_ms']} | "
            f"{r['n_bursts']} | {r['trans_audio_ms']} |"
        )
    evs_vals = [r["evs_ms"] for r in s1 if r["evs_ms"] >= 0]
    if evs_vals:
        ap("")
        ap(
            f"- EVS mean={statistics.fmean(evs_vals):.0f}ms p50={pct(evs_vals, 0.5):.0f}ms"
            f" max={max(evs_vals)}ms（门槛 ≤2000ms）"
        )
    ap("")
    ap("### preemptive 实际行为·默认档（llm_req wall vs definite final wall）")
    ap("")
    ap("- 每轮 llm_req 数含抢跑；抢跑 lead=final−首个 llm_req（正值=FINAL 前已发请求）。")
    for r in s1:
        ap(
            f"- {r['utterance']}: llm_req#={r['n_llm_req']} preempted={r['llm_req_before_final']} "
            f"lead={r['preemptive_lead_ms']}ms"
            f" llm_cancelled={r['n_llm_cancelled']} tts_cancelled={r['n_tts_cancelled']}"
        )
    ap("")
    ap("## S2 连续语音轮切分（两条语料 600ms 间隙一次推完）")
    ap("")
    s2 = results["s2"]
    ap(f"- bursts={s2.get('n_bursts')} finals={s2.get('n_finals')} assistant_items={s2.get('n_assistant_items')}")
    ap("- 注：burst 分段门=音频间隙 ≥500ms；两轮译文背靠背连播（间隙 <500ms）时合为一段 burst——")
    ap("  轮切分以 finals/生成次数为准（框架对每条 final 各起一次生成，转录项合并为一个 assistant item）。")
    ap(f"- 译文爆发起点（相对推完）: {s2.get('rel_start_ms')} ms；爆发间隙: {s2.get('gaps_ms')} ms")
    ap(f"- finals: {s2.get('finals')}")
    ap(f"- assistant 译文: {s2.get('assistant_texts')}")
    for i, b in enumerate(s2.get("bursts") or [], 1):
        _rel = b["start_ms"] - (s2["bursts"][0]["start_ms"] if s2.get("bursts") else 0)
        ap(f"  - burst{i}: rel_start={_rel}ms audio={b['audio_ms']}ms bytes={b['bytes']}")
    ap("")
    ap("## S3 播报中打断（译文起声后立即推第二条语料）")
    ap("")
    s3 = results["s3"]
    if s3.get("error"):
        ap(f"- ERROR: {s3['error']}")
    else:
        ap(f"- barge lead（译文开播→抢话开始）: {s3.get('barge_lead_ms')} ms")
        ap(
            f"- burst1 音频时长/字节: {s3.get('burst1_audio_ms')} ms / {s3.get('burst1_bytes')} B"
            "（被打断=比无打断译文短；参照 S1 zh_02 译文时长）"
        )
        ap(f"- 新译文爆发: {s3.get('new_translation_started')}（rel={s3.get('new_burst_rel_ms')} ms after 抢话推完）")
        ap(f"- finals: {s3.get('final_texts')}")
        ap(f"- assistant 译文: {s3.get('assistant_texts')}")
        ap(f"- cancelled TTS 请求: {s3.get('cancelled_tts_count')}（框架打断在途合成取消打点）")
        ap(f"- 抢话后 agent 状态序列: {s3.get('post_barge_states')}")
    ap("")
    ap("## S4 追嘴档（preemptive_tts=True，同语料；EVS 相对停嘴可为负=边讲边出声）")
    ap("")
    ap(
        "| # | utterance | EVS rel停嘴 ms | 说话中出声 | EVS rel推流起 ms | final lag ms"
        " | preflight# | final后新请求(未采纳) | 采纳 | assistant items | bursts | 状态 |"
    )
    ap("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(results["s4"], 1):
        if "error" in r:
            ap(f"| {i} | — | — | — | — | — | — | — | — | — | — | — | {r['error']} |")
            continue
        ap(
            f"| {i} | {r['utterance']} | {r['evs_ms']} | {r['onset_before_push_end']}"
            f" | {r['evs_from_push_start_ms']} | {r['final_lag_ms']}"
            f" | {r.get('n_preflight_req', '-')} | {r.get('n_regen_after_final', '-')}"
            f" | {r.get('spec_adopted', '-')} | {r['n_assistant_items']} | {r['n_bursts']} | {r['states']} |"
        )
    if results["s4"]:
        s4_rows = [r for r in results["s4"] if "error" not in r]
        s4_speech_time = [r for r in s4_rows if r.get("onset_before_push_end")]
        evs4 = [r["evs_ms"] for r in s4_rows if r["evs_ms"] is not None]
        n_pre = sum(r.get("n_preflight_req", 0) for r in s4_rows)
        n_adopt = sum(1 for r in s4_rows if r.get("spec_adopted"))
        n_regen = sum(r.get("n_regen_after_final", 0) for r in s4_rows)
        ap("")
        ap(
            f"- 追嘴档 EVS rel停嘴 mean={statistics.fmean(evs4) if evs4 else 0:.0f}ms；说话中出声 "
            f"{len(s4_speech_time)}/{len(s4_rows)} 轮；投机请求（FINAL 前抢跑）={n_pre}，"
            f"final 提交后新请求（投机未接上=重生成）={n_regen}，投机被采纳轮数={n_adopt}/{len(s4_rows)}"
        )
        ap("- 两档并排（同语料，EVS 相对停嘴）:")
        ap("")
        ap("| utterance | 默认档 EVS ms | 追嘴档 EVS ms（负=边讲边出声） | Δms | 抢跑# | 采纳 |")
        ap("|---|---|---|---|---|---|")
        s1_by = {r["utterance"]: r for r in s1}
        for r in s4_rows:
            d = s1_by.get(r["utterance"], {}).get("evs_ms", -1)
            ap(
                f"| {r['utterance']} | {d} | {r['evs_ms']}"
                f" | {(r['evs_ms'] - d) if (d >= 0 and r['evs_ms'] is not None) else '—'}"
                f" | {r.get('n_preflight_req', 0)} | {r.get('spec_adopted', '-')} |"
            )
    ap("")
    ap("## agent 事件流水（全窗口）")
    ap("")
    try:
        _ev_text = (REPORT_DIR / "p4-agent-events.jsonl").read_text(encoding="utf-8")
        rows = [json.loads(line) for line in _ev_text.splitlines() if line.strip()]
        ap("```")
        for r in rows:
            ap(json.dumps(r, ensure_ascii=False))
        ap("```")
    except Exception as exc:  # noqa: BLE001
        ap(f"(events 读取失败: {exc!r})")
    ap("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="W7-P4 official AgentSession probe driver")
    parser.add_argument("--report", default=str(REPORT_DIR / "p4-session.md"))
    parser.add_argument("--skip", default="", help="comma list: s1,s2,s3,s4")
    args = parser.parse_args()
    skip = {s.strip().lower() for s in args.skip.split(",") if s.strip()}
    rc = asyncio.run(run_driver(skip, Path(args.report)))
    sys.exit(rc)


if __name__ == "__main__":
    main()
