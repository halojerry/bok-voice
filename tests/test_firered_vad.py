"""FireRedVAD 试点适配层离线单测（2026-09-28，providers/firered_vad.py）。

覆盖：①参数映射纯函数；②kaldi cmvn.ark 手写解析（对合成二进制往返 + 真资产）；
③状态机转移（onset/offset 确认）；④pre-roll 长度（_SpeechBuffer 游标语义）；
⑤装配 fail-safe（缺模型抛清晰异常）+ kill-switch 默认零变化（源码级 pin）；
⑥真模型在场的 ONNX 前端 smoke（skipif）。

全部离线可跑：③④用替身 fbank/模型驱动**真** FireRedVADStream 事件链（不碰 knf/ort）；
只有装配与 smoke 腿需要真模型/依赖，缺则 skip。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import numpy as np
import pytest

from agent_runtime.providers import firered_vad as fv

_ROOT = Path(__file__).resolve().parents[1]
_ASSET_DIR = fv._DEFAULT_ASSET_DIR
_CMVN_PRESENT = (_ASSET_DIR / fv._CMVN_FILENAME).is_file()
_MODEL_PRESENT = (_ASSET_DIR / fv._MODEL_FILENAME).is_file()


def _deps_present() -> bool:
    if not (_CMVN_PRESENT and _MODEL_PRESENT):
        return False
    try:
        import kaldi_native_fbank  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


_DEPS_PRESENT = _deps_present()


# ---------------------------------------------------------------------------
# ① 参数映射
# ---------------------------------------------------------------------------
def test_duration_to_frames():
    assert fv.duration_to_frames(0.15) == 15
    assert fv.duration_to_frames(0.35) == 35
    assert fv.duration_to_frames(0.5) == 50
    assert fv.duration_to_frames(15.0) == 1500
    # 下界 1（0 或极小值不得成 0 帧）
    assert fv.duration_to_frames(0.0) == 1
    assert fv.duration_to_frames(0.004) == 1


def test_params_defaults_align_repo_baseline():
    p = fv.VADParams.from_durations()
    # min_speech 0.15→15、min_silence 0.35→35（对齐本仓基线，非官方 200ms）
    assert (p.min_speech_frames, p.min_silence_frames) == (15, 35)
    assert p.prefix_padding_frames == 50
    assert p.max_speech_frames == 1500
    assert p.smooth_window == 5
    # activation_threshold 独立档=0.5，不照抄 silero 0.75
    assert p.activation_threshold == 0.5


def test_params_custom_durations_and_smooth():
    p = fv.VADParams.from_durations(
        min_speech_duration=0.2,
        min_silence_duration=0.5,
        prefix_padding_duration=0.3,
        max_buffered_speech=10.0,
        activation_threshold=0.7,
        smooth_window=3,
    )
    assert (p.min_speech_frames, p.min_silence_frames) == (20, 50)
    assert p.prefix_padding_frames == 30
    assert p.max_speech_frames == 1000
    assert p.smooth_window == 3 and p.activation_threshold == 0.7


# ---------------------------------------------------------------------------
# ② cmvn.ark 解析
# ---------------------------------------------------------------------------
def test_read_cmvn_real_asset():
    if not _CMVN_PRESENT:
        pytest.skip("cmvn.ark asset absent")
    mean, istd = fv.read_kaldi_cmvn(_ASSET_DIR / fv._CMVN_FILENAME)
    assert mean.shape == (80,) and istd.shape == (80,)
    assert np.all(np.isfinite(mean)) and np.all(np.isfinite(istd))
    assert np.all(istd > 0)
    # 官方 cmvn 是 kaldi log-fbank 统计，均值应落在合理量级
    assert 0.0 < float(mean.mean()) < 40.0


def test_read_cmvn_kaldi_format_roundtrip(tmp_path):
    # 手写 kaldi 二进制 DM 矩阵（\0B + "DM " + int32(rows/cols 带长度前缀) + float64）
    stats = np.array(
        [[2.0, 4.0, 6.0, 8.0, 3.0], [30.0, 30.0, 30.0, 30.0, 3.0]], dtype=np.float64
    )
    rows, cols = stats.shape
    blob = bytearray(b"\x00B")
    blob += b"DM "
    for n in (rows, cols):
        blob += struct.pack("<b", 4) + struct.pack("<i", n)
    blob += stats.tobytes()
    p = tmp_path / "cmvn.ark"
    p.write_bytes(bytes(blob))

    mean, istd = fv.read_kaldi_cmvn(p)
    count = 3.0
    exp_mean = stats[0, :4] / count
    exp_var = stats[1, :4] / count - exp_mean * exp_mean
    np.testing.assert_allclose(mean, exp_mean, rtol=1e-6)
    np.testing.assert_allclose(istd, 1.0 / np.sqrt(exp_var), rtol=1e-6)


def test_read_cmvn_rejects_garbage(tmp_path):
    p = tmp_path / "bad.ark"
    p.write_bytes(b"not a kaldi matrix at all")
    with pytest.raises(ValueError):
        fv.read_kaldi_cmvn(p)


def test_read_cmvn_variance_floor(tmp_path):
    # var = row1/count - mean² = 0 - 25 → floor → istd = 1/sqrt(1e-20) = 1e10
    stats = np.array([[5.0, 1.0], [0.0, 1.0]], dtype=np.float64)  # dim=1, count=1
    blob = bytearray(b"\x00B")
    blob += b"DM "
    for n in stats.shape:
        blob += struct.pack("<b", 4) + struct.pack("<i", n)
    blob += stats.tobytes()
    p = tmp_path / "floor.ark"
    p.write_bytes(bytes(blob))
    mean, istd = fv.read_kaldi_cmvn(p)
    assert mean[0] == pytest.approx(5.0)
    # var = 0/1 - 25 = -25 → floor → istd = 1/sqrt(1e-20) = 1e10
    assert istd[0] == pytest.approx(1e10)


# ---------------------------------------------------------------------------
# ③ 状态机
# ---------------------------------------------------------------------------
def test_state_machine_onset_confirm():
    p = fv.VADParams.from_durations(smooth_window=1)  # min_speech=15
    v = fv.simulate([0.0] * 10 + [0.9] * 20, p)
    starts = [x.frame_idx for x in v if x.speech_start]
    assert starts == [25]  # 10 静音 + 15 语音确认
    assert not any(x.speech_start for x in v[:24])
    assert v[24].speaking is True


def test_state_machine_offset_confirm():
    p = fv.VADParams.from_durations(smooth_window=1)  # min_silence=35
    v = fv.simulate([0.0] * 10 + [0.9] * 20 + [0.0] * 50, p)
    assert [x.frame_idx for x in v if x.speech_start] == [25]
    assert [x.frame_idx for x in v if x.speech_end] == [65]  # 31 + 35 - 1
    assert v[-1].speaking is False


def test_state_machine_short_speech_no_start():
    p = fv.VADParams.from_durations(smooth_window=1)
    v = fv.simulate([0.0] * 5 + [0.9] * 10 + [0.0] * 5, p)  # 10 < 15
    assert not any(x.speech_start for x in v)
    assert not any(x.speaking for x in v)


def test_state_machine_smoothing_delays_onset():
    p = fv.VADParams.from_durations(smooth_window=5)
    v = fv.simulate([0.0] * 10 + [0.9] * 30, p)
    # 平滑窗 5：语音起后 ramp ~2 帧才过阈，确认 15 帧 → 27
    assert [x.frame_idx for x in v if x.speech_start] == [27]


def test_state_machine_reset_clears_state():
    p = fv.VADParams.from_durations(smooth_window=1)
    sm = fv.FireRedStateMachine(p)
    for _ in range(30):
        sm.process(0.9)
    assert sm.speaking is True
    sm.reset()
    assert sm.speaking is False and sm.state is fv.VADState.SILENCE
    assert sm.process(0.0).frame_idx == 1


# ---------------------------------------------------------------------------
# ④ pre-roll 缓冲
# ---------------------------------------------------------------------------
def test_speech_buffer_preroll_length():
    shift = fv.FRAME_SHIFT_SAMPLE  # 160
    prefix = 50 * shift  # 0.5s
    buf = fv._SpeechBuffer(1500 * shift, prefix)
    for _ in range(60):  # 静音 60 hop
        buf.append(b"\x00\x00" * shift)
    buf.reset_cursor()  # 静音游标回收：只留 prefix
    assert buf.index == prefix
    for _ in range(10):  # 说话累积 10 hop
        buf.append(b"\x01\x00" * shift)
    fr = buf.frame(16000)
    assert fr.samples_per_channel == prefix + 10 * shift


def test_speech_buffer_max_cap():
    shift = fv.FRAME_SHIFT_SAMPLE
    buf = fv._SpeechBuffer(2 * shift, 0)
    for _ in range(5):
        buf.append(b"\x01\x00" * shift)
    assert buf.index == 2 * shift  # 封顶


def test_speech_buffer_clear():
    shift = fv.FRAME_SHIFT_SAMPLE
    buf = fv._SpeechBuffer(10 * shift, 0)
    buf.append(b"\x01\x00" * shift)
    buf.clear()
    assert buf.index == 0


# ---------------------------------------------------------------------------
# ⑤ 装配 fail-safe + kill-switch 默认零变化
# ---------------------------------------------------------------------------
def test_load_firered_missing_model_raises(tmp_path):
    with pytest.raises(FileNotFoundError) as ei:
        fv.load_firered_vad(model_dir=tmp_path)
    assert "FireRedVAD" in str(ei.value)


def test_resolve_model_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BOK_FIRERED_MODEL_DIR", str(tmp_path))
    assert fv._resolve_model_dir(None) == tmp_path
    monkeypatch.delenv("BOK_FIRERED_MODEL_DIR", raising=False)
    assert fv._resolve_model_dir(None) == fv._DEFAULT_ASSET_DIR


def test_env_defaults_are_firered_specific(monkeypatch):
    monkeypatch.delenv("BOK_FIRERED_THRESHOLD", raising=False)
    monkeypatch.delenv("BOK_FIRERED_SMOOTH", raising=False)
    assert fv._env_float("BOK_FIRERED_THRESHOLD", fv._DEFAULT_THRESHOLD) == 0.5
    assert fv._env_int("BOK_FIRERED_SMOOTH", fv._DEFAULT_SMOOTH) == 5
    # 坏值回退默认
    monkeypatch.setenv("BOK_FIRERED_THRESHOLD", "not-a-float")
    assert fv._env_float("BOK_FIRERED_THRESHOLD", fv._DEFAULT_THRESHOLD) == 0.5


def test_agent_assembly_pins_killswitch_default():
    """kill-switch 默认零变化 + 回退 silero：源码级 pin（默认 silero、字面量读取）。

    test_forward_env 已保证 4 键登记；此处钉「默认档=不碰 silero」的装配形状。
    """
    src = (_ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
        encoding="utf-8"
    )
    assert 'os.environ.get("BOK_VAD_PROVIDER", "silero")' in src
    assert '== "firered"' in src
    assert "_build_vad_provider" in src
    assert "falling back to silero" in src


def _import_agent():
    # conftest 已把 apps/agent 入 sys.path
    from agent_runtime import agent as agent_mod

    return agent_mod


def test_build_vad_provider_defaults_to_silero(monkeypatch):
    import livekit.agents.inference as inference_mod

    monkeypatch.delenv("BOK_VAD_PROVIDER", raising=False)
    sentinel = object()
    monkeypatch.setattr(inference_mod, "VAD", lambda **kw: sentinel)
    agent_mod = _import_agent()
    provider, kind = agent_mod._build_vad_provider({}, use_fake=False)
    assert kind == "silero" and provider is sentinel


def test_build_vad_provider_fake_wins(monkeypatch):
    # use_fake/设置 provider=fake 优先于 BOK_VAD_PROVIDER
    monkeypatch.setenv("BOK_VAD_PROVIDER", "firered")
    from agent_runtime.providers.livekit_plugins import FakeLiveKitVAD

    agent_mod = _import_agent()
    provider, kind = agent_mod._build_vad_provider({}, use_fake=True)
    assert kind == "fake" and isinstance(provider, FakeLiveKitVAD)


def test_build_vad_provider_firered_selected(monkeypatch):
    import types

    monkeypatch.setenv("BOK_VAD_PROVIDER", "firered")
    sentinel = types.SimpleNamespace(model="fake-firered")
    monkeypatch.setattr(fv, "load_firered_vad", lambda **kw: sentinel)
    agent_mod = _import_agent()
    provider, kind = agent_mod._build_vad_provider({}, use_fake=False)
    assert kind == "firered" and provider is sentinel


def test_build_vad_provider_firered_falls_back_on_error(monkeypatch, capsys):
    monkeypatch.setenv("BOK_VAD_PROVIDER", "firered")

    def _boom(**kw):
        raise FileNotFoundError("no model")

    monkeypatch.setattr(fv, "load_firered_vad", _boom)
    import livekit.agents.inference as inference_mod

    sentinel = object()
    monkeypatch.setattr(inference_mod, "VAD", lambda **kw: sentinel)
    agent_mod = _import_agent()
    provider, kind = agent_mod._build_vad_provider({}, use_fake=False)
    assert kind == "silero" and provider is sentinel  # fail-safe 回退
    assert "falling back to silero" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ⑥ 离线事件链：替身 fbank + 脚本模型驱动真 FireRedVADStream
# ---------------------------------------------------------------------------
class _FakeFbank:
    """每喂满 160 样本产 1 帧（前 2 hop 预热），帧内容固定；镜像 knf 语义。"""

    def __init__(self) -> None:
        self._fed = 0
        self._frames: list[np.ndarray] = []

    def accept_waveform(self, sr: int, samples) -> None:
        self._fed += len(samples)
        while self._fed >= 400 and len(self._frames) < (self._fed - 400) // 160 + 1:
            self._frames.append(np.full(80, 0.1, dtype=np.float32))

    @property
    def num_frames_ready(self) -> int:
        return len(self._frames)

    def get_frame(self, i: int) -> np.ndarray:
        return self._frames[i]


class _ScriptedModel:
    """按调用序吐脚本概率；cache 原样回环（本测试不验证 cache 数值）。"""

    name = "fake-firered"

    def __init__(self, probs: list[float], cache_shape=(8, 1, 128, 19)) -> None:
        self._probs = probs
        self._i = 0
        self._cache_shape = cache_shape

    def zeros_cache(self) -> np.ndarray:
        return np.zeros(self._cache_shape, dtype=np.float32)

    def infer(self, feat: np.ndarray, cache: np.ndarray):
        p = self._probs[min(self._i, len(self._probs) - 1)]
        self._i += 1
        return float(p), cache


def _audio_frame(n: int = 160) -> "object":
    from livekit import rtc

    data = np.zeros(n, dtype=np.int16).tobytes()
    return rtc.AudioFrame(
        sample_rate=16000, num_channels=1, samples_per_channel=n, data=data
    )


def _run_stream(vad, frames):
    async def _main():
        stream = vad.stream()
        events = []

        async def _collect():
            async for ev in stream:
                events.append(ev)

        task = asyncio.create_task(_collect())
        for fr in frames:
            stream.push_frame(fr)
            await asyncio.sleep(0)
        stream.end_input()
        await asyncio.wait_for(task, timeout=10)
        return events

    return asyncio.run(_main())


def test_stream_events_preroll_and_tiling(monkeypatch):
    from livekit.agents import vad as lk_vad

    monkeypatch.setattr(fv, "_make_fbank", lambda: _FakeFbank())
    # 60 帧静音（填满 0.5s prefix）+ 25 帧语音 + 95 帧静音
    n_frames = 180
    probs = [0.0] * 60 + [0.9] * 25 + [0.0] * 95
    params = fv.VADParams.from_durations(smooth_window=1)  # 15/35
    model = _ScriptedModel(probs)
    vad = fv.FireRedVAD(
        model=model,
        cmvn_mean=np.zeros(80, dtype=np.float32),
        cmvn_istd=np.ones(80, dtype=np.float32),
        params=params,
    )
    events = _run_stream(vad, [_audio_frame() for _ in range(n_frames)])

    infs = [e for e in events if e.type == lk_vad.VADEventType.INFERENCE_DONE]
    starts = [e for e in events if e.type == lk_vad.VADEventType.START_OF_SPEECH]
    ends = [e for e in events if e.type == lk_vad.VADEventType.END_OF_SPEECH]

    assert len(infs) == n_frames - 2  # 前 2 hop 为 25ms 窗预热，无帧无 prob
    assert len(starts) == 1 and len(ends) == 1
    assert events.index(starts[0]) < events.index(ends[0])

    # 每 INFERENCE_DONE 恰好挂 1 个 10ms hop（不重叠——消费侧连续性前提）
    assert all(e.frames[0].samples_per_channel == 160 for e in infs)

    # START pre-roll = 0.5s prefix + min_speech 确认窗（silero 等价语义）
    assert starts[0].frames[0].samples_per_channel == (
        params.prefix_padding_frames + params.min_speech_frames
    ) * 160
    # END 段完整（含 prefix，长度 > START）
    assert ends[0].frames[0].samples_per_channel > starts[0].frames[0].samples_per_channel


def test_stream_flush_resets_state(monkeypatch):
    from livekit.agents import vad as lk_vad

    monkeypatch.setattr(fv, "_make_fbank", lambda: _FakeFbank())
    model = _ScriptedModel([0.9] * 400)
    params = fv.VADParams.from_durations(smooth_window=1)
    vad = fv.FireRedVAD(
        model=model,
        cmvn_mean=np.zeros(80, dtype=np.float32),
        cmvn_istd=np.ones(80, dtype=np.float32),
        params=params,
    )

    async def _main():
        stream = vad.stream()
        events = []

        async def _collect():
            async for ev in stream:
                events.append(ev)

        task = asyncio.create_task(_collect())
        for _ in range(20):
            stream.push_frame(_audio_frame())
            await asyncio.sleep(0)
        stream.flush()  # 硬段边界：状态机/游标清零
        for _ in range(20):
            stream.push_frame(_audio_frame())
            await asyncio.sleep(0)
        stream.end_input()
        await asyncio.wait_for(task, timeout=10)
        return events

    events = asyncio.run(_main())
    starts = [e for e in events if e.type == lk_vad.VADEventType.START_OF_SPEECH]
    # 两段各自独立起报（flush 不残留旧段落）
    assert len(starts) >= 2


# ---------------------------------------------------------------------------
# ⑦ 真模型 smoke（skipif）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _DEPS_PRESENT, reason="knf/ort/model asset absent")
def test_onnx_one_frame_smoke():
    model = fv._get_model(_ASSET_DIR / fv._MODEL_FILENAME)
    assert model._feat_input == "feat"
    assert model._cache_input == "caches_in"
    assert model._cache_shape == (8, 1, 128, 19)
    feat = np.zeros((1, 1, 80), dtype=np.float32)
    prob, cache = model.infer(feat, model.zeros_cache())
    assert 0.0 <= prob <= 1.0
    assert cache.shape == model._cache_shape


@pytest.mark.skipif(not _DEPS_PRESENT, reason="knf/ort/model asset absent")
def test_load_firered_real_assembly_and_env_override(monkeypatch):
    monkeypatch.setenv("BOK_FIRERED_THRESHOLD", "0.7")
    monkeypatch.setenv("BOK_FIRERED_SMOOTH", "3")
    v = fv.load_firered_vad()
    assert v.provider == "firered"
    assert "firered" in v.model.lower()
    assert v._opts.activation_threshold == 0.7
    assert v._opts.smooth_window == 3
    assert v._opts.min_speech_frames == 15 and v._opts.min_silence_frames == 35


@pytest.mark.skipif(not _DEPS_PRESENT, reason="knf/ort/model asset absent")
def test_fbank_matches_official_whole_audio():
    """流式持久 fbank 逐 160 喂 == 官方整段 accept_waveform（逐帧 0 误差）。"""
    import kaldi_native_fbank as knf

    rng = np.random.default_rng(0)
    wav = (rng.standard_normal(16000) * 3000).astype(np.int16)

    f = fv._make_fbank()
    f.accept_waveform(16000, wav.tolist())
    ref = np.vstack([f.get_frame(i) for i in range(f.num_frames_ready)])

    g = fv._make_fbank()
    got = []
    for h in range(0, len(wav) - 159, 160):
        g.accept_waveform(16000, wav[h : h + 160].tolist())
        while len(got) < g.num_frames_ready:
            got.append(g.get_frame(len(got)))
    got = np.vstack(got)
    assert ref.shape == got.shape
    np.testing.assert_array_equal(ref, got)
