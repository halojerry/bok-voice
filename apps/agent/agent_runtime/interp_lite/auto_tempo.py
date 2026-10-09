"""interp_lite 播放背压 auto_tempo（W8-B，jinxi 水位设计移植）。

句级流水线积压时听感落后（FIFO 深度×句均时长的播放债）。本模块移植 jinxi
chase-controller 的稳定语义（代码自写，勿抄其标识符）：

- **播放水位三档**（积压 ms 缺省 1200/3000/5000，env ``BOK_INTERP_TEMPO_T1_MS/
  T2_MS/T3_MS``）：越过多一道水位=目标档+1；状态 NORMAL/GUARDED/CONSTRAINED。
- **变速追播**（缺省 1.0→1.25→1.35，env ``BOK_INTERP_TEMPO_SPEEDS`` 逗号分隔、
  升序化）——目标档=水位档钳到速度档上界：配 4 个速度时 T3（≥5000）自成第四
  档（落后极端档），缺省三速时 T3 只作深积压旗（``severe``，jinxi 裁静音档的
  观测位，本版不裁静音只亮旗）。
- **档位切换纪律**：升级一次一档（急追不等待，下一轮 resolve 续升）；降级需
  本档保持满 ``hold_s``（缺省 3s，env ``BOK_INTERP_TEMPO_HOLD_S``）且积压低于
  exitMs=本档进入阈值×0.7（退出滞回，防水位线附近抖档）。
- **积压估计**：FIFO 深度×近期句均时长（句均时长指数均值，初值 2.5s；TTS 侧
  逐流实测输入时长经 ``observe_audio_ms`` 回灌）。

装配面（A 线零经过）：``TempoController.make_frame_transform(sample_rate)`` 产出
「逐流变换工厂」注入 ``providers/tts_minimax.build(frame_transform=...)`` →
``MiniMaxTTS`` bidi 流在 emitter push 处应用（插件 ``_TempoPushProxy``）；
工厂缺省不传/返回 None=原速直通。总闸 ``BOK_INTERP_AUTO_TEMPO``（缺省开，
"0"=装配期全 None，旧路径逐字节）。变速实现单源
``bok_voice_core.audio_tempo.speedup_pcm``（OLA/SOLA 保音高，自 A 线探针
``probe_fast_speech.speedup_pcm`` 上收——本模块不持有第二份算法）。
"""

from __future__ import annotations

import os
import time
from typing import Callable, Iterable

import numpy as np

KILL_SWITCH_ENV = "BOK_INTERP_AUTO_TEMPO"
_T1_ENV = "BOK_INTERP_TEMPO_T1_MS"
_T2_ENV = "BOK_INTERP_TEMPO_T2_MS"
_T3_ENV = "BOK_INTERP_TEMPO_T3_MS"
_SPEEDS_ENV = "BOK_INTERP_TEMPO_SPEEDS"
_HOLD_ENV = "BOK_INTERP_TEMPO_HOLD_S"

DEFAULT_THRESHOLDS_MS = (1200.0, 3000.0, 5000.0)
DEFAULT_SPEEDS = (1.0, 1.25, 1.35)
DEFAULT_HOLD_S = 3.0
DEFAULT_SENT_MS = 2500.0  # 句均时长初值（EMA 首观测前的估计）

STATE_NAMES = ("NORMAL", "GUARDED", "CONSTRAINED")


def auto_tempo_enabled() -> bool:
    """总闸（缺省开；"0"=逐字节回旧行为——变速/账本全部不装配）。"""
    return os.environ.get(KILL_SWITCH_ENV, "1") == "1"


def parse_speeds(raw: str | None) -> tuple[float, ...]:
    """速度档解析：逗号分隔 float；非法项/小于 1.0 丢弃（不减速）；升序化。

    全无效/空 → 缺省三档（坏值回缺省纪律）。"""
    speeds: list[float] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = float(part)
        except ValueError:
            continue
        if v >= 1.0:
            speeds.append(v)
    if not speeds:
        return DEFAULT_SPEEDS
    return tuple(sorted(set(speeds)))


