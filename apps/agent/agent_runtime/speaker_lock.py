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

**v1 精度边界（2026-10-07 真语音实弹改判，probe_speaker_lock 四把 macOS 嗓音×
office 底噪三档 SNR）**：白化 mel 统计嵌入在真嗓音上**只分「语音 vs 非语音」，
不分「谁在说话」**——同人净音频 0.940-0.995、同人+底噪(0dB SNR) ≥0.859、纯
office 底噪段 0.694-0.697、**异人嗓音 0.816-0.973 与同人带噪区间重叠**（男女声
之间也 0.85+；早期手搓谐波栈素材的 0.03-0.14 异人分离度**不迁移**到真嗓音，
已勘误）。因此 v1 的诚实能力=**环境音/键盘噪声滤除**（Ethan 的原始痛点：
call-15c712aa 键盘噪声被 ASR 幻听成轮），阈值按真语音分布重标 0.78/0.65；
**「旁人声冒充通话对象」的区分需要 v2 神经声纹**（ECAPA-TDNN ONNX，CPU 每段
~20ms，模型入 ensure 可选档；单点替换 :func:`embed_pcm`，cosine/门状态机/
接线全部不动——那是独立票，落地前勿对本模块抱「认人」预期）。跨信道（蓝牙
带宽受限）会拉低同人相似度——阈值保守+强烈偏向不误杀：段太短/未登记/总闸关
一律放行（fail-open），只有「已登记且相似度明确低于 drop 阈」才丢；非司法级，
不防录音回放/变声欺骗。

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

# 默认阈值（构造参数缺省单源；**2026-10-07 真语音重标**——probe_speaker_lock
# 四嗓音实测：同人+0dB 底噪 ≥0.859 | 纯 office 底噪 0.694-0.697 | 异人 0.816-0.973
# （与同人重叠=不可分，见上「v1 精度边界」）。0.78/0.65=噪声上界与带噪同人下界
# 的居中，两侧 ≥0.08 余量；能力=环境音滤除，非认人。旧 0.55/0.40 系手搓合成
# 素材标定值，真语音上连纯底噪 0.69 都放行=恒 no-op，已废）。
DEFAULT_DROP_SIM = 0.78
DEFAULT_RELOCK_SIM = 0.65
DEFAULT_RELOCK_STREAK = 3
# enrollment 最短段（spec 定案：<1s 拒绝登记）。
ENROLL_MIN_SECONDS = 1.0
# running-mean 登记上限（前 N 个确证段滚动平均成锚）。
ENROLL_MAX_SAMPLES = 3

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


# ---- v2 ECAPA 嵌入器（2026-10-07 认人票）-------------------------------------
# 模型=官方 speechbrain/spkrec-ecapa-voxceleb（Apache-2.0）自导出单文件 ONNX
# （wav→Fbank80→CMVN→ECAPA→192 维整管线，导出脚本 scripts/seed/export_ecapa_onnx.py；
# sha256=fa492b67…3574e4，84MB）。**激活=显式 env ``BOK_SPEAKER_LOCK_MODEL`` 指路
# （只认 env,不自动探测磁盘——本机碰巧有模型文件不许泄进单测/意外换档）**；
# 未设/文件缺位/坏档=fail-open 回 v1 mel 统计嵌入（字节不变）。部署位惯例=
# ``<app-data>/models/ecapa_tdnn_voxceleb.onnx``（serve 侧 env 指过去）。
# v2 阈值（四嗓音 TTS 语料 provisional，0dB SNR 极端臂=灰区由段末复核+relock 兜）：
#   同人净 ≥0.78 | 异人 max 0.57（v1 完全无分离）| 纯噪声 ≈0——0.62/0.40 取
#   「同人净下界-0.16 / 异人上界+0.05」。
ECAPA_MODEL_ENV = "BOK_SPEAKER_LOCK_MODEL"
ECAPA_DROP_SIM = 0.62
ECAPA_RELOCK_SIM = 0.40
_ONNX_STATE: dict = {"sess": None, "resolved": False, "path": ""}


