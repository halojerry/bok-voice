"""离线单测:探针刺激工具（保音高加速 / 窄带 / 短轮整句门 / CSC 增强守卫）。

无真栈、无网络:全部纯函数 + 合成信号。覆盖:
  - ``speedup_pcm`` 保音高（OLA 时域拉伸）:正弦主频不变、时长压缩、factor=1 恒等;
  - ``narrowband_pcm``:7kHz 掉 ≥90% 能量、1kHz 保 ≥80%;
  - 侧车 ``_needs_full_decode`` 阈值（短轮整句强开门);
  - ``prepare_csc_data`` 信道增强:数字冻结逐字节不变、粤语特征字永不删、
    固定种子确定性。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from random import Random

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SIDECAR = ROOT / "services" / "qwen3-asr-sidecar"
for _p in (str(SCRIPTS), str(SIDECAR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from probe_fast_speech import speedup_pcm  # noqa: E402
from probe_stimulus import narrowband_pcm  # noqa: E402
import prepare_csc_data as csc  # noqa: E402


def _load_sidecar_app():
    """从文件路径加载侧车 app（避开通用模块名 ``app`` 与其它测试冲突）。"""
    spec = importlib.util.spec_from_file_location("_bok_sidecar_app", SIDECAR / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bok_sidecar_app"] = mod
    spec.loader.exec_module(mod)
    return mod


def _sine(freq: float, seconds: float, rate: int = 16000, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * 32767.0 * np.sin(2.0 * np.pi * freq * t)).astype(np.int16)


def _dominant_freq(pcm: bytes, rate: int = 16000) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    k = int(np.argmax(spec))
    return k * rate / x.size


def _energy(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sum(x * x))


# ---------------------------------------------------------------------------
# 1. speedup_pcm —— 保音高时长压缩
# ---------------------------------------------------------------------------

def test_speedup_preserves_pitch_and_compresses_duration():
    x = _sine(440.0, 2.0)
    y = speedup_pcm(x.tobytes(), 1.4)
    yy = np.frombuffer(y, dtype=np.int16)
    dur_in = x.size / 16000.0
    dur_out = yy.size / 16000.0
    f_in = _dominant_freq(x.tobytes())
    f_out = _dominant_freq(y)
    print(f"\n[speedup] in f={f_in:.1f}Hz dur={dur_in:.3f}s | "
          f"out f={f_out:.1f}Hz dur={dur_out:.3f}s (ratio={dur_out / dur_in:.3f})")
    assert abs(f_in - 440.0) / 440.0 < 0.05
    # 主频必须仍是 440（旧 audioop 版会整体上移到 ~616Hz = 440*1.4）
    assert abs(f_out - 440.0) / 440.0 < 0.05, f"pitch shifted: {f_out:.1f}Hz"
    expected = dur_in / 1.4
    assert abs(dur_out - expected) / expected < 0.10, f"duration {dur_out:.3f} vs {expected:.3f}"


def test_speedup_factor_one_is_passthrough():
    x = _sine(440.0, 1.0)
    y = speedup_pcm(x.tobytes(), 1.0)
    yy = np.frombuffer(y, dtype=np.int16)
    n = min(yy.size, x.size)
    max_err = int(np.max(np.abs(yy[:n].astype(np.int32) - x[:n].astype(np.int32)))) if n else 0
    print(f"\n[speedup] factor=1 len in={x.size} out={yy.size} max_err={max_err}")
    assert abs(yy.size - x.size) / x.size < 0.05
    assert max_err <= 1


# ---------------------------------------------------------------------------
# 2. narrowband_pcm —— 8k 电话窄带
# ---------------------------------------------------------------------------

def test_narrowband_rejects_high_and_keeps_low_freq():
    hi = _sine(7000.0, 0.5).tobytes()
    lo = _sine(1000.0, 0.5).tobytes()
    hi2 = narrowband_pcm(hi)
    lo2 = narrowband_pcm(lo)
    r_hi = _energy(hi2) / _energy(hi)
    r_lo = _energy(lo2) / _energy(lo)
    print(f"\n[narrowband] 7kHz energy ratio={r_hi:.4f} | 1kHz energy ratio={r_lo:.4f}")
    assert r_hi <= 0.10, f"7kHz not rejected: ratio={r_hi:.4f}"
    assert r_lo >= 0.80, f"1kHz not preserved: ratio={r_lo:.4f}"


def test_narrowband_empty_input():
    assert narrowband_pcm(b"") == b""
    odd = _sine(1000.0, 0.01)[:101].tobytes()  # 奇数样本
    assert len(narrowband_pcm(odd)) == len(odd)


# ---------------------------------------------------------------------------
# 3. 侧车 _needs_full_decode —— 短轮整句强开门
# ---------------------------------------------------------------------------

def test_needs_full_decode_threshold():
    mod = _load_sidecar_app()
    assert mod._FULL_DECODE_MAX_S == 2.0
    assert mod._needs_full_decode(1.5) is True
    assert mod._needs_full_decode(2.5) is False
    assert mod._needs_full_decode(2.0) is True  # 边界闭区间
    assert mod._needs_full_decode(0.0) is True


# ---------------------------------------------------------------------------
# 4. prepare_csc_data —— 信道增强守卫
# ---------------------------------------------------------------------------

_DIGIT_SENT = "單號係六四三一一三三"
_MARKER_SENT = "我哋嘅貨件已經登記好咗喇"
_HOMOPHONES = {"yue": {"號": ["号"], "係": ["系"], "單": ["单"], "記": ["记"], "貨": ["货"], "件": ["见"]}}


def test_augment_narrowband_freezes_digit_span():
    rng = Random(7)
    out = csc.augment_narrowband(_DIGIT_SENT, csc.LANE_YUE, rng, _HOMOPHONES)
    assert "六四三一一三三" in out
    assert csc._num_signature(out) == csc._num_signature(_DIGIT_SENT)


def test_augment_speed_freezes_digit_span():
    for seed in range(20):
        out = csc.augment_speed(_DIGIT_SENT, csc.LANE_YUE, Random(seed))
        assert "六四三一一三三" in out, f"digit span damaged at seed={seed}: {out!r}"
        assert csc._num_signature(out) == csc._num_signature(_DIGIT_SENT)


def test_augment_speed_never_removes_cantonese_markers():
    base = csc._count_markers(_MARKER_SENT)
    assert base >= 3  # 句子确实含多个特征字（嘅/哋/咗）
    for seed in range(30):
        out = csc.augment_speed(_MARKER_SENT, csc.LANE_YUE, Random(seed))
        assert csc._count_markers(out) >= base, f"marker dropped at seed={seed}: {out!r}"
        assert csc._num_signature(out) == csc._num_signature(_MARKER_SENT)


def test_augmentation_deterministic_across_two_seeded_runs():
    for seed in (1, 123, 9999):
        nb1 = csc.augment_narrowband(_DIGIT_SENT, csc.LANE_YUE, Random(seed), _HOMOPHONES)
        nb2 = csc.augment_narrowband(_DIGIT_SENT, csc.LANE_YUE, Random(seed), _HOMOPHONES)
        sp1 = csc.augment_speed(_MARKER_SENT, csc.LANE_YUE, Random(seed))
        sp2 = csc.augment_speed(_MARKER_SENT, csc.LANE_YUE, Random(seed))
        assert nb1 == nb2, f"narrowband nondeterministic at seed={seed}"
        assert sp1 == sp2, f"speed nondeterministic at seed={seed}"
        # 目标恒为原始干净文本（增强只动输入侧）
        assert csc._num_signature(nb1) == csc._num_signature(_DIGIT_SENT)
        assert csc._num_signature(sp1) == csc._num_signature(_MARKER_SENT)


def test_parse_augment_kinds():
    assert csc.parse_augment_kinds("") == []
    assert csc.parse_augment_kinds(None) == []
    assert csc.parse_augment_kinds("narrowband,speed") == ["narrowband", "speed"]
    assert csc.parse_augment_kinds(" Speed , narrowband ") == ["speed", "narrowband"]
    with pytest.raises(ValueError):
        csc.parse_augment_kinds("bogus")


def test_build_dataset_augment_off_vs_on_offline():
    # 默认关：无任何 `+kind` 后缀 origin。
    rows_off, _stats_off = csc.build_dataset(
        count=60, seed=5, use_db=False, use_replay=False, use_char_inject=False
    )
    assert all("+" not in r["origin"] for r in rows_off)
    # speed 不依赖同音数据 → 打开后必产 `inject+speed` 行并出现在审计计数。
    rows_on, stats_on = csc.build_dataset(
        count=60, seed=5, use_db=False, use_replay=False, use_char_inject=False,
        augment_kinds=["speed"],
    )
    n_speed = sum(1 for r in rows_on if r["origin"] == "inject+speed")
    print(f"\n[augment] off rows={len(rows_off)} | on rows={len(rows_on)} inject+speed={n_speed}")
    assert n_speed >= 1
    assert stats_on["augment"]["rows"].get("inject+speed", 0) == n_speed
    assert stats_on["augment"]["kinds"] == ["speed"]