def _env_float(name: str, default: float, min_v: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        v = float(raw)
    except ValueError:
        return float(default)
    if min_v is not None and v < min_v:
        return float(default)
    return v


# ---- 批量变速（测试/离线工具口；流式路径走 ChunkStretcher） --------------------


def speedup_frames(frames: Iterable[bytes], speed: float, sample_rate: int) -> bytes:
    """批量变速：s16le mono PCM 帧迭代器 → 变速后整段 PCM。

    单源实现=``bok_voice_core.audio_tempo.speedup_pcm``（OLA/SOLA 保音高时长
    压缩，与 A 线探针同算法同份代码）；speed<=1.0 原样拼接返回（不减速）。"""
    from bok_voice_core.audio_tempo import speedup_pcm as _core

    buf = bytearray()
    for f in frames:
        if f:
            buf += bytes(f)
    if len(buf) % 2:
        del buf[-1]  # 防半样本（s16le）
    speed = float(speed)
    if not buf or speed <= 1.0:
        return bytes(buf)
    return _core(bytes(buf), speed, int(sample_rate))


class ChunkStretcher:
    """流式 OLA 变换：逐 push 吃 s16le mono 字节、吐拉长比 ``1/speed`` 的输出。

    状态机等价于 ``bok_voice_core.audio_tempo.speedup_pcm`` 的批内数学（60ms
    Hann 窗、hop_out=w//4、hop_in=hop_out×speed、±hop_out//2 归一化互相关对齐），
    但窗口/累加器跨 chunk 持续——逐 200ms 块独立变速会在每块边界产生硬接缝与
    窗尾丢失，这里以流式状态消掉。速度在构造时钉死（每 say 单元一个实例，
    档位=单元开播时刻的决策）。``finalize()`` 排干残余（零补齐）并吐出窗口
    余量、经 ``done_cb`` 回报输入时长 ms（句均时长 EMA 观测源）。
    """

    def __init__(self, sample_rate: int, speed: float, done_cb: Callable[[float], None] | None = None):
        self._sr = max(8000, int(sample_rate))
        self._speed = float(speed)
        self._done_cb = done_cb
        self._w = max(64, int(round(self._sr * 0.06)))  # 60ms 窗（16k=960 与探针一致）
        self._hop_out = self._w // 4
        self._hop_in = max(1, int(round(self._hop_out * self._speed)))
        self._search = self._hop_out // 2
        self._corr = self._hop_out
        self._win = np.hanning(self._w)
        self._x = np.zeros(0, dtype=np.float64)  # 未消费输入（全局坐标=offset+局部下标）
        self._offset = 0  # 已回收前缀样本数
        self._frames = 0  # 已处理帧数 k（out_start=k*hop_out 与 acc 基准恒对齐）
        self._acc = np.zeros(self._w, dtype=np.float64)
        self._wsum = np.zeros(self._w, dtype=np.float64)
        self._in_samples = 0
        self._finalized = False

    # ---- 输入 ----
    def push(self, chunk: bytes) -> bytes:
        """吃一块原始 PCM，返回（可能为空的）变速输出。"""
        if self._finalized:
            return b""
        if not chunk:
            return b""
        if len(chunk) % 2:
            chunk = chunk[:-1]  # 防半样本
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float64)
        if samples.size:
            self._x = np.concatenate([self._x, samples])
            self._in_samples += int(samples.size)
        out = bytearray()
        while self._can_process():
            out += self._process_frame()
        return bytes(out)

    def __call__(self, chunk: bytes) -> bytes:
        """插件注入契约口：变换对象可调用 ``(bytes)->bytes``（_TempoPushProxy 消费）。"""
        return self.push(chunk)

    def finalize(self) -> bytes:
        """流尽收尾：残余输入零补齐排干+窗口余量吐出；回报输入时长（一次）。

        帧数与批式 core 同公式（``ceil((n-w)/hop_in)+1``）——流式输出长度因此与
        ``speedup_frames`` 批式逐字节同长（差异只在 SOLA 对齐搜索的可用窗）。"""
        if self._finalized:
            return b""
        self._finalized = True
        out = bytearray()
        n = self._offset + int(self._x.size)
        if n > self._w:
            total_frames = int(np.ceil((n - self._w) / self._hop_in)) + 1
        else:
            total_frames = 1 if self._x.size or n else 0
        while self._frames < total_frames:
            out += self._process_frame()
        emit = self._acc / np.maximum(self._wsum, 1e-3)
        out += np.clip(np.rint(emit), -32768, 32767).astype(np.int16).tobytes()
        self._acc[:] = 0.0
        self._wsum[:] = 0.0
        if self._done_cb is not None:
            try:
                self._done_cb(self._in_samples / float(self._sr) * 1000.0)
            except Exception:  # noqa: BLE001 - 观测纯增益
                pass
        return bytes(out)

    # ---- 内部：与 core 批式 OLA 同数学的流式化 ----
    def _can_process(self) -> bool:
        k = self._frames
        if k == 0:
            return int(self._x.size) >= self._w
        avail = self._offset + int(self._x.size)
        nominal = k * self._hop_in
        return avail >= nominal + self._search + self._w  # 最坏读位 a+w 的安全门

    def _process_frame(self) -> bytes:
        k = self._frames
        nominal = k * self._hop_in
        avail = self._offset + int(self._x.size)
        if k == 0 or nominal + self._corr > avail:
            shift = 0
        else:
            shift = self._best_shift(nominal, avail)
        a = nominal + shift - self._offset
        if a < 0:
            a = 0
        seg = self._x[a:a + self._w]
        if seg.size < self._w:  # 尾帧零补齐
            seg = np.concatenate([seg, np.zeros(self._w - seg.size, dtype=np.float64)])
        self._acc += seg * self._win
        self._wsum += self._win
        emit = self._acc[:self._hop_out] / np.maximum(self._wsum[:self._hop_out], 1e-3)
        piece = np.clip(np.rint(emit), -32768, 32767).astype(np.int16).tobytes()
        # 窗口左移 hop_out：[0,hop_out) 已定格吐出，尾部清零等下一帧叠加
        self._acc[:-self._hop_out] = self._acc[self._hop_out:]
        self._acc[-self._hop_out:] = 0.0
        self._wsum[:-self._hop_out] = self._wsum[self._hop_out:]
        self._wsum[-self._hop_out:] = 0.0
        self._frames = k + 1
        drop = (k + 1) * self._hop_in - self._search - self._offset  # 前缀回收
        if drop > 0:
            self._x = self._x[drop:]
            self._offset += int(drop)
        return piece

    def _best_shift(self, nominal: int, avail: int) -> int:
        """±search 内找与新帧头最对齐的偏移（对已写输出的归一化互相关）。"""
        seg_out = self._acc[:self._corr]
        ob = seg_out - seg_out.mean()
        onorm = float(np.sqrt(np.dot(ob, ob))) + 1e-9
        best_s, best_v = 0, -2.0
        for s in range(-self._search, self._search + 1):
            a = nominal + s
            if a < self._offset or a + self._corr > avail:
                continue
            seg = self._x[a - self._offset:a - self._offset + self._corr]
            ab = seg - seg.mean()
            anorm = float(np.sqrt(np.dot(ab, ab))) + 1e-9
            v = float(np.dot(ab, ob)) / (anorm * onorm)
            if v > best_v:
                best_v, best_s = v, s
        return best_s