def _onnx_session():
    """懒加载单例（进程级）；**仅显式 env** 指路；缺位/异常=None（fail-open v1）。"""
    if _ONNX_STATE["resolved"]:
        return _ONNX_STATE["sess"]
    _ONNX_STATE["resolved"] = True
    path = os.environ.get(ECAPA_MODEL_ENV, "").strip()
    if not path or not os.path.isfile(path):
        return None
    try:
        import onnxruntime as ort

        _ONNX_STATE["sess"] = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        _ONNX_STATE["path"] = path
        print(f"SPEAKER_LOCK_EMBEDDER onnx src={path}", flush=True)
    except Exception as exc:  # noqa: BLE001 - 模型缺位/坏档=回 v1,绝不炸会话
        print(f"SPEAKER_LOCK_EMBEDDER onnx_unavailable {exc!r} -> v1 mel-stat", flush=True)
        _ONNX_STATE["sess"] = None
    return _ONNX_STATE["sess"]


def _reset_onnx_state() -> None:
    """测试钩子：清单例（monkeypatch 改 env 后重解析）。"""
    _ONNX_STATE.update(sess=None, resolved=False, path="")


def embed_pcm_onnx(pcm: bytes, sr: int = 16000) -> np.ndarray:
    """v2 嵌入：raw wav 整段进 ONNX（特征面在图内闭合）→ 192 维 L2 归一。"""
    sess = _onnx_session()
    if sess is None:
        raise ValueError("speaker_lock: onnx model unavailable")
    n = len(pcm) // 2
    wav = np.frombuffer(pcm, dtype="<i2", count=n).astype(np.float32) / 32768.0
    if len(wav) < sr // 2:
        raise ValueError(f"speaker_lock: pcm too short for ecapa ({len(wav)} samples)")
    emb = sess.run(None, {"wav": wav[None, :]})[0].ravel().astype(np.float64)
    norm = float(np.linalg.norm(emb))
    if norm <= 0.0:
        raise ValueError("speaker_lock: degenerate ecapa embedding")
    return emb / norm


