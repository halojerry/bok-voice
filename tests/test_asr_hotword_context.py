"""ASR 热词/context 通道单测:/api/start 收 context 存 session,partial 与 finish
两条解码路径的 generate 都要透传 system_prompt(Qwen3-ASR 官方 customizable
context = system message 词汇表软偏置,与 language 强制可叠加);kill-switch
QWEN3_ASR_CONTEXT=0 时一律 None(行为同旧)。

不依赖真实模型:importlib 载 sidecar(同 test_asr_incremental_finish 的
fake-model 模式),记录每次 generate 的 system_prompt kwarg。
"""

from __future__ import annotations

import importlib.util
import os
import types
from pathlib import Path

os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")
os.environ.setdefault("QWEN3_ASR_STREAM", "1")

ROOT = Path(__file__).resolve().parents[1]
VOICED = b"\x00\x19"
CONTEXT = "Vocabulary: 單號,賠償,運費"


def _load_sidecar_app():
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_hotword", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _RecordingModel:
    """记录每次 generate 的 language/system_prompt,文本按窗口长度分流。"""

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, wav, language=None, max_tokens=256, system_prompt=None):
        self.calls.append({"language": language, "system_prompt": system_prompt})
        if len(wav) <= 16000:
            text = "單號"
        else:
            text = "我個單號係三七七八九零，唔該幫我查下。"
        return types.SimpleNamespace(text=text, language=["Cantonese"])


def _make_svc(mod, model: _RecordingModel):
    svc = mod.ASRService()
    svc._model = model
    return svc


def _run_partial(svc, sid: str) -> None:
    """喂一窗音频并强制 partial 立即推理(节流窗口清零,同 incremental 测试姿势)。"""
    svc._sessions[sid]["last_partial_at"] = 0.0
    out = svc.chunk(sid, VOICED * (16000 * 2))
    assert out["partial"] is True, out


def test_start_stores_context():
    mod = _load_sidecar_app()
    svc = _make_svc(mod, _RecordingModel())
    sid = svc.start(language="Cantonese", context=CONTEXT)
    assert svc._sessions[sid]["context"] == CONTEXT
    # 旧姿势(无 context)照常,session 字段空串
    sid2 = svc.start(language="cantonese")
    assert svc._sessions[sid2]["context"] == ""


def test_generate_paths_receive_system_prompt():
    mod = _load_sidecar_app()
    model = _RecordingModel()
    svc = _make_svc(mod, model)
    sid = svc.start(language="Cantonese", context=CONTEXT)
    _run_partial(svc, sid)
    assert model.calls and model.calls[0]["system_prompt"] == CONTEXT, model.calls
    final = svc.finish(sid)
    assert final["text"], final
    prompts = [c["system_prompt"] for c in model.calls]
    assert prompts and all(p == CONTEXT for p in prompts), prompts


def test_no_context_means_none():
    mod = _load_sidecar_app()
    model = _RecordingModel()
    svc = _make_svc(mod, model)
    sid = svc.start(language="cantonese")
    _run_partial(svc, sid)
    svc.finish(sid)
    assert all(c["system_prompt"] is None for c in model.calls), model.calls


def test_kill_switch_disables_context(monkeypatch):
    mod = _load_sidecar_app()
    monkeypatch.setenv("QWEN3_ASR_CONTEXT", "0")
    model = _RecordingModel()
    svc = _make_svc(mod, model)
    sid = svc.start(language="Cantonese", context=CONTEXT)
    _run_partial(svc, sid)
    svc.finish(sid)
    assert all(c["system_prompt"] is None for c in model.calls), model.calls