class TempoController:
    """播放水位三档状态机 + 句均时长 EMA + 逐流变速工厂（纯逻辑可离线单测）。

    ``resolve()`` 每单元 say 前调用一次；快照同时挂在 ``current``（TTS 侧工厂
    读取）。升级一次一档；降级需保持满 ``hold_s`` 且积压 < 本档进入阈值×0.7。"""

    def __init__(
        self,
        *,
        thresholds_ms: tuple[float, float, float] = DEFAULT_THRESHOLDS_MS,
        speeds: tuple[float, ...] = DEFAULT_SPEEDS,
        hold_s: float = DEFAULT_HOLD_S,
        exit_ratio: float = 0.7,
        ema_init_ms: float = DEFAULT_SENT_MS,
        ema_alpha: float = 0.3,
    ):
        self.thresholds_ms = tuple(float(t) for t in thresholds_ms)
        self.speeds = tuple(float(s) for s in speeds)
        self.hold_s = float(hold_s)
        self.exit_ratio = float(exit_ratio)
        self._ema = float(ema_init_ms)
        self._ema_alpha = float(ema_alpha)
        self._level = 0
        self._changed_at = 0.0
        self.current: dict = {
            "speed": self.speeds[0],
            "state": STATE_NAMES[0],
            "level": 0,
            "backlog_ms": 0.0,
            "queue_depth": 0,
            "severe": False,
        }

    # ---- 决策 ----
    def resolve(self, playable_backlog_ms: float, queue_depth: int, now_s: float) -> dict:
        backlog = max(0.0, float(playable_backlog_ms))
        ceiling = len(self.speeds) - 1
        target = 0
        for t in self.thresholds_ms:
            if backlog >= t:
                target += 1
        target = min(target, ceiling)
        level = self._level
        if target > level:
            level += 1  # 升级一次一档（急追；下一轮 resolve 续升）
            self._changed_at = float(now_s)
        elif target < level:
            entry = self.thresholds_ms[level - 1]  # 本档进入水位（exitMs=entry×0.7）
            held = float(now_s) - self._changed_at
            if held >= self.hold_s and backlog < entry * self.exit_ratio:
                level -= 1
                self._changed_at = float(now_s)
        self._level = level
        snap = {
            "speed": self.speeds[level],
            "state": STATE_NAMES[min(level, len(STATE_NAMES) - 1)],
            "level": level,
            "backlog_ms": backlog,
            "queue_depth": int(queue_depth),
            "severe": len(self.thresholds_ms) > 2 and backlog >= self.thresholds_ms[2],
        }
        self.current = snap
        return snap

    # ---- 积压估计 ----
    def observe_audio_ms(self, ms: float) -> None:
        """句均时长观测（TTS 侧逐流实测回灌；指数均值，钳 50ms-60s）。"""
        ms = min(max(float(ms), 50.0), 60000.0)
        self._ema = self._ema_alpha * ms + (1.0 - self._ema_alpha) * self._ema

    @property
    def sent_ms(self) -> dict:
        return {"ema": self._ema}

    def backlog_estimate_ms(self, queue_depth: int) -> float:
        """播放积压估计=FIFO 深度×近期句均时长。"""
        return max(0, int(queue_depth)) * self._ema

    # ---- TTS 装配注入 ----
    def make_frame_transform(self, sample_rate: int) -> Callable[[], "ChunkStretcher | None"]:
        """产出「逐流变换工厂」：``factory() -> ChunkStretcher | None``。

        None=本流原速直通（当前档 1.0 零开销零包装）；非 None=插件 emitter
        push 处逐块调用、流尽调 ``finalize()``。速度钉在工厂调用时刻（=该
        say 单元开播时的档位）。"""
        tc = self

        def factory():
            speed = float(tc.current.get("speed", 1.0))
            if speed <= 1.0:
                return None
            return ChunkStretcher(sample_rate, speed, done_cb=tc.observe_audio_ms)

        return factory


