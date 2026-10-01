"""句级置信度暴露单测(2026-09-27):不加载真模型、不依赖 mlx。

替身 stream_generate 逐 token yield (token_id, fake_logprobs);mlx 专用的标量
求值 (_top1_prob) 与采样器构造 (_make_greedy_sampler) 用替身替换——主 venv
(.venv312) 无 mlx,真 mlx 路径留给实弹验收。覆盖:聚合正确性 / fail-open 回退 /
kill-switch / API 键向后兼容 / partial 不接。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import types
from pathlib import Path

import numpy as np

os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")
# 注意:不在模块级改 QWEN3_ASR_STREAM——test_asr_incremental_finish 等同套件靠
# setdefault("1") 开滑窗 partial,模块级污染会令它们全部哑火(实测踩到)。
# 需要关/开 partial 的用例在测试内用 monkeypatch 显式设。

ROOT = Path(__file__).resolve().parents[1]

# 替身 tokenizer 的 token id → 文本映射,拼出 "你好嗎"。
_TOKEN_TEXT = {1: "你", 2: "好", 3: "嗎"}


def _load_sidecar_app():
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_conf",
        ROOT / "services" / "qwen3-asr-sidecar" / "app.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeTokenizer:
    def decode(self, tokens, skip_special_tokens=True):
        return "".join(_TOKEN_TEXT.get(int(t), "") for t in tokens)


class _FakeLogprobs:
    """替身 logprobs:只带一个 top1 概率 p;_top1_prob 替身直接读它。"""

    def __init__(self, p: float):
        self.p = float(p)


class _ConfModel:
    """替身模型:stream_generate 逐 token yield;generate 为旧路兜底。

    - stream_error 非空 → stream_generate 抛该异常(模拟签名漂移/模型不支持)。
    - extract_result 非空 → extract_language 直接返回该 (lang, text)(测 auto 档)。
    """

    def __init__(
        self,
        probs,
        tokens=(1, 2, 3),
        stream_error: Exception | None = None,
        old_text: str = "舊路結果",
        extract_result: tuple[str, str] | None = None,
    ):
        self._probs = list(probs)
        self._tokens = list(tokens)
        self._stream_error = stream_error
        self._old_text = old_text
        self._extract_result = extract_result
        self._tokenizer = _FakeTokenizer()
        self.stream_calls = 0
        self.generate_calls = 0

    def stream_generate(self, audio, *, max_tokens, sampler, language, system_prompt):
        self.stream_calls += 1
        if self._stream_error is not None:
            raise self._stream_error
        for tok, p in zip(self._tokens, self._probs):
            yield tok, _FakeLogprobs(p)

    def generate(self, audio, *, language=None, system_prompt=None, max_tokens=256, **kw):
        self.generate_calls += 1
        return types.SimpleNamespace(text=self._old_text, language=["Cantonese"])

    def extract_language(self, text):
        if self._extract_result is not None:
            return self._extract_result
        return "English", text


def _patch_mlx(monkeypatch, mod):
    """替掉 mlx 专用两端:采样器构造 + 标量求值(直读替身 logprobs 的 p)。"""
    monkeypatch.setattr(mod, "_make_greedy_sampler", lambda: None)
    monkeypatch.setattr(mod, "_top1_prob", lambda lp: lp.p)


def _wav(n: int = 16000) -> np.ndarray:
    # 恒定振幅:高于 trim 静音门限,不会被裁。
    return np.full(n, 0.19, dtype=np.float32)


def _voiced_pcm(seconds: float = 2.0) -> bytes:
    return b"\x00\x19" * int(16000 * seconds)


# ---- 聚合正确性 ----

def test_aggregate_conf_counts_low_prob_tokens():
    """<0.3 的 token 计 low_tokens;均值/最小值 4 位四舍五入。"""
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_conf_pure",
        ROOT / "services" / "qwen3-asr-sidecar" / "app.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod._aggregate_conf([]) is None
    assert mod._aggregate_conf([1.0]) == {
        "mean": 1.0, "min": 1.0, "low_tokens": 0, "n_tokens": 1,
    }
    conf = mod._aggregate_conf([0.9, 0.2, 0.05])
    assert conf == {"mean": 0.3833, "min": 0.05, "low_tokens": 2, "n_tokens": 3}


def test_generate_conf_decodes_tokens_and_aggregates(monkeypatch):
    """_generate_conf 逐 token 采 top1 概率,自行 decode,语言 hint 原样返回。"""
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "1")
    mod = _load_sidecar_app()
    _patch_mlx(monkeypatch, mod)
    model = _ConfModel(probs=[0.9, 0.2, 0.05])
    svc = mod.ASRService()
    svc._model = model

    text, language, conf = svc._generate_conf(
        _wav(), language="Cantonese", system_prompt=None, max_tokens=64
    )
    assert text == "你好嗎"
    assert language == "Cantonese"
    assert conf == {"mean": 0.3833, "min": 0.05, "low_tokens": 2, "n_tokens": 3}
    assert model.stream_calls == 1 and model.generate_calls == 0


def test_generate_conf_auto_language_uses_extract_language(monkeypatch):
    """hint 为空(auto 档)→ 同 generate() 走 extract_language 剥离 language 前缀。"""
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "1")
    mod = _load_sidecar_app()
    _patch_mlx(monkeypatch, mod)
    model = _ConfModel(probs=[0.8, 0.8, 0.8], extract_result=("Chinese", "你好嗎"))
    svc = mod.ASRService()
    svc._model = model

    text, language, conf = svc._generate_conf(
        _wav(), language=None, system_prompt=None, max_tokens=64
    )
    assert (text, language) == ("你好嗎", "Chinese")
    assert conf == {"mean": 0.8, "min": 0.8, "low_tokens": 0, "n_tokens": 3}


# ---- fail-open 回退 ----

def test_generate_out_fail_open_falls_back_to_old_generate(monkeypatch, capsys):
    """stream_generate 失手 → 打 ASR_CONF fallback 一行 + 回退旧 generate(),置信度 None。"""
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "1")
    mod = _load_sidecar_app()
    _patch_mlx(monkeypatch, mod)
    model = _ConfModel(
        probs=[0.9], stream_error=RuntimeError("unexpected kwarg"), old_text="舊路結果"
    )
    svc = mod.ASRService()
    svc._model = model

    text, language, conf = svc._generate_out_with_conf(
        _wav(), mod.SAMPLE_RATE, {"context": ""}, "Chinese"
    )
    assert text == "舊路結果"
    assert language == "Cantonese"
    assert conf is None
    assert model.stream_calls == 1 and model.generate_calls == 1  # 先试新路再回退
    assert "ASR_CONF fallback" in capsys.readouterr().out


def test_finish_fail_open_still_returns_confidence_key_none(monkeypatch):
    """fail-open 时响应仍带 confidence 键,值为 None(形状不随失败改变)。"""
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "1")
    mod = _load_sidecar_app()
    _patch_mlx(monkeypatch, mod)
    model = _ConfModel(probs=[0.9], stream_error=ValueError("no stream"), old_text="舊路結果")
    svc = mod.ASRService()
    svc._model = model
    sid = svc.start(language="cantonese")
    svc._sessions[sid]["chunks"].extend(_voiced_pcm())

    out = svc.finish(sid)
    assert out["text"] == "舊路結果"
    assert "confidence" in out and out["confidence"] is None


# ---- kill-switch ----

def test_kill_switch_off_uses_old_path_without_key(monkeypatch):
    """QWEN3_ASR_CONFIDENCE=0:流式路径不触发,响应不带 confidence 键(旧形状)。"""
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "0")
    mod = _load_sidecar_app()
    _patch_mlx(monkeypatch, mod)
    assert mod._CONF_ENABLED is False

    model = _ConfModel(probs=[0.9, 0.1, 0.0])
    svc = mod.ASRService()
    svc._model = model

    text, language, conf = svc._generate_out_with_conf(
        _wav(), mod.SAMPLE_RATE, {"context": ""}, "Chinese"
    )
    assert text == "舊路結果" and conf is None
    assert model.stream_calls == 0 and model.generate_calls == 1

    # _with_confidence 在关档时原样返回(逐字节旧形状)
    old = {"text": "x", "language": "", "partial": False}
    assert svc._with_confidence(old, {"mean": 1.0}) == old
    assert "confidence" not in svc._with_confidence(old, {"mean": 1.0})


def test_kill_switch_off_finish_has_no_key(monkeypatch):
    """关档下 finish 全链:旧 generate() 被走,响应无 confidence 键。"""
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "0")
    mod = _load_sidecar_app()
    _patch_mlx(monkeypatch, mod)
    model = _ConfModel(probs=[0.1], old_text="舊路結果")
    svc = mod.ASRService()
    svc._model = model
    sid = svc.start(language="cantonese")
    svc._sessions[sid]["chunks"].extend(_voiced_pcm())

    out = svc.finish(sid)
    assert "confidence" not in out
    assert out["text"] == "舊路結果"
    assert model.stream_calls == 0 and model.generate_calls >= 1


# ---- 端到端 / API 形状 ----

def test_finish_response_includes_confidence_object(monkeypatch):
    """开档下 service.finish 响应带 confidence 对象,旧键全部保留(纯加键)。"""
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "1")
    mod = _load_sidecar_app()
    _patch_mlx(monkeypatch, mod)
    model = _ConfModel(probs=[0.9, 0.5, 0.1])
    svc = mod.ASRService()
    svc._model = model
    sid = svc.start(language="cantonese")
    svc._sessions[sid]["chunks"].extend(_voiced_pcm())

    out = svc.finish(sid)
    assert out["text"] == "你好嗎"
    assert out["partial"] is False
    assert out["language"] == "cantonese"  # hint 原样
    assert out["confidence"] == {
        "mean": 0.5, "min": 0.1, "low_tokens": 1, "n_tokens": 3,
    }


def test_finish_endpoint_returns_confidence_key(monkeypatch):
    """/api/finish 端点透传 service 结果(键向后兼容,旧调用方忽略新键即可)。"""
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "1")
    mod = _load_sidecar_app()
    _patch_mlx(monkeypatch, mod)
    model = _ConfModel(probs=[0.95, 0.95, 0.95])
    mod.service._model = model
    sid = mod.service.start(language="cantonese")
    mod.service._sessions[sid]["chunks"].extend(_voiced_pcm())

    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/finish",
            "query_string": b"",
            "headers": [],
        },
        receive,
    )
    out = asyncio.run(mod.finish(session_id=sid, request=req))
    assert out["text"] == "你好嗎"
    assert out["confidence"]["n_tokens"] == 3


def test_partial_chunk_does_not_expose_confidence(monkeypatch):
    """partial 高频路径不接置信度(成本先不给):/api/chunk 响应无 confidence 键。"""
    monkeypatch.setenv("QWEN3_ASR_CONFIDENCE", "1")
    monkeypatch.setenv("QWEN3_ASR_STREAM", "1")
    mod = _load_sidecar_app()
    _patch_mlx(monkeypatch, mod)
    model = _ConfModel(probs=[0.9, 0.5, 0.1])
    svc = mod.ASRService()
    svc._model = model
    sid = svc.start(language="cantonese")
    svc._sessions[sid]["last_partial_at"] = 0.0  # 清节流窗,立即解码

    out = svc.chunk(sid, _voiced_pcm())
    assert out["partial"] is True
    assert "confidence" not in out
    assert model.stream_calls == 0  # partial 走旧 generate(),不触发流式置信度