class SpeakerLock:
    """1v1 声纹锁：:meth:`enroll` 登记通话对象，:meth:`admit` 逐 VAD 段把门。

    嵌入器双档：**v2 ECAPA ONNX**（模型在场时自动启用,阈值 0.62/0.40——真分人：
    异人 max 0.57 vs 同人 ≥0.78）优先；缺位回 **v1 mel 统计**（0.78/0.65,只分
    语音/非语音）。enrollment=**running mean**（前 3 个确证段滚动平均,抬同人
    稳定性;v1 单段语义不变——首段即锚,后续段继续精化）。

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
        drop_sim: float | None = None,
        relock_sim: float | None = None,
        relock_streak: int = DEFAULT_RELOCK_STREAK,
        sample_rate: int = 16000,
    ) -> None:
        self._use_onnx = _onnx_session() is not None
        self.drop_sim = float(
            drop_sim if drop_sim is not None else (ECAPA_DROP_SIM if self._use_onnx else DEFAULT_DROP_SIM)
        )
        self.relock_sim = float(
            relock_sim if relock_sim is not None else (ECAPA_RELOCK_SIM if self._use_onnx else DEFAULT_RELOCK_SIM)
        )
        self.relock_streak = max(1, int(relock_streak))
        self.sample_rate = int(sample_rate)
        self._centroid_sum: np.ndarray | None = None
        self._centroid_n = 0
        self._centroid: np.ndarray | None = None
        self._severe_streak = 0
        # 观测位（打点用，admit 每次嵌入时刷新；relock 触发时刻的连击数）。
        self.last_sim: float | None = None
        self.last_relock_streak = 0

    @property
    def enrolled(self) -> bool:
        return self._centroid is not None

    def enroll(self, pcm: bytes) -> bool:
        """确证语音段登记（<1s 或不可嵌入拒绝；返回是否成功）。

        running mean（v2 认人票,2026-10-07）：前 ``ENROLL_MAX_SAMPLES``(3) 个确证段
        滚动平均成锚（TTS 语料标定:单段锚同人下界 0.78,多段平均抬同人间隙）;
        v1 行为不变——首段即锚(平均=首段),后续段只是精化同向。"""
        if len(pcm) < 2 * int(ENROLL_MIN_SECONDS * self.sample_rate):
            return False
        vec = self._embed(pcm)
        if vec is None:
            return False
        if self._centroid_sum is None:
            self._centroid_sum = vec.copy()
            self._centroid_n = 1
        elif self._centroid_n < ENROLL_MAX_SAMPLES:
            self._centroid_sum = self._centroid_sum + vec
            self._centroid_n += 1
        mean = self._centroid_sum / float(self._centroid_n)
        norm = float(np.linalg.norm(mean))
        if norm <= 0.0:
            return False
        self._centroid = mean / norm
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
        self.last_sim = sim
        if sim >= self.drop_sim:
            self._severe_streak = 0
            return (True, "hit")
        if sim < self.relock_sim:
            self._severe_streak += 1
            if self._severe_streak >= self.relock_streak:
                self.last_relock_streak = self._severe_streak
                # 重锁=触发段即新锚（重置 running mean 账本）
                self._centroid_sum = vec.copy()
                self._centroid_n = 1
                self._centroid = vec
                self._severe_streak = 0
                return (True, "relock")
        return (False, "drop")

    def _embed_raw(self, pcm: bytes) -> np.ndarray:
        """原始嵌入（可抛 ValueError）：v2 ECAPA 在场优先，否则 v1 mel 统计。"""
        if self._use_onnx:
            return embed_pcm_onnx(pcm, self.sample_rate)
        return embed_pcm(pcm, self.sample_rate)

    def _embed(self, pcm: bytes) -> np.ndarray | None:
        """嵌入失败（段太短/全静音/模型缺位）返回 None；测试可子类覆盖钉状态机。"""
        try:
            return self._embed_raw(pcm)
        except ValueError:
            return None


def gate_enabled() -> bool:
    """总闸公开面（与 :func:`_gate_enabled` 同一读取点；装配层打点用）。"""
    return _gate_enabled()


def has_enroll_text(text: str, min_chars: int = 4) -> bool:
    """登记文本门：实词字符（字母/数字，CJK 象形文字 ``isalnum`` 亦真）≥ min_chars。

    防键盘/环境噪声的 ASR 幻听短碎片（「嗯。」「哒。」）把锁锚到噪声上——首段
    要登记，必须真说了话。标点/空白不计。
    """
    n = 0
    for ch in text or "":
        if ch.isalnum():
            n += 1
            if n >= min_chars:
                return True
    return False


# 早期判定窗（墙钟秒，含 VAD 前导 padding）：段首攒够即做**一次** admit 判定。
# 1.5s 的取法=扣除 silero 前导 ~0.5-0.6s 后净语音 ~0.9s；短于窗的段（「嗯」类
# backchannel）结构性不判=放行。**前缀只裁两档**（2026-10-07 probe_storm_expiry
# 实弹勘误：标定用整段(同人+0dB ≥0.859)、门判前缀方差更大——同人前缀实测可落
# 0.75 灰区误杀真客户）：<relock_sim 清弃（白噪/键盘形）早丢省成本；灰区**不早丢**
# （继续喂 ASR），段末**整段复核**（与标定同形状）裁决，复核判丢才吞 FINAL。
GATE_WINDOW_S = 1.5


class SegmentSpeakerGate:
    """Per-VAD-段早期声纹门（豆包/本地两 ASR 车道共用接线件，2026-10-07 W5 接线波）。

    车道侧三调用点契约::

        START_OF_SPEECH:  gate.segment_start()
        每块段内 PCM:      ok = gate.feed(pcm)    # False=清弃(<relock_sim)：丢这块且
                                                  # 本段剩余恒 False（调用方不喂 ASR）
        END_OF_SPEECH 后:  ok = gate.segment_end(text)  # 灰区段在此整段复核：
                                                  # False=复核判丢，调用方吞 FINAL；
                                                  # 正常段=登记钩（首段确证文本）；
                                                  # 被丢段传 ""（只清段态）

    行为铁律：
    - ``lock`` 为 None 或总闸关 → ``feed`` 恒 True、零累积零 DSP（缺省档字节零漂移）；
    - 前缀判定**一次性**：段首攒够 ``window_s`` 即 admit 一次——同段不重判（VAD 段=
      单人连续话流）；前缀灰区(relock≤sim<drop)=不早丢，段末整段复核裁决；
    - 未登记段恒放行（SpeakerLock.admit 语义）；登记只发生在**段末**且要求
      ``has_enroll_text(text)``（首段真说话才算「确证语音」，防幻听锚噪声）；
    - 打点单源：DROP/RELOCK/ENROLL 三行全从这里出，车道不再各自 print。
    """

    def __init__(
        self,
        lock: SpeakerLock | None,
        *,
        window_s: float = GATE_WINDOW_S,
        sample_rate: int = 16000,
    ) -> None:
        self.lock = lock
        self.window_s = float(window_s)
        self.sample_rate = int(sample_rate)
        self._window_bytes = max(1, int(self.window_s * self.sample_rate) * 2)
        self._buf = bytearray()
        self._dropped = False
        self._verdict = ""
        self._grey = False  # 前缀落灰区(relock≤sim<drop)=不早丢,段末整段复核
        self._off = True  # segment_start 按总闸实况翻（恒守 admit 的 env 语义）

    @property
    def dropped(self) -> bool:
        """本段是否已判丢（END 分支据此整段抑制：不发 EOS/FINAL、关会话、reset）。"""
        return self._dropped

    def segment_start(self) -> None:
        """VAD START：清段态并按总闸实况决定本段是否把门。"""
        self._buf.clear()
        self._dropped = False
        self._verdict = ""
        self._grey = False
        self._off = self.lock is None or not _gate_enabled()

    def feed(self, pcm: bytes) -> bool:
        """段内音频块把门；False=判非通话对象（本段后续恒 False，调用方丢块）。

        前缀判定的两档（2026-10-07 probe_storm_expiry 实弹勘误:标定用**整段**
        (同人+0dB 底噪 ≥0.859),门判**前缀**(净语音 ~0.9s)方差更大——同人前缀
        可落到 0.75 灰区=误杀真客户):**清弃**(<relock_sim,白噪/键盘形)才早丢
        省成本;**灰区不早丢**(继续喂 ASR),段末整段复核(与标定同形状)由
        :meth:`segment_end` 裁决。"""
        if self._dropped:
            return False
        if self._off:
            return True
        self._buf.extend(pcm)
        if self._verdict or self._grey or len(self._buf) < self._window_bytes:
            return True  # 已判放行 / 灰区待段末 / 未攒够判定窗
        ok, reason = self.lock.admit(bytes(self._buf))
        self._verdict = reason
        if not ok:
            _sim = self.lock.last_sim if self.lock.last_sim is not None else 0.0
            if _sim >= self.lock.relock_sim:
                self._grey = True  # 灰区:证据不足不早丢——段末整段复核裁决
                return True
            self._dropped = True
            print(
                speaker_lock_drop_line(_sim, len(self._buf) / (2.0 * self.sample_rate)),
                flush=True,
            )
        elif reason == "relock":
            print(speaker_relock_line(self.lock.last_relock_streak), flush=True)
        return ok

    def segment_end(self, text: str) -> bool:
        """段末收口，返回 **FINAL 是否放行**（False=复核判丢，调用方吞 FINAL）。

        三件事：①灰区段整段复核（前缀方差大，整段才是标定形状——复核 sim 仍
        <drop_sim 才真丢，此时 ASR 已喂过但 FINAL 吞掉=幻听轮照样不成）；②正
        常段吃登记钩（首个确证文本段）；③清段态。"""
        try:
            if self._off or self.lock is None:
                return True
            if self._dropped:
                return False
            if self._grey:
                ok, _reason = self.lock.admit(bytes(self._buf))
                if not ok:
                    self._dropped = True
                    print(
                        speaker_lock_drop_line(
                            self.lock.last_sim if self.lock.last_sim is not None else 0.0,
                            len(self._buf) / (2.0 * self.sample_rate),
                        )
                        + " src=segment_end",
                        flush=True,
                    )
                    return False
            if not self.lock.enrolled and _gate_enabled() and has_enroll_text(text):
                dur_s = len(self._buf) / (2.0 * self.sample_rate)
                if dur_s >= ENROLL_MIN_SECONDS and self.lock.enroll(bytes(self._buf)):
                    print(f"SPEAKER_LOCK_ENROLL dur={dur_s:.1f}s chars={len(text)}", flush=True)
            return True
        finally:
            self._buf.clear()
            self._dropped = False
            self._verdict = ""
            self._grey = False