def build_tempo_controller() -> TempoController | None:
    """env 装配口：总闸关=None（旧路径逐字节）；坏值逐键回缺省。"""
    if not auto_tempo_enabled():
        return None
    thresholds = (
        _env_float(_T1_ENV, DEFAULT_THRESHOLDS_MS[0], min_v=1.0),
        _env_float(_T2_ENV, DEFAULT_THRESHOLDS_MS[1], min_v=1.0),
        _env_float(_T3_ENV, DEFAULT_THRESHOLDS_MS[2], min_v=1.0),
    )
    if list(thresholds) != sorted(thresholds):  # 乱配=整组回缺省（滞回语义防错乱）
        thresholds = DEFAULT_THRESHOLDS_MS
    speeds = parse_speeds(os.environ.get(_SPEEDS_ENV))
    hold_s = _env_float(_HOLD_ENV, DEFAULT_HOLD_S, min_v=0.0)
    return TempoController(thresholds_ms=thresholds, speeds=speeds, hold_s=hold_s)


def tempo_tick(p) -> None:
    """播放水位→变速档决策点（pipeline run 循环每单元出队后调用；tempo 未装配
    =no-op 逐字节——决策体拆域进本模块保 pipeline LOC 预算）。

    积压估计=FIFO 深度×近期句均时长（指数均值，TTS 侧逐流实测回灌，初值
    2.5s）；档位升级一次一档、降级需保持满 hold_s 且积压低于进入阈值×0.7
    （退出滞回）。变速的音频应用在 TTS 侧（frame_transform 注入），
    本点只做决策与档位观测。"""
    tempo = getattr(p, "_tempo", None)
    if tempo is None:
        return
    depth = p.q.qsize()
    prev_level = tempo.current.get("level", 0)
    snap = tempo.resolve(tempo.backlog_estimate_ms(depth), depth, time.monotonic())
    if snap["level"] != prev_level or snap["severe"]:
        print(
            f"[interp-lite] INTERP_TEMPO state={snap['state']} speed={snap['speed']} "
            f"backlog_ms={snap['backlog_ms']:.0f} depth={depth}",
            flush=True,
        )
