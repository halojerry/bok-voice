"""W5 声纹锁（2026-10-06 demo-quality-wave）：enrollment + 段相似度 pre-ASR 门。

需求（Ethan 2026-10-06）：环境音/旁人声不许冒充通话对象——实弹痛点 `call-15c712aa`
有一轮「用户说话」是 ASR 把键盘噪声幻觉成「打打键盘的声音。」。门的位置=VAD 段与
ASR 之间：第一段**确证语音**（调用方喂入，模块不自己判定「确证」）做 enrollment；
之后每个 VAD 段算 embedding 相似度，低于 drop 阈值 → 判「不是通话对象」→ 调用方
不喂 ASR、不成轮。总闸 env `BOK_SPEAKER_LOCK`（默认 "0" 关；已注册
tools/bokctl/env.py ``_FORWARD_ENV`` 2026-10-06，开关关闭=模块零行为全放行）。

v1 技术定案：纯 numpy 统计声纹（零新依赖）——mel 风格对数谱统计嵌入：
分帧（25ms 窗/10ms hop）→ 汉宁窗 → 功率谱 → mel 滤波器组（24 带，HTK 刻度）→
log（峰值参考 -60dB 相对地板，见下）→ 逐帧带均值归零（谱形状白化）→ 静音帧剔除
（帧功率 < 峰值×0.01）→ 按带均值+标准差拼 2×24=48 维向量 → L2 归一。
log 域两件处理的目的：相对地板保证无 -inf 且对录音增益完全不变（换麦克风/距离
音量漂移零影响）；带均值归零剥掉 log 域公共电平/宽带倾斜（标定实测：不归零时
同人 0.99 与异人/噪声 0.8+ 挤在一起没有分离度，归零后只留带间相对形状——
共振峰/谐波结构说话，白噪/键盘噪声塌向近零向量）。

**v1 精度边界（明账）**：这是统计声纹，不是神经声纹（resemblyzer/ECAPA 级）——
①区分「同一说话人 vs 键盘噪声/白噪/另一把基频共振峰不同的嗓音」够用（合成素材
下同人/异人/噪声的相似度分布钉在 tests/test_speaker_lock.py）；②跨信道（蓝牙
耳机带宽受限）与强噪声底会拉低同人相似度——阈值保守设置+强烈偏向不误杀：段太短
/未登记/总闸关一律放行（fail-open），只有「已登记且相似度明确低于 drop 阈」才丢；
③非司法级，不防录音回放/变声欺骗。

**v2 升级路径**：若实弹误杀率高（同人相似度掉进 drop 阈以下），换 ECAPA-TDNN
ONNX 小模型（CPU 每段 ~20ms，模型入 ensure 可选档不默认下载）——单点替换
``embed_pcm`` 即可，cosine/门状态机/接线全部不动。

接线点建议（本模块零 agent.py 改动，接线任务后置；doubao 与本地 ASR 两路同挂）::

    _lock = SpeakerLock()                          # worker 装配处单点
    # 每个 VAD 段（16k mono s16le bytes）进 ASR 前：
    ok, reason = _lock.admit(seg_pcm)              # "enroll"|"hit"|"drop"|"relock"
    if not ok:
        print(speaker_lock_drop_line(sim, dur_s))  # 打点后直接丢段
        return                                     # 不喂 ASR、不成轮
    if reason == "enroll" and seg_confirmed and not _lock.enrolled:
        _lock.enroll(seg_pcm)                      # 首个确证语音段登记
    if reason == "relock":
        print(speaker_relock_line(after_n))        # 客户换蓝牙耳机等场景自动重锚

子键调参：``BOK_SPEAKER_LOCK_DROP_SIM``/``BOK_SPEAKER_LOCK_RELOCK_SIM``
**故意不读 env**（``test_forward_env`` 扫 agent_runtime 全部 ``os.environ``
读取面，任何未注册键 CI 红）——两键做成构造参数（缺省值=模块常量），运行时
调参=改构造点；模块只读已注册的总闸 ``BOK_SPEAKER_LOCK``。
"""

from __future__ import annotations

import functools
import math
import os

import numpy as np

# 总闸（唯一 env 读取面；已注册 _FORWARD_ENV，test_forward_env 扫描面内）。
SPEAKER_LOCK_ENV = "BOK_SPEAKER_LOCK"

# 默认阈值（构造参数缺省单源；0.55/0.40/3 为合成素材标定值，见
# tests/test_speaker_lock.py 与交付报告——同人段相似度 ≥0.8、异人/噪声 <0.4，
# 0.55/0.40 两档留足余量且强烈偏向不误杀）。
DEFAULT_DROP_SIM = 0.55
DEFAULT_RELOCK_SIM = 0.40
DEFAULT_RELOCK_STREAK = 3
# enrollment 最短段（spec 定案：<1s 拒绝登记）。
ENROLL_MIN_SECONDS = 1.0

