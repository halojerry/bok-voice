"""smart-turn 语义闸（V1）离线单测：判定分支/kill-switch/fail-open/接线 pin。

模型判定用 mock ONNX（不打真模型——真模型只留一条 smoke 腿，缺件自动跳过）。
BOK_SMART_TURN 默认 "0"=关：关档下接线点零行为变化是本特性的第一铁律。

设计（定案）：VAD 停嘴 → 会话最近 ≤8s 尾部 PCM 喂 smart-turn-v3.2-cpu.onnx →
p≥0.5 照旧 pause-commit；p<0.5 复用既有 join-hold 等下一段并入；模型不可判
（缺件/异常/太短）→ fail-open 照旧路径。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402
from agent_runtime.providers import smart_turn as st  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_LP_PATH = ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"


# ---- kill-switch 与纯函数 -----------------------------------------------------


def test_gate_default_off(monkeypatch):
    """默认档=关（未验收特性不默认开）；=="1" 才启用，其余值一律关。"""
    monkeypatch.delenv("BOK_SMART_TURN", raising=False)
    assert st.smart_turn_enabled() is False
    monkeypatch.setenv("BOK_SMART_TURN", "0")
    assert st.smart_turn_enabled() is False
    monkeypatch.setenv("BOK_SMART_TURN", "1")
    assert st.smart_turn_enabled() is True
    monkeypatch.setenv("BOK_SMART_TURN", "true")  # 全仓同款 =="1" 读法，宽进不行
    assert st.smart_turn_enabled() is False


def test_forward_env_registration():
    """立法门禁呼应：BOK_SMART_TURN 必须在 bok.env._FORWARD_ENV（prod 封闭 env 面）。"""
    assert "BOK_SMART_TURN" in bok.env._FORWARD_ENV


def test_decide_pure():
    """p≥0.5=commit、p<0.5=hold、None=pass（fail-open 语义）。"""
    assert st.smart_turn_decide(None) == "pass"
    assert st.smart_turn_decide(0.0) == "hold"
    assert st.smart_turn_decide(0.499) == "hold"
    assert st.smart_turn_decide(0.5) == "commit"
    assert st.smart_turn_decide(0.97) == "commit"


def test_preprocess_pcm16_to_float_and_8s():
    """int16 → [-1,1]；留尾截 8s / 头部补零到 8s（与上游同款语义）。"""
    import numpy as np

    wave = st._pcm16_to_float(b"\x00\x40")  # int16 16384 → 0.5
    assert wave.dtype == np.float32
    assert wave[0] == pytest.approx(0.5)

    full = np.zeros(st._MODEL_SAMPLES_8S + 100, dtype=np.float32)
    full[-1] = 1.0  # 尾部标记
    cut = st.truncate_or_pad_to_8s(full)
    assert cut.size == st._MODEL_SAMPLES_8S
    assert cut[-1] == 1.0  # 留尾：标记保住

    short = np.ones(16, dtype=np.float32)
    pad = st.truncate_or_pad_to_8s(short)
    assert pad.size == st._MODEL_SAMPLES_8S
    assert pad[0] == 0.0 and pad[-1] == 1.0  # 头部补零


# ---- smart_turn_prob（mock ONNX）---------------------------------------------


class _FakeAnalyzer:
    def __init__(self, prob: float = 0.9, *, raise_: bool = False):
        self.prob = prob
        self.raise_ = raise_
        self.calls: list[bytes] = []

    def predict(self, pcm: bytes) -> float:
        if self.raise_:
            raise RuntimeError("boom")
        self.calls.append(pcm)
        return self.prob


@pytest.fixture()
def fresh_singleton(monkeypatch):
    """单例全局态复位：每个用例独立（monkeypatch 掉已加载的分析器）。"""
    monkeypatch.setattr(st, "_analyzer", None)
    monkeypatch.setattr(st, "_analyzer_failed", False)
    yield
    # 还原真实态（后续真模型 smoke 用例自行重取）
    st._analyzer = None
    st._analyzer_failed = False


def test_prob_gate_off_returns_none_without_model(monkeypatch, fresh_singleton):
    """闸关：零成本返回 None，绝不触碰模型加载（未验收默认档的零行为变化）。"""
    monkeypatch.delenv("BOK_SMART_TURN", raising=False)

    def _boom():
        raise AssertionError("gate off must not load model")

    monkeypatch.setattr(st, "SmartTurnAnalyzer", _boom)
    assert asyncio.run(st.smart_turn_prob(b"\x00\x01" * 16000)) is None


def test_prob_hold_and_commit_with_fake(monkeypatch, fresh_singleton):
    """p<0.5→hold 判定、p≥0.5→commit 判定；PCM 原样透传给推理层。"""
    monkeypatch.setenv("BOK_SMART_TURN", "1")
    pcm = b"\x01\x02" * (st._MIN_PCM_BYTES // 2 + 8)
    fake = _FakeAnalyzer(prob=0.2)
    monkeypatch.setattr(st, "_analyzer", fake)
    prob = asyncio.run(st.smart_turn_prob(pcm))
    assert prob == pytest.approx(0.2)
    assert st.smart_turn_decide(prob) == "hold"
    fake2 = _FakeAnalyzer(prob=0.8)
    monkeypatch.setattr(st, "_analyzer", fake2)
    prob2 = asyncio.run(st.smart_turn_prob(pcm))
    assert prob2 == pytest.approx(0.8)
    assert st.smart_turn_decide(prob2) == "commit"
    assert fake.calls[0] == pcm


def test_prob_short_audio_skips(monkeypatch, fresh_singleton, capsys):
    """太短（<0.4s）不喂模型：skip 打点 + None（照旧路径）。"""
    monkeypatch.setenv("BOK_SMART_TURN", "1")
    monkeypatch.setattr(st, "_analyzer", _FakeAnalyzer())
    assert asyncio.run(st.smart_turn_prob(b"\x00\x01" * 16)) is None
    out = capsys.readouterr().out
    assert "SMART_TURN verdict=skip" in out and "short_audio" in out


def test_prob_inference_error_failopen(monkeypatch, fresh_singleton, capsys):
    """推理异常 → None（fail-open）+ failopen 打点，绝不 raise 伤转写主链。"""
    monkeypatch.setenv("BOK_SMART_TURN", "1")
    monkeypatch.setattr(st, "_analyzer", _FakeAnalyzer(raise_=True))
    assert asyncio.run(st.smart_turn_prob(b"\x00\x01" * (st._MIN_PCM_BYTES // 2 + 4))) is None
    assert "SMART_TURN verdict=failopen" in capsys.readouterr().out


def test_load_failure_cached_not_retried(monkeypatch, fresh_singleton, capsys):
    """模型缺位：failopen 打点一次并缓存失败（防每段语音重复吃异常）。"""
    monkeypatch.setenv("BOK_SMART_TURN", "1")
    monkeypatch.setattr(st, "SmartTurnAnalyzer", lambda: (_ for _ in ()).throw(FileNotFoundError("no model")))
    pcm = b"\x00\x01" * (st._MIN_PCM_BYTES // 2 + 4)
    assert asyncio.run(st.smart_turn_prob(pcm)) is None
    assert asyncio.run(st.smart_turn_prob(pcm)) is None
    out = capsys.readouterr().out
    assert out.count("reason=model_load") == 1, "失败必须缓存，只打一次"


# ---- 真模型 smoke（缺件跳过）-------------------------------------------------


def test_real_model_smoke(monkeypatch, fresh_singleton):
    """真 ONNX + 真 vendored 特征件端到端：静音→「未说完」、有声短语→概率有值。

    只断言契约形状（概率 ∈ [0,1]、静音判 incomplete），不断言具体数值——
    语义质量由 A/B 实弹腿验证（粤语不在官方 23 语言表，恰是本仓关注点）。
    """
    onnxruntime = pytest.importorskip("onnxruntime")
    if not st._MODEL_PATH.is_file():
        pytest.skip(f"model asset missing: {st._MODEL_PATH}")
    import numpy as np

    analyzer = st.SmartTurnAnalyzer()
    # 纯静音 2s：「没有进行中的话」=轮次已完，模型应判 complete（p ≥ 0.5）。
    silence = np.zeros(16000 * 2, dtype="<i2").tobytes()
    p_silence = analyzer.predict(silence)
    assert 0.0 <= p_silence <= 1.0
    assert p_silence >= 0.5, f"纯静音应判说完，得到 p={p_silence:.3f}"
    # 2s 白噪声（有能量）：契约形状
    rng = np.random.default_rng(42)
    noise = (rng.integers(-3000, 3000, size=16000 * 2)).astype("<i2").tobytes()
    p_noise = analyzer.predict(noise)
    assert 0.0 <= p_noise <= 1.0
    del onnxruntime  # 只为 importorskip


# ---- 接线 pin（源级，与 test_late_final_guard 同惯例）------------------------


def test_livekit_plugins_wiring_source_pins():
    """接线必须落在 END_OF_SPEECH 分支的 closing_say 抑制之后、join-hold 词表检查
    之前；held 分支复用 _hold_flush；_reset 清尾部缓冲。"""
    src = _LP_PATH.read_text(encoding="utf-8")
    i_suppress = src.index('print("QWEN3_ASR_CLOSING_SAY_SUPPRESS src=segment_eos"')
    i_gate = src.index("_smart_turn.smart_turn_enabled()")
    i_vocab = src.index("_vocab_hit = (")
    assert i_suppress < i_gate < i_vocab, "smart-turn 闸必须在 closing_say 抑制后、join-hold 词表检查前"
    assert src.count("SMART_TURN verdict=held") == 1
    assert src.count("SMART_TURN verdict=committed") == 1
    # held 分支复用既有 join-hold 机制（不新造）：
    i_held = src.index("SMART_TURN verdict=held")
    assert src.index("self._join_hold_active = True", i_held) < src.index("self._join_task = asyncio.create_task(self._hold_flush())", i_held)
    assert "_smart_turn.smart_turn_prob" in src
    assert "self._smart_pcm.clear()" in src
    assert "def _append_turn_pcm" in src
    # hold 期间 partial 继续滚（finishing 置 False，与 join-hold 分支同纪律）
    assert "self._finishing = False  # hold 期间 partial 继续滚" in src


# ---- 车道门（V1 定案 2026-09-26：cantonese 恒关，zh/en 可用）------------------


def test_lane_allowed_pure():
    """cantonese 恒关；zh/en 可用；None/未知/空=不挡（fail-open 精神）。"""
    assert st.smart_turn_lane_allowed("cantonese") is False
    assert st.smart_turn_lane_allowed("zh") is True
    assert st.smart_turn_lane_allowed("en") is True
    assert st.smart_turn_lane_allowed(None) is True
    assert st.smart_turn_lane_allowed("") is True
    assert st.smart_turn_lane_allowed("fr") is True


def test_lane_gate_combo_blocks_canto(monkeypatch):
    """组合门（照接线点条件逐字模拟）：闸开 + cantonese → 闸不激活，
    smart_turn_prob 不被调用（推理层零触碰）。"""
    monkeypatch.setenv("BOK_SMART_TURN", "1")
    assert st.smart_turn_enabled() is True

    def _never_call(pcm):
        raise AssertionError("cantonese lane must not reach inference")

    monkeypatch.setattr(st, "smart_turn_prob", _never_call)
    # 与 livekit_plugins END_OF_SPEECH 分支同款组合条件：
    lang = "cantonese"
    if st.smart_turn_enabled() and st.smart_turn_lane_allowed(lang):
        asyncio.run(st.smart_turn_prob(b"\x00\x01" * 16))  # pragma: no cover - 不应到达


def test_lane_gate_combo_allows_zh_and_en(monkeypatch, fresh_singleton):
    """组合门：闸开 + zh/en → 车道放行，闸照常评估（prob 语义由既有用例钉）。"""
    monkeypatch.setenv("BOK_SMART_TURN", "1")
    monkeypatch.setattr(st, "_analyzer", _FakeAnalyzer(prob=0.2))
    pcm = b"\x01\x02" * (st._MIN_PCM_BYTES // 2 + 8)
    for lang in ("zh", "en"):
        assert st.smart_turn_enabled() and st.smart_turn_lane_allowed(lang)
        prob = asyncio.run(st.smart_turn_prob(pcm))
        assert prob == pytest.approx(0.2)
        assert st.smart_turn_decide(prob) == "hold"


def test_livekit_plugins_lane_wiring_source_pins():
    """车道门接线 pin：cantonese 必须旧路径逐字节照走——lane 只挡 smart-turn 块、
    不碰 join-hold；lane_off 打点每流一次（标志在流初始化、不在 _reset 重置）。"""
    src = _LP_PATH.read_text(encoding="utf-8")
    assert "smart_turn_lane_allowed" in src
    assert src.count('print(f"SMART_TURN lane_off lang={_st_lang}", flush=True)') == 1
    assert "self._smart_lane_off_logged = False" in src
    # 每流一次：_reset 不得重置该标志（否则每段语音都刷一遍）。
    i_reset = src.index("def _reset(self)")
    assert "_smart_lane_off_logged" not in src[i_reset:]
    # lane_off 分支只是打点，不得吞 join-hold：其位置必须在既有 join-hold 段之前。
    i_lane = src.index("SMART_TURN lane_off")
    i_vocab = src.index("_vocab_hit = (")
    assert i_lane < i_vocab, "lane_off elif 后必须原样落到 join-hold 逻辑"
