"""ASR partial 解码会话级抑制单测(2026-09-08 GPU 竞态专项 P0b)。

场景:同卡上 LLM prefill/生成与 ASR partial 全窗重解抢 Metal 时间片(受控实验:
持续 ASR 解码拖慢 LLM TTFT +24%,反之 LLM 拖慢 ASR 2-4.6×)。修复=会话级
partial_ms 档位:agent 在回复生成/播报中(thinking/speaking)把该通会话的
partial 解码间隔抬高(BOK_ASR_PARTIAL_SLOW_MS,默认 3000),listening 恢复默认。

不依赖真实模型:importlib 载 sidecar(同 test_asr_hotword_context 的 fake-model
模式),记录 generate 调用。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
import types
from pathlib import Path

import pytest

os.environ.setdefault("QWEN3_ASR_BACKEND", "mlx")
os.environ.setdefault("QWEN3_ASR_STREAM", "1")

ROOT = Path(__file__).resolve().parents[1]
VOICED = b"\x00\x19" * 12800  # 0.8s 有声 PCM(过 0.6s 最小转写门槛)


def _load_sidecar_app():
    spec = importlib.util.spec_from_file_location(
        "qwen3_asr_sidecar_partial_gate", ROOT / "services" / "qwen3-asr-sidecar" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _RecordingModel:
    def __init__(self):
        self.calls = 0

    def generate(self, wav, language=None, max_tokens=256, system_prompt=None):
        self.calls += 1
        return types.SimpleNamespace(text="我個單號係三七七八九零", language=["Cantonese"])


def _make_svc(mod, model):
    svc = mod.ASRService()
    svc._model = model
    return svc


def test_partial_ms_override_gates_decode():
    """会话 partial_ms=99999:即便过了默认 700ms 节流也不解码(回缓存)。"""
    mod = _load_sidecar_app()
    model = _RecordingModel()
    svc = _make_svc(mod, model)
    sid = svc.start(language="cantonese", partial_ms="99999")
    assert sid
    assert svc._sessions[sid]["partial_ms"] == 99999
    svc._sessions[sid]["last_partial_at"] = time.monotonic()  # 刚解过窗:elapsed≈0
    svc.chunk(sid, VOICED)
    assert model.calls == 0, "partial_ms 抑制档下 chunk 不得触发解码"


def test_default_interval_unchanged_without_param():
    """无 partial_ms 参数:行为同旧(过默认 700ms 即解码)——默认链路零回归。"""
    mod = _load_sidecar_app()
    model = _RecordingModel()
    svc = _make_svc(mod, model)
    sid = svc.start(language="cantonese")
    assert svc._sessions[sid]["partial_ms"] is None
    svc.chunk(sid, VOICED)  # 首窗:last_partial_at=0,elapsed 远超默认 700ms
    assert model.calls == 1


def test_partial_ms_endpoint_updates_live_session():
    """/api/partial_ms 调档:已开会话即时生效;空值=恢复默认;未知会话 404。"""
    mod = _load_sidecar_app()
    mod.service._model = _RecordingModel()  # 端点走模块级 service 单例
    sid = mod.service.start(language="cantonese")

    async def _tune(ms: str):
        return await mod.tune_partial_ms(session_id=sid, ms=ms)

    assert asyncio.run(_tune("3000")) == {"ok": True, "partial_ms": 3000}
    assert mod.service._sessions[sid]["partial_ms"] == 3000
    assert asyncio.run(_tune("")) == {"ok": True, "partial_ms": None}
    assert mod.service._sessions[sid]["partial_ms"] is None

    with pytest.raises(mod.HTTPException) as ei:
        asyncio.run(mod.tune_partial_ms(session_id="nope", ms="3000"))
    assert ei.value.status_code == 404


def test_live_stt_set_partial_ms_stores_and_forwards():
    """Qwen3ASRLiveSTT.set_partial_ms:记 override(下个会话生效)+ 即时转发在活流。"""
    sys.path.insert(0, str(ROOT / "apps" / "agent"))
    from agent_runtime.providers import livekit_plugins as lp

    captured: list[int | None] = []

    class _FakeStream:
        async def _apply_partial_ms(self, ms):
            captured.append(ms)

    inner = types.SimpleNamespace(
        capabilities=lp.stt.STTCapabilities(streaming=False, interim_results=False),
        on=lambda *a, **k: None,
    )
    wrapper = lp.Qwen3ASRLiveSTT(stt_=inner, vad_=object())
    fs = _FakeStream()
    wrapper._live_streams.add(fs)

    async def _scenario():
        wrapper.set_partial_ms(3000)
        await asyncio.sleep(0)  # 让 ensure_future 排上的转发跑完
        wrapper.set_partial_ms(None)
        await asyncio.sleep(0)

    asyncio.run(_scenario())
    assert inner._partial_ms_override is None  # 末次恢复默认
    assert captured == [3000, None], captured


def test_start_session_sends_partial_ms():
    """_start_session 把 override 带进 /api/start 的 partial_ms 参数。"""
    sys.path.insert(0, str(ROOT / "apps" / "agent"))
    from agent_runtime.providers import livekit_plugins as lp

    posted: dict = {}

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"session_id": "sid-p"}

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, params=None, content=None, headers=None):
            posted["url"] = url
            posted["params"] = params
            return _FakeResp()

    monkey_mod = types.SimpleNamespace(AsyncClient=_FakeClient)
    orig_httpx = lp.httpx
    lp.httpx = monkey_mod
    try:
        inner = types.SimpleNamespace(
            _language_state=types.SimpleNamespace(lang="cantonese"),
            _pin_language=True,
            _base_url="http://127.0.0.1:8787",
            _hotword_context="",
            _partial_ms_override=3000,
        )

        async def _go():
            # RecognizeStream 构造器会调度任务,必须喺 event loop 内构造。
            stream = lp._Qwen3ASRLiveStream(inner, vad=object(), conn_options=lp.APIConnectOptions())
            await stream._start_session()

        asyncio.run(_go())
    finally:
        lp.httpx = orig_httpx
    assert posted["url"].endswith("/api/start")
    assert posted["params"].get("partial_ms") == "3000"


def test_agent_partial_ms_for_state():
    """agent 侧纯函数:thinking/speaking→抑制档;listening→None;slow_ms=0→功能关。"""
    sys.path.insert(0, str(ROOT / "apps" / "agent"))
    from agent_runtime.agent import partial_ms_for_state

    assert partial_ms_for_state("thinking", 3000) == 3000
    assert partial_ms_for_state("speaking", 3000) == 3000
    assert partial_ms_for_state("listening", 3000) is None
    assert partial_ms_for_state("thinking", 0) is None
