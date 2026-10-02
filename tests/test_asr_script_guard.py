"""M-24(2026-09-23 修复波#4):ASR finish 泰文字形幻觉守卫。

task-4 M3 实弹:短粤句 finish 输出整段泰文(「兩日內賠到」→
「เล่าอย่างหน่อย陪到。」4 渲染 3 次;另 3 句各 1 次)——Qwen3-ASR 语种混淆级
解码失败。判据=输出含泰文块(U+0E00–U+0E7F):本产品三语(zh/cantonese/en)
任何位置都不可能合法出现泰文,零误伤面。动作=触发时以 temperature 重解一次
(greedy 同音频恒同结果,采样才有变化);重解干净即采用,仍带泰文则剥泰文串
保留余文——缓解非根除(诚实边界:模型缺陷)。QWEN3_ASR_SCRIPT_GUARD=0 关。

不依赖真实模型:importlib 载 sidecar(同 test_asr_incremental_finish 的
fake-model 模式),脚本化每次 generate 的返回文本。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")
os.environ.setdefault("QWEN3_ASR_STREAM", "0")

ROOT = Path(__file__).resolve().parents[1]
for p in ("services/qwen3-asr-sidecar",):
    sp = str(ROOT / p)
    if sp not in sys.path and os.path.isdir(sp):
        sys.path.insert(0, sp)

_THAI_TEXT = "เล่าอย่างหน่อย陪到。"
_CLEAN_TEXT = "兩日內賠到。"
_THAI_TEXT2 = "เห็นสีว่า運輸途中。"
VOICED = b"\x00\x19"  # int16 6400:RMS 高于静音门限,trim 不裁


def _load_sidecar_app():
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_guard", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ScriptedModel:
    """按脚本逐次返回 generate 文本,记录 temperature/language 调用面。"""

    def __init__(self, texts: list[str]):
        self.texts = list(texts)
        self.calls: list[dict] = []

    def generate(self, wav, language=None, max_tokens=256, system_prompt=None, temperature=0.0):
        self.calls.append({"language": language, "temperature": temperature})
        text = self.texts.pop(0) if self.texts else "好"
        return types.SimpleNamespace(text=text, language=["Cantonese"])


def _finish_once(mod, model: _ScriptedModel, env: dict | None = None) -> tuple[dict, list[dict]]:
    svc = mod.ASRService()
    svc._model = model
    sid = svc.start(language="cantonese")
    svc._sessions[sid]["chunks"].extend(VOICED * 8000)
    out = svc.finish(sid)
    return out, model.calls


# ---- 纯函数面 ----


def test_has_thai_and_strip():
    assert mod_has_thai(_THAI_TEXT)
    assert not mod_has_thai(_CLEAN_TEXT)
    assert mod_has_thai(" Mixed เฮือน text")
    # 剥泰文串保余文;剥完开头残留标点一并清
    mod = _load_sidecar_app()
    assert mod._strip_thai_runs(_THAI_TEXT) == "陪到。"
    assert mod._strip_thai_runs("。เงินก้อนนี้ป้อง帮手") == "帮手"
    assert mod._strip_thai_runs(_CLEAN_TEXT) == _CLEAN_TEXT


def mod_has_thai(text: str) -> bool:
    mod = _load_sidecar_app()
    return mod._has_thai_glyphs(text)


# ---- 守卫行为面 ----


def test_clean_output_no_retry_no_cost():
    mod = _load_sidecar_app()
    model = _ScriptedModel([_CLEAN_TEXT])
    out, calls = _finish_once(mod, model)
    assert out["text"] == _CLEAN_TEXT
    assert len(calls) == 1  # 干净输出零重解零开销(误伤面=0 的结构性保证)


def test_thai_output_retry_clean_wins():
    mod = _load_sidecar_app()
    model = _ScriptedModel([_THAI_TEXT, _CLEAN_TEXT])
    out, calls = _finish_once(mod, model)
    assert out["text"] == _CLEAN_TEXT
    assert len(calls) == 2  # 泰文触发 → 恰好一次重解
    assert calls[1]["temperature"] > 0  # greedy 重解同音频恒同结果,采样才有变化


def test_thai_retry_still_thai_stripped():
    mod = _load_sidecar_app()
    model = _ScriptedModel([_THAI_TEXT, _THAI_TEXT2])
    out, calls = _finish_once(mod, model)
    assert len(calls) == 2
    assert out["text"] == "陪到。"  # 剥泰文串保留余文(缓解非根除,如实降级)
    assert not mod._has_thai_glyphs(out["text"])


def test_guard_off_returns_raw():
    mod = _load_sidecar_app()
    model = _ScriptedModel([_THAI_TEXT])
    svc = mod.ASRService()
    svc._model = model
    sid = svc.start(language="cantonese")
    svc._sessions[sid]["chunks"].extend(VOICED * 8000)
    os.environ["QWEN3_ASR_SCRIPT_GUARD"] = "0"
    try:
        out = svc.finish(sid)
    finally:
        os.environ.pop("QWEN3_ASR_SCRIPT_GUARD", None)
    assert out["text"] == _THAI_TEXT  # 关闸原样(回退口)
    assert len(model.calls) == 1


def test_guard_direct_call_on_incremental_result():
    """增量 finish(zero-tail 直转)结果同样过守卫(直接调守卫单元)。"""
    mod = _load_sidecar_app()
    model = _ScriptedModel([_CLEAN_TEXT])
    svc = mod.ASRService()
    svc._model = model
    session = {"language": "cantonese", "context": ""}
    result = {"text": _THAI_TEXT, "language": "Cantonese", "partial": False}
    out = svc._script_confusion_guard(result, session, VOICED * 8000, "Cantonese")
    assert out["text"] == _CLEAN_TEXT
    assert len(model.calls) == 1
    assert out["partial"] is False


# ---- I-2(2026-09-24 评审返工):重解降级可观测 ----


def test_retry_typeerror_observable_degradation(capsys):
    """采样 kwarg 不被签名接受(TypeError,如 mlx_audio 升级改签名)→ 显式告警
    「retry unavailable」+ 降级剥串档——静默降级是评审项,告警路径钉死。
    (真模型实证:1.7B-MLX-8bit 实载 generate 接受 temperature=0.25,737ms
    无 TypeError——本测试防的是未来签名漂移。)"""
    mod = _load_sidecar_app()

    class _OldSignatureModel:
        # 旧签名(无 temperature,无 **kwargs):带 temperature 调用即 TypeError
        def generate(self, wav, language=None, max_tokens=256, system_prompt=None):
            raise TypeError("generate() got an unexpected keyword argument 'temperature'")

    svc = mod.ASRService()
    svc._model = _OldSignatureModel()
    session = {"language": "cantonese", "context": ""}
    result = {"text": _THAI_TEXT, "language": "Cantonese", "partial": False}
    out = svc._script_confusion_guard(result, session, VOICED * 8000, "Cantonese")
    captured = capsys.readouterr().out
    assert "retry unavailable" in captured and "TypeError" in captured
    assert out["text"] == "陪到。"  # 救回路不可用 → 既定地板=剥串档
    assert not mod._has_thai_glyphs(out["text"])


def test_retry_generic_error_observable(capsys):
    """重解的其余异常同样打显式告警(可观测降级,不静默吞)。"""
    mod = _load_sidecar_app()

    class _BrokenModel:
        def generate(self, *a, **k):
            raise RuntimeError("boom")

    svc = mod.ASRService()
    svc._model = _BrokenModel()
    session = {"language": "cantonese", "context": ""}
    result = {"text": _THAI_TEXT, "language": "Cantonese", "partial": False}
    out = svc._script_confusion_guard(result, session, VOICED * 8000, "Cantonese")
    captured = capsys.readouterr().out
    assert "retry unavailable" in captured and "RuntimeError" in captured
    assert out["text"] == "陪到。"


def test_vllm_finish_exit_wrapped_by_guard():
    """M-2(评审):vllm 后端(非默认)finish 出口同样过守卫(剥串档;
    重解路 BACKEND==mlx 才有)。锚定 finish() 内的 vllm 分支——裸 index 会
    命中 chunk() 的同名分支(M-8 同款教训)。"""
    src = (ROOT / "services" / "qwen3-asr-sidecar" / "app.py").read_text(encoding="utf-8")
    i_finish = src.index("    def finish(self")
    i_vllm = src.index('if BACKEND == "vllm":', i_finish)
    i_mlx = src.index('if BACKEND == "mlx":', i_vllm)
    region = src[i_vllm:i_mlx]
    assert "finish_streaming_transcribe" in region  # 锚点自证:确实是 finish 的 vllm 段
    assert "_script_confusion_guard" in region