_FRAME_S = 0.025
_HOP_S = 0.010
_N_MELS = 24
_MEL_FMIN_HZ = 50.0
# 静音帧剔除：帧功率 < 全段峰值功率 × 0.01（≈-20dB）视为静音/间隙帧，
# 不进统计（语段首尾静音、敲击间隙不稀释谱形）。
_SILENCE_POWER_RATIO = 0.01
# 少于此帧数=段太短不可嵌入（embed_pcm 抛 ValueError）。
_MIN_FRAMES = 5
# log 地板：峰值参考的相对下限（-60dB），同时消灭绝对电平依赖与 log(0)。
_LOG_REL_FLOOR = 1e-6

_GATE_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def _gate_enabled() -> bool:
    """总闸：``BOK_SPEAKER_LOCK`` 缺省 "0"=关（零行为全放行）。"""
    return os.environ.get("BOK_SPEAKER_LOCK", "0").strip().lower() not in _GATE_OFF_VALUES


def _hz_to_mel(f: float) -> float:
    return 2595.0 * math.log10(1.0 + f / 700.0)


def _mel_to_hz(m: float) -> float:
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


@functools.lru_cache(maxsize=8)
def _mel_filterbank(n_mels: int, n_fft: int, sr: int) -> np.ndarray:
    """三角 mel 滤波器组（HTK 刻度，形状 (n_mels, n_fft//2+1)；只读不写）。"""
    n_bins = n_fft // 2 + 1
    m_min = _hz_to_mel(_MEL_FMIN_HZ)
    m_max = _hz_to_mel(sr / 2.0)
    hz_pts = _mel_to_hz(np.linspace(m_min, m_max, n_mels + 2))
    bins = np.floor((n_fft + 1) * hz_pts / sr).astype(np.int64)
    fb = np.zeros((n_mels, n_bins), dtype=np.float64)
    for i in range(n_mels):
        left, center, right = int(bins[i]), int(bins[i + 1]), int(bins[i + 2])
        if right <= left:  # 零宽保护（极低采样率下可能退化）
            continue
        center = min(max(center, left + 1), right - 1) if right - left > 1 else left
        ramp_up = np.arange(left, center) - left
        denom_up = max(center - left, 1)
        ramp_down = right - np.arange(center, right)
        denom_down = max(right - center, 1)
        fb[i, left:center] = ramp_up / denom_up
        fb[i, center:right] = ramp_down / denom_down
    return fb


def embed_pcm(pcm: bytes, sr: int = 16000) -> np.ndarray:
    """16k mono s16le PCM → 48 维 L2 归一化声纹向量（2×``_N_MELS`` 均值+标准差）。

    段太短（不足 :data:`_MIN_FRAMES` 帧）或全静音（数字零）抛 :class:`ValueError`；
    调用方（enroll/admit）按 fail-open 处理。重采样不做——约定调用方给 16k。
    """
    n_samples = len(pcm) // 2  # s16le；奇数字节截断
    if n_samples <= 0:
        raise ValueError(f"speaker_lock: empty pcm ({len(pcm)} bytes)")
    samples = np.frombuffer(pcm, dtype="<i2", count=n_samples).astype(np.float64) / 32768.0

    frame_len = max(1, int(_FRAME_S * sr))
    hop = max(1, int(_HOP_S * sr))
    if len(samples) < frame_len:
        raise ValueError(f"speaker_lock: pcm too short ({len(samples)} samples < 1 frame)")
    n_frames = 1 + (len(samples) - frame_len) // hop
    if n_frames < _MIN_FRAMES:
        raise ValueError(f"speaker_lock: pcm too short ({n_frames} frames < {_MIN_FRAMES})")

    frames = np.lib.stride_tricks.sliding_window_view(samples, frame_len)[::hop]
    window = np.hanning(frame_len)
    spec = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2  # (n_frames, n_bins)

    fb = _mel_filterbank(_N_MELS, frame_len, sr)
    mel_pow = spec @ fb.T  # (n_frames, n_mels)
    peak = float(mel_pow.max())
    if peak <= 0.0:
        raise ValueError("speaker_lock: all-silent pcm (digital zero)")
    # 静音帧剔除（峰值功率 -20dB 以下不进统计；全被剔则保留全部帧）。
    frame_power = spec.sum(axis=1)
    keep = frame_power >= frame_power.max() * _SILENCE_POWER_RATIO
    if int(keep.sum()) >= _MIN_FRAMES:
        mel_pow = mel_pow[keep]
    # log：峰值参考 -60dB 相对地板（无 -inf、无绝对电平依赖），随后逐帧做带均值
    # 归零（谱形状白化）——log 域的宽带倾斜/公共电平对所有声音几乎同形（实测
    # 不归零时同人 0.99 与异人/噪声 0.8+ 挤在一起，cosine 没有分离度），归零后
    # 只留「带间相对形状」：共振峰/谐波结构说话，白噪/键盘噪声塌向零向量。
    logmel = np.log(np.maximum(mel_pow, peak * _LOG_REL_FLOOR))
    logmel -= logmel.mean(axis=1, keepdims=True)

    feats = np.concatenate([logmel.mean(axis=0), logmel.std(axis=0)])
    norm = float(np.linalg.norm(feats))
    if norm <= 0.0:
        raise ValueError("speaker_lock: degenerate embedding (zero norm)")
    return feats / norm


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """余弦相似度；任一零向量返回 0.0（保守：不相似）。"""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def speaker_lock_drop_line(sim: float, dur_s: float) -> str:
    """丢段打点行（调用方 print；sim 两位小数、时长一位小数）。"""
    return f"SPEAKER_LOCK_DROP sim={sim:.2f} dur={dur_s:.1f}s"


