"""P1-B ASR sidecar partial 节流 + FINAL 停发 + en finish hint 单测。

沿用 test_asr_incremental_finish.py 的 importlib fake-model 模式(不依赖真模型):
- PARTIAL_INTERVAL_MS 默认 700(旧 400)/PARTIAL_MAX_SEC 默认 12(旧 25)。
- /api/finish 后 partials_done=True,同会话再来的 partial 直接回缓存(零解码)。
- finish generate 的语言提示:en/english → "English";cantonese 透传;空=auto。
"""

from __future__ import annotations

import importlib.util
import os
import types
from pathlib import Path

os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")
os.environ.setdefault("QWEN3_ASR_STREAM", "1")

ROOT = Path(__file__).resolve().parents[1]

PARTIAL_TEXT = "有冇人知道灣仔活道"  # CJK 结尾:接缝安全
TAIL_TEXT = "係點去㗎"
VOICED = b"\x00\x19"  # int16 6400:RMS 高于静音门限,trim 唔会裁


def _load_sidecar_app():
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_throttle", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeModel:
    """长输入回 PARTIAL_TEXT,短输入(≤1s 尾巴)回 TAIL_TEXT;记录每次 language。"""

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, wav, language=None, max_tokens=256):
        self.calls.append({"samples": len(wav), "language": language})
        if len(wav) <= 16000:
            return types.SimpleNamespace(text=TAIL_TEXT, language=["Cantonese"])
        return types.SimpleNamespace(text=PARTIAL_TEXT, language=["Cantonese"])


def _make_svc(mod, model: _FakeModel):
    svc = mod.ASRService()
    svc._model = model
    return svc


def test_defaults_interval_700_max_sec_12(monkeypatch):
    """新默认:PARTIAL_INTERVAL_MS=700(旧 400)、PARTIAL_MAX_SEC=12(旧 25)。"""
    monkeypatch.delenv("QWEN3_ASR_PARTIAL_MS", raising=False)
    monkeypatch.delenv("QWEN3_ASR_PARTIAL_MAX_SEC", raising=False)
    mod = _load_sidecar_app()
    assert mod.PARTIAL_INTERVAL_MS == 700.0
    assert mod.PARTIAL_MAX_SEC == 12.0
    # env 名不变,仍可覆盖。
    monkeypatch.setenv("QWEN3_ASR_PARTIAL_MS", "250")
    mod2 = _load_sidecar_app()
    assert mod2.PARTIAL_INTERVAL_MS == 250.0


def test_finish_sets_partials_done_and_partial_skips_decode(monkeypatch):
    """FINAL 后停发:finish 打 partials_done 标记,喺途/迟到 partial 零解码。"""
    monkeypatch.delenv("QWEN3_ASR_PARTIAL_MS", raising=False)
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    assert svc._sessions[sid]["partials_done"] is False  # start 重置
    # 一窗正常 partial(2s > 0.6s 起步阈值,last_partial_at 清零强制推理)。
    svc._sessions[sid]["last_partial_at"] = 0.0
    out = svc.chunk(sid, VOICED * 32000)
    assert out["partial"] is True and out["text"] == PARTIAL_TEXT
    assert len(model.calls) == 1

    session = svc._sessions[sid]
    final = svc.finish(sid)
    assert final["partial"] is False
    assert session["partials_done"] is True  # finish 摘除会话前打标记

    # 同一会话 dict 再来 partial(在途 chunk 已拿到引用的场景):回缓存、零新解码。
    cached = mod.ASRService._partial_mlx(svc, session)
    assert cached == {"text": PARTIAL_TEXT, "language": "Cantonese", "partial": True}
    assert len(model.calls) == 1, "FINAL 后 partial 不得再排 GPU 解码"


def test_start_resets_partials_done(monkeypatch):
    """每个 /api/start 的新会话 partials_done 必须回 False。"""
    monkeypatch.delenv("QWEN3_ASR_PARTIAL_MS", raising=False)
    mod = _load_sidecar_app()
    svc = _make_svc(mod, _FakeModel())
    sid1 = svc.start(language="cantonese")
    svc._sessions[sid1]["partials_done"] = True
    sid2 = svc.start(language="cantonese")
    assert sid2 != sid1
    assert svc._sessions[sid2]["partials_done"] is False


def test_en_start_language_hints_english_on_finish(monkeypatch):
    """en 会话整句 decode 用显式 "English" hint,唔交 auto-LID。"""
    monkeypatch.delenv("QWEN3_ASR_PARTIAL_MS", raising=False)
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)
    sid = svc.start(language="en")
    svc._sessions[sid]["chunks"].extend(VOICED * 16000)  # 1s 语音
    svc.finish(sid)
    assert model.calls[-1]["language"] == "English"


def test_english_start_language_also_hints_english(monkeypatch):
    """start language="english"(拼写变体)同样归一 "English"。"""
    monkeypatch.delenv("QWEN3_ASR_PARTIAL_MS", raising=False)
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)
    sid = svc.start(language="english")
    svc._sessions[sid]["chunks"].extend(VOICED * 16000)
    svc.finish(sid)
    assert model.calls[-1]["language"] == "English"


def test_cantonese_hint_passthrough_and_empty_auto(monkeypatch):
    """cantonese 照旧透传;空语言=auto(None);行为与旧实现一致。"""
    monkeypatch.delenv("QWEN3_ASR_PARTIAL_MS", raising=False)
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="cantonese")
    svc._sessions[sid]["chunks"].extend(VOICED * 16000)
    svc.finish(sid)
    assert model.calls[-1]["language"] == "cantonese"

    model2 = _FakeModel()
    svc2 = _make_svc(mod, model2)
    sid2 = svc2.start(language="")
    svc2._sessions[sid2]["chunks"].extend(VOICED * 16000)
    svc2.finish(sid2)
    assert model2.calls[-1]["language"] is None  # auto


def test_en_hint_reaches_incremental_tail_decode(monkeypatch):
    """增量 finish 的尾段 generate 同样收 "English" hint(共用同一 hint 变量)。"""
    monkeypatch.delenv("QWEN3_ASR_PARTIAL_MS", raising=False)
    mod = _load_sidecar_app()
    model = _FakeModel()
    svc = _make_svc(mod, model)

    sid = svc.start(language="en")
    # 一窗新鲜 partial 覆盖 2s(大输入→PARTIAL_TEXT,CJK 尾接缝安全)。
    svc._sessions[sid]["last_partial_at"] = 0.0
    out = svc.chunk(sid, VOICED * 32000)
    assert out["partial"] is True and out["text"] == PARTIAL_TEXT
    # 尾巴 0.5s → 增量路径只解码尾段。
    svc._sessions[sid]["chunks"].extend(VOICED * 8000)
    final = svc.finish(sid)
    assert final["partial"] is False
    assert final["text"] == PARTIAL_TEXT + TAIL_TEXT
    assert model.calls[1]["language"] == "English"
    assert model.calls[1]["samples"] == 8000
