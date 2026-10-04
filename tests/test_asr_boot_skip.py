"""Qwen3 权重启动跳载/懒加载（任务 B，2026-10-01）。

病灶：BOK_ASR_ENGINE 缺省已翻 sensevoice（CPU 车道 40-55ms/轮），但 :8787
sidecar 启动仍 eager 加载 Qwen3-1.7B-8bit GPU 权重（asr.log "loaded ...
device=gpu"，~1.9GB 纯占卡零消费）。契约：

- ``_boot_engine``：env 缺省/未知 → sensevoice（与 agent ``_asr_engine_from_cfg``
  缺省对齐）；显式 qwen3/mlx → 旧路径（eager，行为零变化）。
- ``_startup``：sensevoice 档跳载并打一行 ``ASR_QWEN3_SKIPPED engine=sensevoice``；
  显式 qwen3 照旧 eager。
- 跳载档首个真走 qwen3 路径的请求（chunk partial / finish 的 ``_ensure_loaded``）
  触发懒加载（单飞锁，并发请求排队不丢；失败 503 明确报错）——回滚
  ``BOK_ASR_ENGINE=qwen3`` 免重启。
- bok 起 :8787 时把 ``BOK_ASR_ENGINE`` 透传进 sidecar env（源级 pin）。
"""
from __future__ import annotations

import importlib.util
import os
import threading
import time
import types
from pathlib import Path

import pytest

from _bok_src import bok_source

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")


def _load_sidecar_app():
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_bootskip", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 启动档解析
# ---------------------------------------------------------------------------


def test_boot_engine_defaults_to_sensevoice(monkeypatch):
    """缺省/未知/sensevoice(任何大小写) → sensevoice（agent 缺省对齐）;
    显式 qwen3/mlx → ""=旧路径。"""
    mod = _load_sidecar_app()
    monkeypatch.delenv("BOK_ASR_ENGINE", raising=False)
    assert mod._boot_engine() == "sensevoice"
    for v in ("", "sensevoice", "SV", "garbage"):
        monkeypatch.setenv("BOK_ASR_ENGINE", v)
        assert mod._boot_engine() == "sensevoice", v
    for v in ("qwen3", " QWEN3 ", "mlx"):
        monkeypatch.setenv("BOK_ASR_ENGINE", v)
        assert mod._boot_engine() == "", v


# ---------------------------------------------------------------------------
# _startup gating
# ---------------------------------------------------------------------------


def test_startup_skips_qwen3_load_when_sensevoice(monkeypatch, capsys):
    """sensevoice 档启动不调 load（零 GPU 权重），打 ASR_QWEN3_SKIPPED 一行。"""
    mod = _load_sidecar_app()
    monkeypatch.setenv("BOK_ASR_ENGINE", "sensevoice")
    loads: list = []
    monkeypatch.setattr(mod.service, "load", lambda: loads.append(1))
    monkeypatch.setattr(mod.service, "_qwen3_deferred", False)
    mod._startup()
    assert loads == [], "sensevoice 档绝不 eager 加载 Qwen3 权重"
    assert mod.service._qwen3_deferred is True
    assert "ASR_QWEN3_SKIPPED engine=sensevoice" in capsys.readouterr().out


def test_startup_qwen3_eager_unchanged(monkeypatch, capsys):
    """显式 qwen3=回滚档：启动 eager 加载，行为零变化；不打跳载行。"""
    mod = _load_sidecar_app()
    monkeypatch.setenv("BOK_ASR_ENGINE", "qwen3")
    loads: list = []
    monkeypatch.setattr(mod.service, "load", lambda: loads.append(1))
    monkeypatch.setattr(mod.service, "_qwen3_deferred", False)
    mod._startup()
    assert loads == [1]
    assert mod.service._qwen3_deferred is False
    assert "ASR_QWEN3_SKIPPED" not in capsys.readouterr().out


def test_health_reports_deferred_state(monkeypatch):
    """跳载档 health：model_ready=False 是设计态，qwen3_deferred 如实暴露。"""
    from fastapi.testclient import TestClient

    mod = _load_sidecar_app()
    monkeypatch.setenv("BOK_ASR_ENGINE", "sensevoice")
    with TestClient(mod.app) as client:
        body = client.get("/health").json()
    assert body["ok"] is True
    assert body["model_ready"] is False
    assert body["qwen3_deferred"] is True


# ---------------------------------------------------------------------------
# 懒加载
# ---------------------------------------------------------------------------


