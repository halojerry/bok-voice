"""ASR sidecar 并发竞态让位(2026-10-01 双通实测定案)。

病灶:一机多通共享 MPS——一通的 finish 整段重解/regular partial 在飞时,
另一通的关键解码(chunk partial/EOU finish)排队,延迟到窗截断=转写乱字
(双探针并发 soak 第二句被听成「…拜拜」→farewell 收线实证)。

契约:
- ``_INF_INFLIGHT`` 计数覆盖三条 MLX generate 入口(partial/增量尾段/整句);
- 让位①:finish 短轮强制整句(置信度专用,文本质量与增量路径等价)在
  「另有推理在飞」时跳过——``finish contention-skip short-full`` 观测行;
- 让位②:partial 解码间隔在别人在飞时 ×2(``_partial_should_skip`` 纯函数);
- kill-switch ``QWEN3_ASR_CONTENTION_YIELD=0`` 全关回旧行为。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from _bok_src import bok_source

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")


def _load_sidecar_app():
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_contention",
        ROOT / "services" / "qwen3-asr-sidecar" / "app.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeOut:
    def __init__(self, text: str, lang: str = "Cantonese"):
        self.text = text
        self.language = [lang]


class _FakeModel:
    def __init__(self, text: str = "你好嗎"):
        self._text = text
        self.generate_calls = 0

    def generate(self, audio, language=None, system_prompt=None, max_tokens=256):
        self.generate_calls += 1
        return _FakeOut(self._text)


def _voiced_pcm(seconds: float = 1.5) -> bytes:
    return b"\x00\x19" * int(16000 * seconds)


# ---- 计数器与纯函数 ----


def test_inflight_counter_semantics():
    mod = _load_sidecar_app()
    assert mod._INF_INFLIGHT["n"] == 0
    assert mod._others_inflight() is False
    with mod._inflight():
        assert mod._INF_INFLIGHT["n"] == 1
        assert mod._others_inflight() is True  # 调用点「进 with 前」的语义:已有在飞
        with mod._inflight():
            assert mod._INF_INFLIGHT["n"] == 2
        assert mod._INF_INFLIGHT["n"] == 1
    assert mod._INF_INFLIGHT["n"] == 0
    assert mod._others_inflight() is False


def test_partial_should_skip_doubles_interval_under_contention():
    mod = _load_sidecar_app()
    # 无并发:到点就跑
    assert mod._partial_should_skip(1500, 1000, False) is False
    assert mod._partial_should_skip(500, 1000, False) is True
    # 有并发:间隔×2——1500ms < 2000ms 照跳,2100ms 才放行
    assert mod._partial_should_skip(1500, 1000, True) is True
    assert mod._partial_should_skip(2100, 1000, True) is False


def test_contention_yield_env_gate(monkeypatch):
    mod = _load_sidecar_app()
    monkeypatch.delenv("QWEN3_ASR_CONTENTION_YIELD", raising=False)
    assert mod._contention_yield_on() is True
    monkeypatch.setenv("QWEN3_ASR_CONTENTION_YIELD", "0")
    assert mod._contention_yield_on() is False


# ---- 让位①行为面:finish 短轮强制整句在别人在飞时跳过 ----


def _mk_service(monkeypatch, mod):
    monkeypatch.setattr(mod, "BACKEND", "mlx")
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "1")
    monkeypatch.setenv("QWEN3_ASR_CONTENTION_YIELD", "1")
    model = _FakeModel("你好嗎")
    svc = mod.ASRService()
    svc._model = model
    return svc, model


def test_finish_skips_short_full_when_others_inflight(monkeypatch, capsys):
    """并发在飞 → 短轮强制整句跳过:走增量零尾路,零额外 generate,打观测行。"""
    mod = _load_sidecar_app()
    svc, model = _mk_service(monkeypatch, mod)
    sid = svc.start(language="cantonese")
    svc.chunk(sid, _voiced_pcm(1.5))  # 攒 buffer + 放一次 partial(全覆盖)
    assert model.generate_calls == 1  # 只有那次 partial 解码
    mod._INF_INFLIGHT["n"] = 1  # 模拟另一通推理在飞
    try:
        out = svc.finish(sid)
    finally:
        mod._INF_INFLIGHT["n"] = 0
    assert "contention-skip short-full" in capsys.readouterr().out
    # 1.5s ≤2.0s 本应强制整句;让位后走增量(零尾直转,零额外解码)
    assert model.generate_calls == 1, "让位后不得再跑整句解码"
    assert isinstance(out.get("text"), str)


def test_finish_keeps_short_full_when_alone(monkeypatch, capsys):
    """无并发 → 短轮强制整句照旧(置信度档零变化,回归面)。"""
    mod = _load_sidecar_app()
    svc, model = _mk_service(monkeypatch, mod)
    sid = svc.start(language="cantonese")
    svc.chunk(sid, _voiced_pcm(1.5))
    mod._INF_INFLIGHT["n"] = 0
    out = svc.finish(sid)
    assert "contention-skip short-full" not in capsys.readouterr().out
    assert model.generate_calls >= 2, "独行时短轮强制整句必须照跑(partial+整句)"


def test_finish_contention_skip_disabled_by_env(monkeypatch, capsys):
    """QWEN3_ASR_CONTENTION_YIELD=0:并发在飞也不让位,旧行为逐字节回退。"""
    mod = _load_sidecar_app()
    svc, model = _mk_service(monkeypatch, mod)
    monkeypatch.setenv("QWEN3_ASR_CONTENTION_YIELD", "0")
    sid = svc.start(language="cantonese")
    svc.chunk(sid, _voiced_pcm(1.5))
    mod._INF_INFLIGHT["n"] = 1
    try:
        svc.finish(sid)
    finally:
        mod._INF_INFLIGHT["n"] = 0
    assert "contention-skip short-full" not in capsys.readouterr().out
    assert model.generate_calls >= 2


# ---- 接线 pin ----


def test_wiring_source_pins():
    """三条 generate 入口全包计数 + 两处让位 + bok.py prod 透传。"""
    src = (ROOT / "services" / "qwen3-asr-sidecar" / "app.py").read_text(encoding="utf-8")
    bok = bok_source()
    # 计数包裹三条入口(partial/增量尾段/整句含 conf 路)
    assert src.count("with _inflight():") >= 3
    # 让位①/②
    assert "finish contention-skip short-full" in src
    assert "_partial_should_skip(" in src
    # prod 封闭面透传(sidecar 不 merge 全 env)
    assert '"QWEN3_ASR_CONTENTION_YIELD"' in bok
