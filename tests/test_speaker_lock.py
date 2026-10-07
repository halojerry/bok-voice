"""W5 声纹锁（2026-10-06 demo-quality-wave）单测——全离线合成素材，零网络零模型文件。

覆盖六块：
1. embed_pcm 基本性质：48 维（2×24 带）、L2 归一、确定性（同 PCM 两次嵌入逐位一致）、
   短段/空段/全静音段 ValueError 拒绝；cosine 零向量守卫；
2. 门状态机（合成素材）：同人段 hit、异嗓音/键盘噪声/白噪 drop、enroll 前 admit=
   待登记（"enroll"）、短段拒绝 enroll、重锁路径（连续严重低相似 → relock →
   新锚生效）、hit 重置连击；
3. 灰区语义（stub 嵌入精确钉）：[relock_sim, drop_sim) 灰区=丢但**不计入**重锁连击
   且**不清零**（证据不够强不重锚，防噪声串偷锚）——序列 [严重, 灰, 严重, 严重]
   必须在第 4 段 relock（清零语义会在第 4 段仍未触发，本测试可区分两语义）；
4. 极端构造参数不崩（drop_sim=1.0/relock_sim=0.99/streak=1 逐段重锁；
   drop_sim=-1 全放行；relock_streak=0 钳 1）；
5. 信道稳健性：同人段理想低通到 4kHz（蓝牙/窄带信道模拟）仍 hit——换设备不误杀；
6. 总闸 env 面：缺省关=全放行零行为；"1"/"TRUE"/"yes" 开、"0"/"off"/"false" 关；
   源级 pin：模块 env 读取面恒等于 {BOK_SPEAKER_LOCK}（drop/relock 子键**故意
   不读 env**——test_forward_env 扫描面内未注册键即红，调参=构造参数，见模块
   docstring）。

合成素材标定（2026-10-07，embed 逐帧带均值归零后实测；素材=谐波栈+共振峰高斯+
音节包络+气息噪声，同人=同 f0/共振峰族不同文本节奏）：
  同人异文本 0.844-0.977 | 异嗓音(f0 130→220/共振峰位移) 0.026-0.136 |
  键盘 click 串 0.046-0.128 | 白噪 -0.32..-0.29 | 同人低通 4k 0.830-0.932。
  ⚠ 真语音勘误（同日 probe_speaker_lock）：合成素材的异人分离度不迁移——真嗓音
  异人 0.816-0.973 与同人带噪重叠（v1 只分语音/非语音，不分人）；缺省阈值已按
  真语音分布重标 0.78/0.65（纯 office 底噪 ≤0.70 全判丢、合成同人 ≥0.83 仍全
  放行——本文件全部合成分布断言在新阈值下依旧成立，这就是下面的回归面）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.speaker_lock import (  # noqa: E402
    DEFAULT_DROP_SIM,
    DEFAULT_RELOCK_SIM,
    DEFAULT_RELOCK_STREAK,
    ENROLL_MIN_SECONDS,
    GATE_WINDOW_S,
    SPEAKER_LOCK_ENV,
    SegmentSpeakerGate,
    SpeakerLock,
    cosine,
    embed_pcm,
    has_enroll_text,
    speaker_lock_drop_line,
    speaker_relock_line,
)

SR = 16000
_MODULE_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "speaker_lock.py"
).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- 合成素材（确定性，种子化）

def _pcm_bytes(sig: np.ndarray) -> bytes:
    peak = float(np.abs(sig).max())
    scaled = sig / max(peak, 1e-9) * 0.5 * 32767.0
    return scaled.astype("<i2").tobytes()


def _formant_gain(freqs: np.ndarray, formants: list[tuple[float, float, float]]) -> np.ndarray:
    g = np.zeros_like(freqs)
    for fc, bw, amp in formants:
        g += amp * np.exp(-((freqs - fc) ** 2) / (2.0 * bw**2))
    return g


def synth_voice(f0: float, formants: list[tuple[float, float, float]], dur_s: float, seed: int) -> bytes:
    """同一「说话人」：谐波栈（基频+整次谐波）× 共振峰高斯 × 音节包络 × 气息噪声。

    不同 seed=不同文本节奏（音节布局/微抖动/共振峰 ±10% 游移），声纹不变。
    """
    rng = np.random.default_rng(seed)
    n = int(dur_s * SR)
    t = np.arange(n) / SR
    f0_t = f0 * (
        1.0
        + 0.02 * np.sin(2 * np.pi * 4.2 * t + rng.uniform(0, 6.283))
        + 0.01 * rng.standard_normal(n).cumsum() / np.sqrt(n)
    )
    phase = 2 * np.pi * np.cumsum(f0_t) / SR
    jit = 1.0 + 0.05 * rng.standard_normal(len(formants))
    fms = [(fc * j, bw, amp) for (fc, bw, amp), j in zip(formants, jit)]
    sig = np.zeros(n)
    k = 1
    while k * f0 < SR / 2:
        amp = _formant_gain(np.array([k * f0], dtype=np.float64), fms)[0] / (k**0.5)
        sig += amp * np.sin(k * phase)
        k += 1
    sig += 0.02 * rng.standard_normal(n)  # 气息噪声底
    env = np.zeros(n)
    for _ in range(int(rng.integers(3, 6))):
        c = rng.uniform(0.1, 0.9) * n
        w = rng.uniform(0.08, 0.22) * SR
        x = (np.arange(n) - c) / (w / 2)
        env += np.where(np.abs(x) < 1, 0.5 * (1 + np.cos(np.pi * x)), 0.0)
    return _pcm_bytes(sig * (0.25 + 0.75 * np.clip(env, 0.0, 1.0)))


def synth_keyboard(dur_s: float, seed: int) -> bytes:
    """键盘噪声：短促宽带 click 串（2.5kHz 频段成形的突发 + 指数衰减 + 随机间隙）。"""
    rng = np.random.default_rng(seed)
    n = int(dur_s * SR)
    sig = np.zeros(n)
    pos = rng.uniform(0.0, 0.05) * SR
    while pos < n - 400:
        clen = int(rng.uniform(0.003, 0.008) * SR)
        burst = rng.standard_normal(clen)
        freqs = np.fft.rfftfreq(clen, 1 / SR)
        shape = np.exp(-((freqs - 2500.0) ** 2) / (2 * 1500.0**2)) + 0.3
        burst = np.fft.irfft(np.fft.rfft(burst) * shape, clen)
        sig[int(pos) : int(pos) + clen] += burst * np.exp(-np.linspace(0, 6, clen))
        pos += clen + rng.uniform(0.02, 0.18) * SR
    return _pcm_bytes(sig + 0.002 * rng.standard_normal(n))


def synth_white_noise(dur_s: float, seed: int) -> bytes:
    return _pcm_bytes(np.random.default_rng(seed).standard_normal(int(dur_s * SR)))


def lowpass_pcm(pcm: bytes, cutoff_hz: float) -> bytes:
    """理想低通（蓝牙/窄带信道模拟：频带截断不改低频谱形）。"""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    X = np.fft.rfft(x)
    X[np.fft.rfftfreq(len(x), 1 / SR) > cutoff_hz] = 0.0
    return _pcm_bytes(np.fft.irfft(X, len(x)))


# 「说话人 A」=男声域（f0 130Hz，共振峰 600/1400/2600）；「说话人 B」=女声域
# （f0 220Hz，共振峰 350/1900/3000）——声纹参数实质不同（基频+共振峰结构双异）。
_FM_A = [(600.0, 120.0, 1.0), (1400.0, 150.0, 0.8), (2600.0, 220.0, 0.5)]
_FM_B = [(350.0, 100.0, 1.0), (1900.0, 180.0, 0.9), (3000.0, 250.0, 0.6)]
_A_SEGS = [synth_voice(130.0, _FM_A, 1.6, 100 + i) for i in range(5)]
_B_SEGS = [synth_voice(220.0, _FM_B, 1.6, 200 + i) for i in range(5)]
_KB_SEGS = [synth_keyboard(1.4, 300 + i) for i in range(4)]
_WN_SEGS = [synth_white_noise(1.4, 400 + i) for i in range(3)]


class _StubEmbedLock(SpeakerLock):
    """状态机精确钉：admit 按队列返回预定向量（enroll 仍走真嵌入）。"""

    def __init__(self, admit_vectors, **kw):
        super().__init__(**kw)
        self._admit_vectors = list(admit_vectors)

    def _embed(self, pcm):
        return self._admit_vectors.pop(0)


def _vector_at_cos(target: np.ndarray, cos_val: float, seed: int) -> np.ndarray:
    """构造与 target 余弦恰为 cos_val 的单位向量（正交补随机方向）。"""
    rng = np.random.default_rng(seed)
    u = rng.standard_normal(target.shape[0])
    u -= float(np.dot(u, target)) * target
    u /= float(np.linalg.norm(u))
    v = cos_val * target + np.sqrt(max(0.0, 1.0 - cos_val**2)) * u
    return v / float(np.linalg.norm(v))


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setenv(SPEAKER_LOCK_ENV, "1")


@pytest.fixture()
def gate_off(monkeypatch):
    monkeypatch.delenv(SPEAKER_LOCK_ENV, raising=False)


# ---------------------------------------------------------------- 1. 嵌入基本性质

def test_embed_shape_norm_deterministic():
    vec = embed_pcm(_A_SEGS[0], SR)
    assert vec.shape == (48,)  # 2×24 带（均值+标准差）
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-9
    assert np.array_equal(vec, embed_pcm(_A_SEGS[0], SR))  # 确定性


def test_embed_rejects_short_empty_silent():
    with pytest.raises(ValueError):
        embed_pcm(b"", SR)
    with pytest.raises(ValueError):
        embed_pcm(b"\x00\x01" * 200, SR)  # 100 samples ≈ 6ms < 5 帧
    with pytest.raises(ValueError):
        embed_pcm(b"\x00\x00" * SR, SR)  # 1s 数字零=全静音


def test_cosine_guards_zero_vector():
    a = embed_pcm(_A_SEGS[0], SR)
    assert cosine(a, a) == pytest.approx(1.0)
    assert cosine(a, np.zeros_like(a)) == 0.0
    assert cosine(np.zeros_like(a), a) == 0.0


def test_default_constants_pin():
    """缺省阈值单源钉死（0.78/0.65/3=2026-10-07 真语音重标值，改动须过全部素材
    断言+probe_speaker_lock PASS——见模块 docstring 勘误段）。"""
    assert DEFAULT_DROP_SIM == 0.78
    assert DEFAULT_RELOCK_SIM == 0.65
    assert DEFAULT_RELOCK_STREAK == 3
    assert ENROLL_MIN_SECONDS == 1.0


# ---------------------------------------------------------------- 2. 门状态机（合成素材）

def test_enroll_rejects_short_segment(gate_on):
    lock = SpeakerLock()
    assert lock.enrolled is False
    assert lock.enroll(_A_SEGS[0][: SR]) is False  # 0.5s < 1s 下限
    assert lock.enrolled is False
    assert lock.enroll(_A_SEGS[0]) is True  # 1.6s
    assert lock.enrolled is True


def test_admit_before_enrollment_pending(gate_on):
    lock = SpeakerLock()
    assert lock.admit(_A_SEGS[1]) == (True, "enroll")  # 未登记=放行待登记
    assert lock.enrolled is False  # 模块不自己判定「确证」


def test_same_speaker_segments_hit(gate_on):
    lock = SpeakerLock()
    assert lock.enroll(_A_SEGS[0]) is True
    for seg in _A_SEGS[1:]:
        assert lock.admit(seg) == (True, "hit")


def _classify_lock() -> SpeakerLock:
    """分类面专用锁：relock_streak 抬到 99 隔离「连击重锁」支路（连击→relock
    语义由 test_relock_new_anchor_takes_effect/test_hit_resets_relock_streak 钉），
    纯钉「异嗓音/噪声逐段判 drop」的分类正确性。"""
    return SpeakerLock(relock_streak=99)


def test_other_voice_dropped(gate_on):
    lock = _classify_lock()
    lock.enroll(_A_SEGS[0])
    for seg in _B_SEGS:
        assert lock.admit(seg) == (False, "drop")


def test_keyboard_noise_dropped(gate_on):
    lock = _classify_lock()
    lock.enroll(_A_SEGS[0])
    for seg in _KB_SEGS:
        assert lock.admit(seg) == (False, "drop")


def test_white_noise_dropped(gate_on):
    lock = _classify_lock()
    lock.enroll(_A_SEGS[0])
    for seg in _WN_SEGS:
        assert lock.admit(seg) == (False, "drop")


def test_channel_shifted_same_voice_still_hit(gate_on):
    """蓝牙/窄带信道模拟：同人段低通到 4kHz 仍 hit（换设备不误杀）。"""
    lock = SpeakerLock()
    lock.enroll(_A_SEGS[0])
    for seg in _A_SEGS[1:4]:
        assert lock.admit(lowpass_pcm(seg, 4000.0)) == (True, "hit")


def test_relock_new_anchor_takes_effect(gate_on):
    """连续 3 段严重低相似 → 第 3 段 relock 放行并以触发段为新锚。"""
    lock = SpeakerLock()
    lock.enroll(_A_SEGS[0])
    assert lock.admit(_B_SEGS[0]) == (False, "drop")
    assert lock.admit(_B_SEGS[1]) == (False, "drop")
    assert lock.admit(_B_SEGS[2]) == (True, "relock")
    assert lock.admit(_B_SEGS[3]) == (True, "hit")  # 新锚=异嗓音 B：B 后续段命中
    assert lock.admit(_A_SEGS[1]) == (False, "drop")  # 原说话人 A 反被丢（重锚语义）


def test_hit_resets_relock_streak(gate_on):
    """hit 清零连击：B B A(hit) B B 不触发 relock。"""
    lock = SpeakerLock()
    lock.enroll(_A_SEGS[0])
    assert lock.admit(_B_SEGS[0]) == (False, "drop")
    assert lock.admit(_B_SEGS[1]) == (False, "drop")
    assert lock.admit(_A_SEGS[1]) == (True, "hit")
    assert lock.admit(_B_SEGS[2]) == (False, "drop")
    assert lock.admit(_B_SEGS[3]) == (False, "drop")


# ---------------------------------------------------------------- 3. 灰区语义（stub 精确钉）

def test_gray_zone_preserves_but_not_counts_streak(gate_on):
    """[relock, drop) 灰区=丢、连击不清零也不计入：[严重,灰,严重,严重] 第 4 段 relock。"""
    anchor = embed_pcm(_A_SEGS[0], SR)
    seq = [
        _vector_at_cos(anchor, 0.10, seed=11),  # 严重（<0.65）→ 丢，连击 1
        _vector_at_cos(anchor, 0.70, seed=12),  # 灰区（0.65≤sim<0.78）→ 丢，连击仍 1
        _vector_at_cos(anchor, 0.10, seed=13),  # 严重 → 丢，连击 2
        _vector_at_cos(anchor, 0.10, seed=14),  # 严重 → 连击 3 ≥ 3 → relock
    ]
    lock = _StubEmbedLock(seq, relock_streak=3)
    lock.enroll(_A_SEGS[0])
    assert lock.admit(b"x") == (False, "drop")
    assert lock.admit(b"x") == (False, "drop")
    assert lock.admit(b"x") == (False, "drop")
    assert lock.admit(b"x") == (True, "relock")


def test_gray_zone_drop_is_loss_not_pass(gate_on):
    """灰区段必须丢（不喂 ASR）——保守语义的另一面：不确定 ≠ 放行。"""
    anchor = embed_pcm(_A_SEGS[0], SR)
    lock = _StubEmbedLock([_vector_at_cos(anchor, 0.72, seed=21)], relock_streak=3)
    lock.enroll(_A_SEGS[0])
    assert lock.admit(b"x") == (False, "drop")


def test_relock_enrolls_trigger_segment(gate_on):
    """relock 新锚=触发段本身（随后同向量必然 hit）。"""
    anchor = embed_pcm(_A_SEGS[0], SR)
    v = _vector_at_cos(anchor, 0.05, seed=31)
    lock = _StubEmbedLock([v, v], relock_streak=1)
    lock.enroll(_A_SEGS[0])
    assert lock.admit(b"x") == (True, "relock")
    assert lock.admit(b"x") == (True, "hit")  # 与新锚逐位同向量 → cos=1


# ---------------------------------------------------------------- 4. 极端构造参数不崩

def test_extreme_thresholds_no_crash(gate_on):
    lock = SpeakerLock(drop_sim=1.0, relock_sim=0.99, relock_streak=1)
    lock.enroll(_A_SEGS[0])
    assert lock.admit(_A_SEGS[1]) == (True, "relock")  # 0.99 以下全重锁
    assert lock.admit(_A_SEGS[1]) == (True, "hit")  # 新锚后同素材命中

    lock2 = SpeakerLock(drop_sim=-1.0)  # cosine ≥ -1 → 全放行
    lock2.enroll(_A_SEGS[0])
    assert lock2.admit(_B_SEGS[0]) == (True, "hit")

    lock3 = SpeakerLock(relock_streak=0)
    assert lock3.relock_streak == 1  # 钳 1（防 0 除/永不触发）


def test_unembeddable_segment_fail_open(gate_on):
    """太短不可嵌入的段 fail-open 放行（强烈偏向不误杀）。"""
    lock = SpeakerLock()
    lock.enroll(_A_SEGS[0])
    assert lock.admit(b"\x00\x01" * 200) == (True, "enroll")


# ---------------------------------------------------------------- 5. 打点行格式

def test_log_line_formats():
    assert speaker_lock_drop_line(0.4321, 1.234) == "SPEAKER_LOCK_DROP sim=0.43 dur=1.2s"
    assert speaker_relock_line(3) == "SPEAKER_RELOCK after=3"


# ---------------------------------------------------------------- 6. 总闸 env 面

def test_gate_off_admits_all_zero_behavior(gate_off):
    """缺省关：登记后异嗓音/噪声照样放行，零声纹判定（零行为）。"""
    lock = SpeakerLock()
    assert lock.enroll(_A_SEGS[0]) is True
    for seg in _B_SEGS + _KB_SEGS + _WN_SEGS:
        assert lock.admit(seg) == (True, "enroll")


@pytest.mark.parametrize("value", ["0", "off", "false", "no", "FALSE", " Off "])
def test_gate_explicit_off_values(monkeypatch, value):
    monkeypatch.setenv(SPEAKER_LOCK_ENV, value)
    lock = SpeakerLock()
    lock.enroll(_A_SEGS[0])
    assert lock.admit(_KB_SEGS[0]) == (True, "enroll")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " ON "])
def test_gate_explicit_on_values(monkeypatch, value):
    monkeypatch.setenv(SPEAKER_LOCK_ENV, value)
    lock = SpeakerLock()
    lock.enroll(_A_SEGS[0])
    assert lock.admit(_KB_SEGS[0]) == (False, "drop")


def test_env_read_surface_single_registered_key():
    """源级 pin：模块 env 读取面恒等于已注册的 {BOK_SPEAKER_LOCK}。

    drop/relock 两子键**故意不读 env**（test_forward_env 扫描面内未注册键即 CI 红），
    调参=SpeakerLock 构造参数。新增 env 读取必须先进 _FORWARD_ENV 并改本测试认账。
    """
    reads = set(re.findall(r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]+)"', _MODULE_SRC))
    reads |= set(re.findall(r"""\.get\(\s*["'](BOK_[A-Z0-9_]+)["']""", _MODULE_SRC))
    assert reads == {SPEAKER_LOCK_ENV}


def test_admit_returns_reason_enum():
    """admit 原因字符串恒在四值枚举内（接线方 match/打点依据）。"""
    lock = SpeakerLock()
    lock.enroll(_A_SEGS[0])
    reasons = {lock.admit(pcm)[1] for pcm in _A_SEGS[1:] + _B_SEGS[:2] + _KB_SEGS[:1]}
    assert reasons <= {"enroll", "hit", "drop", "relock"}


# ---------------------------------------------------------------- 7. SegmentSpeakerGate（接线件）

def _chunks(pcm: bytes, chunk_s: float = 0.2) -> list[bytes]:
    step = max(2, int(chunk_s * SR) * 2)
    return [pcm[i : i + step] for i in range(0, len(pcm), step)]


def test_has_enroll_text_gates_hallucination_fragments():
    assert has_enroll_text("嗯。") is False            # 单语气词：幻听高危碎片
    assert has_enroll_text("") is False
    assert has_enroll_text("   。！？") is False        # 纯标点空白
    assert has_enroll_text("abc") is False              # 3 实词差一个
    assert has_enroll_text("你好我是陈大文") is True
    assert has_enroll_text("打打键盘的声音") is True     # 长幻听照样过（文本门只防短碎片）


def test_segment_gate_none_lock_passthrough():
    gate = SegmentSpeakerGate(None)
    gate.segment_start()
    assert all(gate.feed(c) for c in _chunks(_A_SEGS[0])) is True
    assert gate.dropped is False
    assert len(gate._buf) == 0  # lock=None 零累积
    gate.segment_end("你好世界")  # no-op 不炸


def test_segment_gate_env_off_zero_cost(gate_off):
    """总闸关：feed 恒放行、零累积、段末不登记（缺省档字节零行为）。"""
    gate = SegmentSpeakerGate(SpeakerLock())
    gate.segment_start()
    assert all(gate.feed(c) for c in _chunks(_A_SEGS[0])) is True
    assert len(gate._buf) == 0
    gate.segment_end("你好我是陈大文")
    assert gate.lock.enrolled is False


def test_segment_gate_enroll_on_confirmed_text(gate_on, capsys):
    """首段：窗内放行（未登记），段末凭确证文本登记并打点。"""
    gate = SegmentSpeakerGate(SpeakerLock())
    gate.segment_start()
    assert all(gate.feed(c) for c in _chunks(_A_SEGS[0])) is True
    gate.segment_end("喂你好我是陈大文")
    assert gate.lock.enrolled is True
    assert "SPEAKER_LOCK_ENROLL" in capsys.readouterr().out


def test_segment_gate_enroll_requires_text_and_duration(gate_on):
    gate = SegmentSpeakerGate(SpeakerLock())
    gate.segment_start()
    for c in _chunks(_A_SEGS[0]):
        gate.feed(c)
    gate.segment_end("嗯。")  # 无确证文本：不登记
    assert gate.lock.enrolled is False
    gate.segment_start()
    gate.feed(_A_SEGS[1][: int(0.6 * SR) * 2])  # <1s：时长不够
    gate.segment_end("你好我是陈大文")
    assert gate.lock.enrolled is False


def test_segment_gate_drop_cross_voice_and_recover(gate_on, capsys):
    """登记 A 后：B 段在判定窗处翻转 False、后续恒 False、段末清态；A 段照常。"""
    gate = SegmentSpeakerGate(SpeakerLock())
    gate.segment_start()
    for c in _chunks(_A_SEGS[0]):
        assert gate.feed(c) is True
    gate.segment_end("你好我是陈大文")
    capsys.readouterr()

    gate.segment_start()
    verdicts = [gate.feed(c) for c in _chunks(_B_SEGS[0])]
    assert False in verdicts                       # 窗处翻转
    assert verdicts[-1] is False                   # 翻转后恒 False
    assert gate.dropped is True
    gate.segment_end("")
    assert gate.dropped is False                   # 段末清态
    out = capsys.readouterr().out
    assert "SPEAKER_LOCK_DROP" in out
    assert speaker_lock_drop_line(0.05, 1.5).startswith("SPEAKER_LOCK_DROP")

    gate.segment_start()                           # A 段照常放行（hit）
    assert all(gate.feed(c) for c in _chunks(_A_SEGS[1])) is True


def test_segment_gate_short_segment_never_judged(gate_on):
    """< 判定窗的段（「嗯」类 backchannel）结构性不判=放行（fail-open 不误杀）。"""
    gate = SegmentSpeakerGate(SpeakerLock())
    gate.segment_start()
    for c in _chunks(_A_SEGS[0]):
        gate.feed(c)
    gate.segment_end("你好我是陈大文")
    gate.segment_start()
    assert int(GATE_WINDOW_S * SR) * 2 <= len(_A_SEGS[0])  # 标定段确在窗上（前提自检）
    short_b = _B_SEGS[0][: int(GATE_WINDOW_S * SR) * 2 - 3200]  # 恒差 0.2s 进不了窗
    assert all(gate.feed(c) for c in _chunks(short_b)) is True
    assert gate.dropped is False


def test_segment_gate_judges_once_per_segment(gate_on):
    class _CountingLock(SpeakerLock):
        n_embeds = 0

        def _embed(self, pcm):
            type(self).n_embeds += 1
            return super()._embed(pcm)

    lock = _CountingLock()
    gate = SegmentSpeakerGate(lock)
    gate.segment_start()
    for c in _chunks(_A_SEGS[0]):
        gate.feed(c)
    gate.segment_end("你好我是陈大文")
    gate.segment_start()
    for c in _chunks(_A_SEGS[1]):
        gate.feed(c)
    gate.segment_end("第二句话")
    gate.segment_start()
    for c in _chunks(_B_SEGS[0]):
        gate.feed(c)  # 判丢
    gate.segment_end("")
    assert lock.n_embeds == 2  # 每段恰一次：hit 段 1+drop 段 1（enroll 直调
    # embed_pcm 不经 _embed 钩、首段未登记 admit 在 _embed 前返回——都不计）


def test_segment_gate_relock_after_streak(gate_on, capsys):
    """连续严重不匹配段数到 relock_streak：触发段放行、打点 SPEAKER_RELOCK after=3。"""
    centroid = embed_pcm(_A_SEGS[0])
    vecs = [_vector_at_cos(centroid, 0.05, 900 + i) for i in range(3)]
    lock = _StubEmbedLock(vecs)
    assert lock.enroll(_A_SEGS[0])
    gate = SegmentSpeakerGate(lock)

    for _ in range(2):  # 前两段严重不匹配：drop（streak 1→2，未到 3）
        gate.segment_start()
        verdicts = [gate.feed(c) for c in _chunks(_B_SEGS[0])]
        assert verdicts[-1] is False
        assert gate.dropped is True
        gate.segment_end("")
        assert gate.dropped is False
    out = capsys.readouterr().out
    assert "SPEAKER_LOCK_DROP" in out
    assert "SPEAKER_RELOCK" not in out

    gate.segment_start()  # 第 3 段：触发重锁——触发段即新锚，放行
    verdicts = [gate.feed(c) for c in _chunks(_B_SEGS[0])]
    assert verdicts[-1] is True
    assert gate.dropped is False
    gate.segment_end("")
    out = capsys.readouterr().out
    assert "SPEAKER_RELOCK after=3" in out
    assert "SPEAKER_LOCK_DROP" not in out
    assert lock.enrolled is True


# ---------------------------------------------------------------- 8. 灰区段末整段复核
# （2026-10-07 probe_storm_expiry 实弹勘误：标定用整段、门判前缀——同人前缀可落
# 0.75 灰区误杀真客户；灰区不早丢，段末整段复核裁决，复核判丢才吞 FINAL。）


def test_segment_gate_grey_prefix_not_dropped_early(gate_on, capsys):
    """前缀灰区(0.65≤sim<0.78)：feed 恒放行不早丢、无 DROP 打点；段末复核仍灰
    → segment_end 返回 False（吞 FINAL）+ DROP src=segment_end 打点。"""
    centroid = embed_pcm(_A_SEGS[0])
    lock = _StubEmbedLock([_vector_at_cos(centroid, 0.72, seed=71),
                           _vector_at_cos(centroid, 0.72, seed=72)])
    assert lock.enroll(_A_SEGS[0])
    gate = SegmentSpeakerGate(lock)
    gate.segment_start()
    verdicts = [gate.feed(c) for c in _chunks(_B_SEGS[0])]
    assert all(v is True for v in verdicts), "灰区不早丢"
    assert gate.dropped is False
    assert "SPEAKER_LOCK_DROP" not in capsys.readouterr().out
    ok = gate.segment_end("尾问句子在这里")
    assert ok is False, "整段复核仍灰→吞 FINAL"
    out = capsys.readouterr().out
    assert "SPEAKER_LOCK_DROP" in out and "src=segment_end" in out


def test_segment_gate_grey_recheck_hit_passes_final(gate_on, capsys):
    """前缀灰区但整段复核 hit（真实场景：前缀方差大、整段回到 ≥drop_sim）→
    segment_end 返回 True，FINAL 放行、无 DROP。"""
    centroid = embed_pcm(_A_SEGS[0])
    lock = _StubEmbedLock([_vector_at_cos(centroid, 0.72, seed=81),
                           _vector_at_cos(centroid, 0.95, seed=82)])
    assert lock.enroll(_A_SEGS[0])
    gate = SegmentSpeakerGate(lock)
    gate.segment_start()
    assert all(gate.feed(c) for c in _chunks(_A_SEGS[0]))
    assert gate.segment_end("你好我是陈大文") is True
    assert "SPEAKER_LOCK_DROP" not in capsys.readouterr().out


def test_segment_gate_clear_mismatch_still_early_drop(gate_on, capsys):
    """清弃档(<relock_sim,白噪/键盘形)不变：前缀处即早丢停喂（省成本语义保留）。"""
    centroid = embed_pcm(_A_SEGS[0])
    lock = _StubEmbedLock([_vector_at_cos(centroid, 0.05, seed=91)])
    assert lock.enroll(_A_SEGS[0])
    gate = SegmentSpeakerGate(lock)
    gate.segment_start()
    verdicts = [gate.feed(c) for c in _chunks(_B_SEGS[0])]
    assert verdicts[-1] is False
    assert gate.dropped is True
    assert "SPEAKER_LOCK_DROP" in capsys.readouterr().out