def test_ensure_loaded_lazy_loads_once(monkeypatch, capsys):
    """跳载档首个 qwen3 请求触发懒加载（单次），打 LAZY_LOAD 一行。"""
    mod = _load_sidecar_app()
    svc = mod.ASRService()
    svc._qwen3_deferred = True
    loads: list = []

    def _fake_load():
        loads.append(1)
        svc._model = object()

    monkeypatch.setattr(svc, "load", _fake_load)
    svc._ensure_loaded()
    svc._ensure_loaded()
    assert len(loads) == 1, "懒加载只发生一次"
    assert svc._qwen3_deferred is False
    assert "ASR_QWEN3_LAZY_LOAD engine=qwen3" in capsys.readouterr().out


def test_lazy_load_failure_is_503_not_silent(monkeypatch):
    """加载失败→503+原因（不静默丢请求）；后续调用直接 503，不重复加载。"""
    mod = _load_sidecar_app()
    svc = mod.ASRService()
    svc._qwen3_deferred = True
    loads: list = []

    def _fake_load():
        loads.append(1)
        svc._load_error = "boom"

    monkeypatch.setattr(svc, "load", _fake_load)
    with pytest.raises(mod.HTTPException) as ei:
        svc._ensure_loaded()
    assert ei.value.status_code == 503 and "boom" in str(ei.value.detail)
    with pytest.raises(mod.HTTPException):
        svc._ensure_loaded()
    assert len(loads) == 1


def test_eager_mode_zero_change(monkeypatch):
    """eager 档（显式 qwen3）_model 未就绪→旧 503，绝不触发补加载。"""
    mod = _load_sidecar_app()
    svc = mod.ASRService()
    loads: list = []
    monkeypatch.setattr(svc, "load", lambda: loads.append(1))
    with pytest.raises(mod.HTTPException) as ei:
        svc._ensure_loaded()
    assert ei.value.status_code == 503
    assert loads == []


def test_concurrent_first_requests_single_load(monkeypatch):
    """并发首个 qwen3 请求：单飞锁排队等加载，不重复加载、不丢请求。"""
    mod = _load_sidecar_app()
    svc = mod.ASRService()
    svc._qwen3_deferred = True
    loads: list = []

    def _fake_load():
        time.sleep(0.05)
        loads.append(1)
        svc._model = object()

    monkeypatch.setattr(svc, "load", _fake_load)
    errors: list = []

    def _worker():
        try:
            svc._ensure_loaded()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert loads == [1]
    assert errors == []


class _FakeQwenModel:
    def generate(self, waveform, language=None, system_prompt=None, max_tokens=None):
        return types.SimpleNamespace(text="懒加载後的 partial", language=["Chinese"])


def test_partial_mlx_triggers_lazy_load(monkeypatch, capsys):
    """qwen3 会话首个 partial（真实解码入口）也收敛懒加载——不静默丢 partial。"""
    mod = _load_sidecar_app()
    svc = mod.ASRService()
    svc._qwen3_deferred = True
    loads: list = []

    def _fake_load():
        loads.append(1)
        svc._model = _FakeQwenModel()

    monkeypatch.setattr(svc, "load", _fake_load)
    session = {
        "chunks": bytearray(b"\x00\x19" * int(16000 * 1.5)),
        "language": "zh",
        "partial_ms": 1,
        "inf_lock": threading.Lock(),
        "last_partial_at": 0.0,
    }
    out = svc._partial_mlx(session)
    assert loads == [1]
    assert out["text"] == "懒加载後的 partial"
    assert session["partial_text"] == "懒加载後的 partial"
    assert "ASR_QWEN3_LAZY_LOAD" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 源级 pin（防回退）
# ---------------------------------------------------------------------------


def test_wiring_source_pins():
    sidecar = (ROOT / "services" / "qwen3-asr-sidecar" / "app.py").read_text(encoding="utf-8")
    bok = bok_source()
    # sidecar:启动闸 + 懒加载单飞 + 观测行
    assert "def _boot_engine(" in sidecar
    assert 'if _boot_engine() == "sensevoice":' in sidecar
    assert "ASR_QWEN3_SKIPPED engine=sensevoice" in sidecar
    assert "ASR_QWEN3_LAZY_LOAD engine=qwen3" in sidecar
    assert "service._qwen3_deferred = True" in sidecar
    assert "self._load_lock = threading.Lock()" in sidecar
    assert "self._loading or self._qwen3_deferred" in sidecar or "(self._qwen3_deferred or self._loading)" in sidecar
    # bok:引擎档透传进 :8787 sidecar env（prod 封闭 env 面下发点）
    assert 'asr_env["BOK_ASR_ENGINE"]' in bok