def speaker_relock_line(after: int) -> str:
    """重锁打点行（after=触发重锁时的连续低相似段数）。"""
    return f"SPEAKER_RELOCK after={after}"


class SpeakerLock:
    """1v1 声纹锁：:meth:`enroll` 登记通话对象，:meth:`admit` 逐 VAD 段把门。

    admit 返回 ``(放行?, 原因)``，原因四值：
      - ``"enroll"``：放行且**未做声纹判定**（总闸关 / 未登记 / 段太短不可嵌入
        ——fail-open，强烈偏向不误杀）；
      - ``"hit"``：相似度 ≥ ``drop_sim``，判同人放行；
      - ``"drop"``：相似度 < ``drop_sim``，判非通话对象，调用方应丢段不喂 ASR；
      - ``"relock"``：连续 ``relock_streak`` 段相似度 < ``relock_sim``（严重不匹配
        ——键盘噪声/另一把嗓音/信道突变），以**触发段**为新锚重登记并放行
        （客户换蓝牙耳机不被锁死）。

    两档阈值语义：``[relock_sim, drop_sim)`` 为灰区——丢（保守），但**不计入**
    重锁连击（证据不够强不重锚，防噪声串偷锚）；``< relock_sim`` 丢且连击 +1。
    """

    def __init__(
        self,
        *,
        drop_sim: float = DEFAULT_DROP_SIM,
        relock_sim: float = DEFAULT_RELOCK_SIM,
        relock_streak: int = DEFAULT_RELOCK_STREAK,
        sample_rate: int = 16000,
    ) -> None:
        self.drop_sim = float(drop_sim)
        self.relock_sim = float(relock_sim)
        self.relock_streak = max(1, int(relock_streak))
        self.sample_rate = int(sample_rate)
        self._centroid: np.ndarray | None = None
        self._severe_streak = 0

    @property
    def enrolled(self) -> bool:
        return self._centroid is not None

    def enroll(self, pcm: bytes) -> bool:
        """首个确证语音段登记（<1s 或不可嵌入拒绝；返回是否成功）。"""
        if len(pcm) < 2 * int(ENROLL_MIN_SECONDS * self.sample_rate):
            return False
        try:
            vec = embed_pcm(pcm, self.sample_rate)
        except ValueError:
            return False
        self._centroid = vec
        self._severe_streak = 0
        return True

    def admit(self, pcm: bytes) -> tuple[bool, str]:
        """逐段把门（16k mono s16le bytes，与 VAD 段一致）。"""
        if not _gate_enabled():
            return (True, "enroll")  # 总闸关=零行为（零 DSP 成本）
        if self._centroid is None:
            return (True, "enroll")  # 未登记：放行待登记
        vec = self._embed(pcm)
        if vec is None:
            return (True, "enroll")  # 段太短不可嵌入：fail-open
        sim = cosine(vec, self._centroid)
        if sim >= self.drop_sim:
            self._severe_streak = 0
            return (True, "hit")
        if sim < self.relock_sim:
            self._severe_streak += 1
            if self._severe_streak >= self.relock_streak:
                self._centroid = vec  # 重锁=触发段即新锚
                self._severe_streak = 0
                return (True, "relock")
        return (False, "drop")

    def _embed(self, pcm: bytes) -> np.ndarray | None:
        """嵌入失败（段太短/全静音）返回 None；测试可子类覆盖钉状态机。"""
        try:
            return embed_pcm(pcm, self.sample_rate)
        except ValueError:
            return None
