"""FireRedVAD 试点适配层（2026-09-28）：把 FireRedVAD 流式 VAD 包成 livekit ``vad.VAD``。

**是什么**：FireRedTeam/FireRedVAD 的 0.6M DFSMN 流式 VAD（Apache-2.0）官方 ONNX
（``fireredvad_stream_vad_with_cache.onnx`` + ``cmvn.ark``），包成与 silero 同形的
livekit ``vad.VAD``/``vad.VADStream``。一个 10ms hop 出一次概率，状态机做 onset/offset
滞回，事件三值 START_OF_SPEECH / INFERENCE_DONE / END_OF_SPEECH 与 silero 逐语义对齐。

**为什么**：现役 silero 在嘈杂/短促噪声下误触发、粤语场景端点偏差；FireRedVAD
多语种（含粤语）F1 明显更高。本层默认**零变化**（``BOK_VAD_PROVIDER`` 缺省 ``silero``），
只在运营显式置 ``firered`` 时替换，且装配失败一律回退 silero（不许让整栈起不来）。

**坑（改代码前必读）**：
- **前置特征必须逐字节对齐官方**：kaldi fbank（16k / 25ms / 10ms / 80 mel / dither=0 /
  snip_edges=True）+ cmvn.ark（kaldi 二进制矩阵，mean=row0/count、var=row1/count-mean²、
  istd=1/sqrt(var)、floor 1e-20）。流式用**持久 OnlineFbank 逐 160 样本喂**——实测与
  官方整段 ``accept_waveform`` 输出逐帧 0 误差（tests 里钉住）。
- **帧↔音频配对偏移**：kaldi snip_edges 帧 t 的窗=``[t*160, t*160+400)``，要喂到
  hop t+2 才 ready（固有 25ms 窗延迟）。因此每个 10ms hop 消费时把**该 hop 的 PCM**
  挂到「此刻刚 ready 的那帧」的概率上（≈20ms 配对偏移，对 150/350ms 滞回无影响），
  但**音频铺排逐 hop 不重叠**——这是消费侧（``livekit_plugins._run`` 把
  INFERENCE_DONE.frames 累进 ``_pending``）连续性的前提，勿改成挂整窗（2.5× 重叠）。
- **pre-roll 合同**：START.frames=0.5s prefix + 已确认 speech 窗（__SpeechBuffer``
  游标语义，等价 silero ``_copy_speech_buffer``）；消费侧 ASR 起报前导就靠它。
- **ONNX I/O 运行时内省**：feat 输入/cache 输入/probs 输出/cache 输出靠名字+秩判定，
  换模型文件不改代码；cache 初值 zeros，流内独立张量。
- **多流独立**：每 ``FireRedVADStream`` 独立 cache+独立单线程 executor（构造自
  ``knf.OnlineFbank`` 亦流内独有）；``InferenceSession`` 进程级共享，aclose 关 executor。
- **登记**：``BOK_VAD_PROVIDER``/``BOK_FIRERED_MODEL_DIR``/``BOK_FIRERED_THRESHOLD``/
  ``BOK_FIRERED_SMOOTH`` 已在 ``tools/bok.py _FORWARD_ENV``（prod 封闭 env 面可达）。
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import time
import weakref
from collections import deque
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path

import numpy as np

from livekit import rtc
from livekit.agents import utils, vad
from livekit.agents.log import logger

# ---------------------------------------------------------------------------
# 常量（与官方 fireredvad/core/constants.py、audio_feat.py 对齐）
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
FRAME_LENGTH_MS = 25
FRAME_SHIFT_MS = 10
FRAME_SHIFT_S = FRAME_SHIFT_MS / 1000.0  # 0.01
FRAME_LENGTH_SAMPLE = int(SAMPLE_RATE * FRAME_LENGTH_MS / 1000)  # 400
FRAME_SHIFT_SAMPLE = int(SAMPLE_RATE * FRAME_SHIFT_MS / 1000)  # 160
MEL_BINS = 80

_MODEL_FILENAME = "fireredvad_stream_vad_with_cache.onnx"
_CMVN_FILENAME = "cmvn.ark"
# 模型资产随源码分发（与 assets/smart-turn/ 同惯例）：agent_runtime/assets/firered-vad/
_DEFAULT_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "firered-vad"

# 默认参数（对齐本仓 A 线基线，非官方默认）：
#   min_speech 150ms、min_silence 350ms（silero 现役档）、prefix 0.5s；
#   activation_threshold=0.5（FireRedVAD 独立档，**不照抄 silero 0.75**）。
_DEFAULT_MIN_SPEECH_S = 0.15
_DEFAULT_MIN_SILENCE_S = 0.35
_DEFAULT_PREFIX_PADDING_S = 0.5
_DEFAULT_MAX_BUFFERED_S = 15.0
_DEFAULT_THRESHOLD = 0.5
_DEFAULT_SMOOTH = 5


# ---------------------------------------------------------------------------
# 前置特征：kaldi cmvn.ark 手写解析 + fbank
# ---------------------------------------------------------------------------
def read_kaldi_cmvn(path: str | os.PathLike[str]) -> tuple[np.ndarray, np.ndarray]:
    """解析 kaldi 二进制 cmvn.ark → (means, inverse_std_variances)，各 (dim,)。

    不引 kaldiio：格式=``\\0B``(binary) + token(``DM ``/``FM ``) + 1 字节长度前缀
    int32 rows + 1 字节长度前缀 int32 cols + rows*cols 个 float64/float32。stats
    形状 [2, dim+1]，末列为 count。mean=row0/count、var=row1/count-mean²、istd=1/√var，
    var floor 1e-20（与官方 CMVN.read_kaldi_cmvn 逐式一致）。
    """
    data = Path(path).read_bytes()
    if len(data) < 15 or data[:2] != b"\x00B":
        raise ValueError(f"not a kaldi binary matrix: {path}")
    pos = 2
    token = data[pos : pos + 3].decode("ascii", "replace").strip()
    pos += 3
    if token not in ("DM", "FM"):
        raise ValueError(f"unsupported kaldi token {token!r} in {path}")
    if data[pos] != 4:  # int32 基本类型长度前缀
        raise ValueError(f"unexpected kaldi int size {data[pos]} in {path}")
    pos += 1
    rows = struct.unpack_from("<i", data, pos)[0]
    pos += 4
    if data[pos] != 4:
        raise ValueError(f"unexpected kaldi int size {data[pos]} in {path}")
    pos += 1
    cols = struct.unpack_from("<i", data, pos)[0]
    pos += 4
    dtype = np.float64 if token == "DM" else np.float32
    n = rows * cols
    stats = np.frombuffer(data, dtype=dtype, count=n, offset=pos).reshape(rows, cols)
    if stats.shape[0] != 2:
        raise ValueError(f"cmvn stats must be 2 rows, got {stats.shape}")
    dim = stats.shape[-1] - 1
    count = float(stats[0, dim])
    if count < 1:
        raise ValueError("cmvn count must be >= 1")
    floor = 1e-20
    means = np.empty(dim, dtype=np.float32)
    istd = np.empty(dim, dtype=np.float32)
    for d in range(dim):
        mean = float(stats[0, d]) / count
        var = float(stats[1, d]) / count - mean * mean
        if var < floor:
            var = floor
        means[d] = mean
        istd[d] = 1.0 / math.sqrt(var)
    return means, istd


def _make_fbank():
    """官方同款 OnlineFbank（16k/25ms/10ms/80mel/dither=0/snip_edges=True）。

    每流独立实例（有状态）；逐 FRAME_SHIFT_SAMPLE 喂入时每 hop 恰好出 1 帧
    （前 2 hop 为 25ms 窗预热，见模块 docstring 配对偏移说明）。
    """
    import kaldi_native_fbank as knf

    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = SAMPLE_RATE
    opts.frame_opts.frame_length_ms = FRAME_LENGTH_MS
    opts.frame_opts.frame_shift_ms = FRAME_SHIFT_MS
    opts.frame_opts.dither = 0
    opts.frame_opts.snip_edges = True
    opts.mel_opts.num_bins = MEL_BINS
    opts.mel_opts.debug_mel = False
    return knf.OnlineFbank(opts)


# ---------------------------------------------------------------------------
# 参数映射（纯函数，离线可测）
# ---------------------------------------------------------------------------
def duration_to_frames(duration_s: float) -> int:
    """时长（秒）→ 10ms 帧数（四舍五入、下界 1）。"""
    return max(1, int(round(float(duration_s) / FRAME_SHIFT_S)))


@dataclass(frozen=True)
class VADParams:
    """FireRedVAD 流式参数（全部以 10ms 帧为单位，除阈值/平滑窗）。"""

    min_speech_frames: int
    min_silence_frames: int
    prefix_padding_frames: int
    max_speech_frames: int
    smooth_window: int
    activation_threshold: float

    @classmethod
    def from_durations(
        cls,
        *,
        min_speech_duration: float = _DEFAULT_MIN_SPEECH_S,
        min_silence_duration: float = _DEFAULT_MIN_SILENCE_S,
        prefix_padding_duration: float = _DEFAULT_PREFIX_PADDING_S,
        max_buffered_speech: float = _DEFAULT_MAX_BUFFERED_S,
        activation_threshold: float = _DEFAULT_THRESHOLD,
        smooth_window: int = _DEFAULT_SMOOTH,
    ) -> VADParams:
        return cls(
            min_speech_frames=duration_to_frames(min_speech_duration),
            min_silence_frames=duration_to_frames(min_silence_duration),
            prefix_padding_frames=duration_to_frames(prefix_padding_duration),
            max_speech_frames=duration_to_frames(max_buffered_speech),
            smooth_window=max(1, int(smooth_window)),
            activation_threshold=float(activation_threshold),
        )


# ---------------------------------------------------------------------------
# 状态机（纯函数，离线可测；镜像官方 StreamVadPostprocessor 语义）
# ---------------------------------------------------------------------------
@unique
class VADState(Enum):
    SILENCE = 0
    POSSIBLE_SPEECH = 1
    SPEECH = 2
    POSSIBLE_SILENCE = 3


@dataclass(frozen=True)
class FrameVerdict:
    """单帧判定（process 的返回，纯数据）。"""

    frame_idx: int  # 1-based
    prob: float
    smoothed_prob: float
    is_speech: bool  # 平滑后过阈值
    speaking: bool  # 状态机当前=SPEECH
    speech_start: bool = False  # 本帧触发 onset 确认
    speech_end: bool = False  # 本帧触发 offset 确认


class FireRedStateMachine:
    """raw prob → 平滑(窗口 N) → 阈值二值化 → 四态滞回（SILENCE/POSSIBLE_SPEECH/
    SPEECH/POSSIBLE_SILENCE），min_speech 帧确认起、min_silence 帧确认落。

    与官方 ``StreamVadPostprocessor`` 同构（含 max_speech 强制断段 + 断后补一发
    start），但去掉音频相关的 pad_start_frame（pre-roll 由 ``__SpeechBuffer`` 承担）。
    """

    def __init__(self, params: VADParams) -> None:
        self._params = params
        self.reset()

    def reset(self) -> None:
        self._frame_cnt = 0
        self._window: deque[float] = deque()
        self._window_sum = 0.0
        self._state = VADState.SILENCE
        self._speech_cnt = 0
        self._silence_cnt = 0
        self._hit_max = False

    @property
    def state(self) -> VADState:
        return self._state

    @property
    def speaking(self) -> bool:
        return self._state is VADState.SPEECH

    def _smooth(self, prob: float) -> float:
        w = self._params.smooth_window
        if w <= 1:
            return prob
        self._window.append(prob)
        self._window_sum += prob
        if len(self._window) > w:
            self._window_sum -= self._window.popleft()
        return self._window_sum / len(self._window)

    def process(self, prob: float) -> FrameVerdict:
        p = self._params
        self._frame_cnt += 1
        smoothed = self._smooth(prob)
        is_speech = smoothed >= p.activation_threshold
        start = False
        end = False

        if self._hit_max:
            start = True
            self._hit_max = False

        if self._state is VADState.SILENCE:
            if is_speech:
                self._state = VADState.POSSIBLE_SPEECH
                self._speech_cnt += 1
            else:
                self._silence_cnt += 1
                self._speech_cnt = 0

        elif self._state is VADState.POSSIBLE_SPEECH:
            if is_speech:
                self._speech_cnt += 1
                if self._speech_cnt >= p.min_speech_frames:
                    self._state = VADState.SPEECH
                    start = True
                    self._silence_cnt = 0
            else:
                self._state = VADState.SILENCE
                self._silence_cnt = 1
                self._speech_cnt = 0

        elif self._state is VADState.SPEECH:
            self._speech_cnt += 1
            if is_speech:
                self._silence_cnt = 0
                if self._speech_cnt >= p.max_speech_frames:
                    self._hit_max = True
                    self._speech_cnt = 0
                    end = True
            else:
                self._state = VADState.POSSIBLE_SILENCE
                self._silence_cnt += 1

        elif self._state is VADState.POSSIBLE_SILENCE:
            self._speech_cnt += 1
            if is_speech:
                self._state = VADState.SPEECH
                self._silence_cnt = 0
                if self._speech_cnt >= p.max_speech_frames:
                    self._hit_max = True
                    self._speech_cnt = 0
                    end = True
            else:
                self._silence_cnt += 1
                if self._silence_cnt >= p.min_silence_frames:
                    self._state = VADState.SILENCE
                    end = True
                    self._speech_cnt = 0

        return FrameVerdict(
            frame_idx=self._frame_cnt,
            prob=prob,
            smoothed_prob=smoothed,
            is_speech=is_speech,
            speaking=self.speaking,
            speech_start=start,
            speech_end=end,
        )


def simulate(probs, params: VADParams) -> list[FrameVerdict]:
    """纯函数：一串 prob → 事件序列（离线单测入口）。"""
    sm = FireRedStateMachine(params)
    return [sm.process(float(p)) for p in probs]


# ---------------------------------------------------------------------------
# pre-roll 缓冲（silero _copy_speech_buffer 等价；离线可测）
# ---------------------------------------------------------------------------
class _SpeechBuffer:
    """游标式 pre-roll 缓冲：静音时只保留最近 prefix 样本，说话中累积。

    START/END 事件取 ``frame()``=``[:index]``（prefix + 本段已累积 PCM）。语义与
    livekit 官方 silero ``_copy_speech_buffer`` / ``_reset_write_cursor`` 等价。
    """

    def __init__(self, max_samples: int, prefix_padding_samples: int) -> None:
        self._prefix = max(0, int(prefix_padding_samples))
        self._buf = np.zeros(max(0, int(max_samples)) + self._prefix, dtype=np.int16)
        self._index = 0
        self._max_reached = False

    def clear(self) -> None:
        self._buf.fill(0)
        self._index = 0
        self._max_reached = False

    @property
    def index(self) -> int:
        return self._index

    def append(self, pcm: bytes) -> None:
        samples = np.frombuffer(pcm, dtype=np.int16)
        space = len(self._buf) - self._index
        n = min(len(samples), space)
        if n > 0:
            self._buf[self._index : self._index + n] = samples[:n]
            self._index += n
        if n < len(samples) and not self._max_reached:
            self._max_reached = True

    def reset_cursor(self) -> None:
        if self._index <= self._prefix:
            return
        self._buf[: self._prefix] = self._buf[self._index - self._prefix : self._index]
        self._index = self._prefix
        self._max_reached = False

    def frame(self, sample_rate: int) -> rtc.AudioFrame:
        return rtc.AudioFrame(
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=self._index,
            data=self._buf[: self._index].tobytes(),
        )


# ---------------------------------------------------------------------------
# ONNX 模型（进程级共享 session + 运行时 I/O 内省）
# ---------------------------------------------------------------------------
class _FireRedOnnxModel:
    """FireRedVAD 流式 ONNX 包装：session 进程级共享，I/O 名/形状运行时内省。"""

    def __init__(self, model_path: str | os.PathLike[str]) -> None:
        import onnxruntime as ort

        path = str(model_path)
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            path, sess_options=so, providers=["CPUExecutionProvider"]
        )
        self._resolve_io()
        print(
            f"FIRERED_VAD model loaded path={Path(path).name} "
            f"feat={self._feat_input}({self._feat_shape}) "
            f"cache={self._cache_input}({self._cache_shape}) "
            f"probs={self._prob_output} cache_out={self._cache_output}",
            flush=True,
        )

    def _resolve_io(self) -> None:
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()

        def _pick(items, name, rank):
            for it in items:
                if it.name == name:
                    return it
            for it in items:
                if len(it.shape) == rank:
                    return it
            return items[0] if items else None

        feat = _pick(inputs, "feat", 3)
        if feat is None:
            raise ValueError("FireRedVAD ONNX has no inputs")
        cache_in = next((i for i in inputs if i.name != feat.name), None)
        prob = _pick(outputs, "probs", 3)
        if prob is None:
            raise ValueError("FireRedVAD ONNX has no outputs")
        cache_out = next((o for o in outputs if o.name != prob.name), None)

        self._feat_input = feat.name
        self._feat_shape = tuple(feat.shape)
        self._cache_input = cache_in.name if cache_in is not None else None
        cache_shape = tuple(cache_in.shape) if cache_in is not None else None
        if cache_shape is not None and not all(isinstance(d, int) for d in cache_shape):
            raise ValueError(f"FireRedVAD cache input shape not fully static: {cache_shape}")
        self._cache_shape = cache_shape
        self._prob_output = prob.name
        self._cache_output = cache_out.name if cache_out is not None else None

    @property
    def name(self) -> str:
        return "fireredvad-stream-vad-with-cache"

    def zeros_cache(self) -> np.ndarray | None:
        if self._cache_shape is None:
            return None
        return np.zeros(self._cache_shape, dtype=np.float32)

    def infer(self, feat: np.ndarray, cache: np.ndarray | None) -> tuple[float, np.ndarray | None]:
        """feat (1,1,80) + cache → (prob, new_cache)。纯同步 CPU 调用（放线程池）。"""
        feeds: dict[str, np.ndarray] = {self._feat_input: feat}
        if self._cache_input is not None:
            feeds[self._cache_input] = cache
        names = [self._prob_output]
        if self._cache_output is not None:
            names.append(self._cache_output)
        outs = self._session.run(names, feeds)
        prob = float(np.asarray(outs[0]).reshape(-1)[0])
        new_cache = np.asarray(outs[1]) if self._cache_output is not None else None
        return prob, new_cache


_MODELS: dict[str, _FireRedOnnxModel] = {}


def _get_model(model_path: str | os.PathLike[str]) -> _FireRedOnnxModel:
    """进程级模型单例（按绝对路径缓存；组装失败即抛，由 load_firered_vad 兜）。"""
    key = str(Path(model_path).resolve())
    model = _MODELS.get(key)
    if model is None:
        model = _FireRedOnnxModel(key)
        _MODELS[key] = model
    return model


# ---------------------------------------------------------------------------
# livekit VAD / VADStream
# ---------------------------------------------------------------------------
class FireRedVAD(vad.VAD):
    """FireRedVAD 流式 VAD（livekit ``vad.VAD``）。每流独立 cache + executor。"""

    def __init__(
        self,
        *,
        model: _FireRedOnnxModel,
        cmvn_mean: np.ndarray,
        cmvn_istd: np.ndarray,
        params: VADParams,
        executor: Executor | None = None,
    ) -> None:
        super().__init__(capabilities=vad.VADCapabilities(update_interval=FRAME_SHIFT_S))
        self._model = model
        self._mean = np.asarray(cmvn_mean, dtype=np.float32)
        self._istd = np.asarray(cmvn_istd, dtype=np.float32)
        self._opts = params
        self._executor = executor
        self._streams: weakref.WeakSet[FireRedVADStream] = weakref.WeakSet()

    @property
    def model(self) -> str:
        return self._model.name

    @property
    def provider(self) -> str:
        return "firered"

    def stream(self) -> vad.VADStream:
        stream = FireRedVADStream(self, self._opts, executor=self._executor)
        self._streams.add(stream)
        return stream


class FireRedVADStream(vad.VADStream):
    def __init__(self, parent: FireRedVAD, params: VADParams, *, executor: Executor | None) -> None:
        super().__init__(parent)
        self._opts = params
        self._model = parent._model
        self._mean = parent._mean
        self._istd = parent._istd
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="firered.vad"
        )
        self._sm = FireRedStateMachine(params)
        self._cache: np.ndarray | None = self._model.zeros_cache()
        self._fbank = _make_fbank()
        self._fbank_read = 0
        self._speech: _SpeechBuffer | None = None
        self._input_sample_rate = 0
        self._input_copy_remaining_fract = 0.0

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            if self._owns_executor:
                self._executor.shutdown(wait=False, cancel_futures=True)

    def _reset_state(self) -> None:
        self._sm.reset()
        self._cache = self._model.zeros_cache()
        self._fbank = _make_fbank()
        self._fbank_read = 0
        self._input_copy_remaining_fract = 0.0
        if self._speech is not None:
            self._speech.clear()

    def _prep_feat(self, frame: np.ndarray) -> np.ndarray:
        # (80,) → (1,1,80)，cmvn 归一（与官方 CMVN.__call__ 同式）
        feat = (frame.astype(np.float32) - self._mean) * self._istd
        return feat.reshape(1, 1, -1)

    @utils.log_exceptions(logger=logger)
    async def _main_task(self) -> None:
        loop = asyncio.get_running_loop()
        params = self._opts
        window_duration = FRAME_SHIFT_S

        pub_speaking = False
        pub_speech_duration = 0.0
        pub_silence_duration = 0.0
        pub_current_sample = 0
        pub_timestamp = 0.0
        speech_threshold_duration = 0.0
        silence_threshold_duration = 0.0

        input_frames: list[rtc.AudioFrame] = []
        inference_frames: list[rtc.AudioFrame] = []
        resampler: rtc.AudioResampler | None = None

        def _reset_state() -> None:
            nonlocal pub_speaking, pub_speech_duration, pub_silence_duration
            nonlocal pub_current_sample, pub_timestamp
            nonlocal speech_threshold_duration, silence_threshold_duration
            nonlocal input_frames, inference_frames, resampler
            self._reset_state()
            pub_speaking = False
            pub_speech_duration = 0.0
            pub_silence_duration = 0.0
            pub_current_sample = 0
            pub_timestamp = 0.0
            speech_threshold_duration = 0.0
            silence_threshold_duration = 0.0
            input_frames = []
            inference_frames = []
            resampler = self._make_resampler()

        def _trim(in_frame: rtc.AudioFrame, to_copy_int: int, inf_frame: rtc.AudioFrame) -> None:
            """去掉本 hop 已消费部分，余量回填（silero 同款）。"""
            nonlocal input_frames, inference_frames
            in_rest = len(in_frame.data) - to_copy_int * 2
            input_frames = (
                [
                    rtc.AudioFrame(
                        data=in_frame.data[to_copy_int * 2 :].tobytes(),
                        sample_rate=self._input_sample_rate,
                        num_channels=1,
                        samples_per_channel=in_rest // 2,
                    )
                ]
                if in_rest > 0
                else []
            )
            inf_rest = len(inf_frame.data) - FRAME_SHIFT_SAMPLE * 2
            inference_frames = (
                [
                    rtc.AudioFrame(
                        data=inf_frame.data[FRAME_SHIFT_SAMPLE * 2 :].tobytes(),
                        sample_rate=SAMPLE_RATE,
                        num_channels=1,
                        samples_per_channel=inf_rest // 2,
                    )
                ]
                if inf_rest > 0
                else []
            )

        async for input_frame in self._input_ch:
            if isinstance(input_frame, self._FlushSentinel):
                _reset_state()
                continue
            if not isinstance(input_frame, rtc.AudioFrame):
                continue

            if not self._input_sample_rate:
                self._input_sample_rate = input_frame.sample_rate
                # 缓冲按输入采样率分配；prefix/max 亦换算到输入采样率
                prefix_samples = int(
                    params.prefix_padding_frames
                    * FRAME_SHIFT_SAMPLE
                    * self._input_sample_rate
                    / SAMPLE_RATE
                )
                max_samples = int(
                    params.max_speech_frames
                    * FRAME_SHIFT_SAMPLE
                    * self._input_sample_rate
                    / SAMPLE_RATE
                )
                self._speech = _SpeechBuffer(max_samples, prefix_samples)
                resampler = self._make_resampler()
            elif self._input_sample_rate != input_frame.sample_rate:
                logger.error("a frame with another sample rate was already pushed")
                continue

            input_frames.append(input_frame)
            if resampler is not None:
                inference_frames.extend(resampler.push(input_frame))
            else:
                inference_frames.append(input_frame)

            while True:
                available = sum(f.samples_per_channel for f in inference_frames)
                if available < FRAME_SHIFT_SAMPLE:
                    break

                in_frame = utils.combine_frames(input_frames)
                inf_frame = utils.combine_frames(inference_frames)

                # 本 10ms hop 消费的 16k 样本（喂 fbank）
                infer_window = np.frombuffer(
                    inf_frame.data[: FRAME_SHIFT_SAMPLE * 2], dtype=np.int16
                )
                # 与 hop 对应的输入采样率样本数（分数进位防漂移，silero 同款）
                ratio = self._input_sample_rate / SAMPLE_RATE
                to_copy = FRAME_SHIFT_SAMPLE * ratio + self._input_copy_remaining_fract
                to_copy_int = int(to_copy)
                self._input_copy_remaining_fract = to_copy - to_copy_int
                hop_bytes = bytes(in_frame.data[: to_copy_int * 2])

                # 音频铺排：每 hop 恰好 append 一次（不重叠——消费侧连续性前提）
                assert self._speech is not None
                self._speech.append(hop_bytes)

                # fbank 喂入 + 取新 ready 的帧（预热期 0，之后每 hop 1）。
                # 帧 t 的窗=[t*160,t*160+400)，喂满该窗才 ready（固有 25ms 延迟）；
                # 本 hop 的 PCM 挂到「此刻刚 ready 那帧」的概率上（配对偏移≈20ms）。
                self._fbank.accept_waveform(SAMPLE_RATE, infer_window.tolist())
                start_time = time.perf_counter()
                last_prob: float | None = None
                started = False
                ended = False
                is_speech = False
                while self._fbank_read < self._fbank.num_frames_ready:
                    fb = np.asarray(self._fbank.get_frame(self._fbank_read), dtype=np.float32)
                    self._fbank_read += 1
                    prob, self._cache = await loop.run_in_executor(
                        self._executor, self._model.infer, self._prep_feat(fb), self._cache
                    )
                    verdict = self._sm.process(prob)
                    last_prob = prob
                    started = started or verdict.speech_start
                    ended = ended or verdict.speech_end
                    is_speech = verdict.is_speech
                inference_duration = time.perf_counter() - start_time

                if last_prob is not None:
                    pub_current_sample += FRAME_SHIFT_SAMPLE
                    pub_timestamp += window_duration
                    if pub_speaking:
                        pub_speech_duration += window_duration
                    else:
                        pub_silence_duration += window_duration

                    self._event_ch.send_nowait(
                        vad.VADEvent(
                            type=vad.VADEventType.INFERENCE_DONE,
                            samples_index=pub_current_sample,
                            timestamp=pub_timestamp,
                            silence_duration=pub_silence_duration,
                            speech_duration=pub_speech_duration,
                            probability=last_prob,
                            inference_duration=inference_duration,
                            frames=[
                                rtc.AudioFrame(
                                    data=hop_bytes,
                                    sample_rate=self._input_sample_rate,
                                    num_channels=1,
                                    samples_per_channel=to_copy_int,
                                )
                            ],
                            speaking=pub_speaking,
                            raw_accumulated_silence=silence_threshold_duration,
                            raw_accumulated_speech=speech_threshold_duration,
                        )
                    )

                    if is_speech:
                        speech_threshold_duration += window_duration
                        silence_threshold_duration = 0.0
                    else:
                        silence_threshold_duration += window_duration
                        speech_threshold_duration = 0.0

                    if started and not pub_speaking:
                        pub_speaking = True
                        pub_silence_duration = 0.0
                        pub_speech_duration = speech_threshold_duration
                        self._event_ch.send_nowait(
                            vad.VADEvent(
                                type=vad.VADEventType.START_OF_SPEECH,
                                samples_index=pub_current_sample,
                                timestamp=pub_timestamp,
                                silence_duration=pub_silence_duration,
                                speech_duration=pub_speech_duration,
                                frames=[self._speech.frame(self._input_sample_rate)],
                                speaking=True,
                            )
                        )
                    elif ended and pub_speaking:
                        pub_speaking = False
                        pub_silence_duration = silence_threshold_duration
                        self._event_ch.send_nowait(
                            vad.VADEvent(
                                type=vad.VADEventType.END_OF_SPEECH,
                                samples_index=pub_current_sample,
                                timestamp=pub_timestamp,
                                silence_duration=pub_silence_duration,
                                speech_duration=max(
                                    0.0, pub_speech_duration - silence_threshold_duration
                                ),
                                frames=[self._speech.frame(self._input_sample_rate)],
                                speaking=False,
                            )
                        )
                        pub_speech_duration = 0.0
                        self._speech.reset_cursor()

                # 静音（非 possible-speech/speech）时回收游标，保留 0.5s pre-roll
                if not pub_speaking and self._sm.state is VADState.SILENCE:
                    self._speech.reset_cursor()

                _trim(in_frame, to_copy_int, inf_frame)

    def _make_resampler(self) -> rtc.AudioResampler | None:
        if not self._input_sample_rate or self._input_sample_rate == SAMPLE_RATE:
            return None
        return rtc.AudioResampler(
            input_rate=self._input_sample_rate,
            output_rate=SAMPLE_RATE,
            quality=rtc.AudioResamplerQuality.QUICK,
        )


def _resolve_model_dir(model_dir: str | os.PathLike[str] | None) -> Path:
    if model_dir is not None:
        return Path(model_dir)
    raw = os.environ.get("BOK_FIRERED_MODEL_DIR", "").strip()
    return Path(raw) if raw else _DEFAULT_ASSET_DIR


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def load_firered_vad(
    *,
    min_speech_duration: float = _DEFAULT_MIN_SPEECH_S,
    min_silence_duration: float = _DEFAULT_MIN_SILENCE_S,
    prefix_padding_duration: float = _DEFAULT_PREFIX_PADDING_S,
    max_buffered_speech: float = _DEFAULT_MAX_BUFFERED_S,
    activation_threshold: float | None = None,
    smooth_window: int | None = None,
    model_dir: str | os.PathLike[str] | None = None,
    executor: Executor | None = None,
) -> FireRedVAD:
    """装配 FireRedVAD（缺依赖/缺模型抛清晰异常，调用方回退 silero）。

    activation_threshold 缺省读 ``BOK_FIRERED_THRESHOLD``（再缺省 0.5——**不是**
    silero 的 0.75）；smooth_window 缺省读 ``BOK_FIRERED_SMOOTH``（再缺省 5）。
    """
    model_dir_p = _resolve_model_dir(model_dir)
    model_path = model_dir_p / _MODEL_FILENAME
    cmvn_path = model_dir_p / _CMVN_FILENAME
    if not model_path.is_file():
        raise FileNotFoundError(f"FireRedVAD ONNX 缺失：{model_path}")
    if not cmvn_path.is_file():
        raise FileNotFoundError(f"FireRedVAD cmvn.ark 缺失：{cmvn_path}")

    try:
        import kaldi_native_fbank  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "kaldi-native-fbank 未安装（pip install kaldi-native-fbank）"
        ) from exc

    mean, istd = read_kaldi_cmvn(cmvn_path)
    model = _get_model(model_path)

    if activation_threshold is None:
        activation_threshold = _env_float("BOK_FIRERED_THRESHOLD", _DEFAULT_THRESHOLD)
    if smooth_window is None:
        smooth_window = _env_int("BOK_FIRERED_SMOOTH", _DEFAULT_SMOOTH)

    params = VADParams.from_durations(
        min_speech_duration=min_speech_duration,
        min_silence_duration=min_silence_duration,
        prefix_padding_duration=prefix_padding_duration,
        max_buffered_speech=max_buffered_speech,
        activation_threshold=activation_threshold,
        smooth_window=smooth_window,
    )
    print(
        f"FIRERED_VAD assembled dir={model_dir_p.name} "
        f"min_speech_frames={params.min_speech_frames} "
        f"min_silence_frames={params.min_silence_frames} "
        f"prefix_frames={params.prefix_padding_frames} "
        f"threshold={params.activation_threshold} smooth={params.smooth_window}",
        flush=True,
    )
    return FireRedVAD(
        model=model, cmvn_mean=mean, cmvn_istd=istd, params=params, executor=executor
    )
