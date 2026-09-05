"""P1.5 FIX 2: STT PREFLIGHT 语言门单测（无网络，fake sidecar）。

语言切换轮（如粤→en）的滑窗 partial 检测语言 ≠ 会话锚定语言时：
- PREFLIGHT_TRANSCRIPT 必须停发（旧锚语言前缀喂框架抢跑，必被 FINAL 语言重锚
  作废重建——twin 双请求并发 prefill 自伤，en 切换轮 TTFT 4.1-4.2s 残留）；
- INTERIM_TRANSCRIPT 照发（前端字幕不受影响）；
- FINAL 路径不动（WhatsApp/话术推进逻辑用 FINAL，commit 正确性零影响）。

kill-switch：QWEN3_ASR_PREFLIGHT_LANG_GATE=0 → 永远发（回退旧行为）。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from livekit.agents.utils.aio.channel import ChanEmpty  # noqa: E402

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    LanguageState,
    Qwen3ASRSTT,
    _Qwen3ASRLiveStream,
)


class _FakeResp:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeClient:
    """sidecar /api/chunk fake：按序回预设 (text, language) 窗。"""

    windows: list[dict] = []

    def __init__(self, *a, **kw):
        self._i = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        win = _FakeClient.windows[min(self._i, len(_FakeClient.windows) - 1)]
        self._i += 1
        return _FakeResp(win)


def _make_stream(anchor: str) -> _Qwen3ASRLiveStream:
    stt_inner = Qwen3ASRSTT(base_url="http://127.0.0.1:8787", language_state=LanguageState(lang=anchor))
    return _Qwen3ASRLiveStream(stt_inner, vad=object(), conn_options=lp.APIConnectOptions())


async def _drive_window(stream: _Qwen3ASRLiveStream, body: dict) -> list[str]:
    """喂一窗 partial 并返回此刻 channel 里的事件类型序列（INTERIM/PREFLIGHT…）。"""
    stream._session_id = "sid"
    stream._last_post = 0.0
    stream._pending = bytearray(16000 * 2)  # ≥0.6s PCM，过门槛
    _FakeClient.windows = [body]
    await stream._maybe_partial()
    got: list[str] = []
    while True:
        try:
            got.append(stream._event_ch.recv_nowait().type.name)
        except ChanEmpty:
            return got


async def _close(stream: _Qwen3ASRLiveStream) -> None:
    stream._event_ch.close()
    try:
        await asyncio.wait_for(stream._metrics_task, 1)
    except Exception:  # noqa: BLE001 - 监视任务收尾失败不影响断言
        pass


def test_preflight_suppressed_on_language_mismatch(monkeypatch):
    """锚定 cantonese + partial 检测 en → PREFLIGHT 唔发，INTERIM 照发。"""
    monkeypatch.delenv("QWEN3_ASR_PREFLIGHT_LANG_GATE", raising=False)  # 默认开
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))

    async def scenario():
        stream = _make_stream("cantonese")
        try:
            # 窗 1：common=""（无参照）→ 无论语言都只有 INTERIM。
            ev1 = await _drive_window(stream, {"text": "hello how are", "language": "en"})
            # 窗 2：common="hello how are"（13 字 ≥6 且 +13 ≥4）→ 旧码必发 PREFLIGHT；
            # 语言门（en ≠ cantonese）拦下。
            ev2 = await _drive_window(
                stream, {"text": "hello how are you my friend", "language": "en"}
            )
            return ev1, ev2, stream._stable
        finally:
            await _close(stream)

    ev1, ev2, stable = asyncio.run(scenario())
    assert ev1 == ["INTERIM_TRANSCRIPT"]
    assert ev2 == ["INTERIM_TRANSCRIPT"], f"语言不匹配窗不得发 PREFLIGHT: {ev2}"
    assert stable == "", "被拦的窗不得推进 _stable"


def test_preflight_emitted_when_language_matches(monkeypatch):
    """锚定 cantonese + partial cantonese（同语言）→ PREFLIGHT 照常发（行为零变化）。"""
    monkeypatch.delenv("QWEN3_ASR_PREFLIGHT_LANG_GATE", raising=False)
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))

    async def scenario():
        stream = _make_stream("cantonese")
        try:
            ev1 = await _drive_window(stream, {"text": "唔該幫我查吓", "language": "cantonese"})
            ev2 = await _drive_window(
                stream, {"text": "唔該幫我查吓件貨幾時到", "language": "cantonese"}
            )
            return ev1, ev2, stream._stable
        finally:
            await _close(stream)

    ev1, ev2, stable = asyncio.run(scenario())
    assert ev1 == ["INTERIM_TRANSCRIPT"]  # 窗 1 无稳定前缀，只有 INTERIM
    assert ev2 == ["INTERIM_TRANSCRIPT", "PREFLIGHT_TRANSCRIPT"], ev2
    assert stable == "唔該幫我查吓"


def test_lang_gate_env_zero_disables_gate(monkeypatch):
    """QWEN3_ASR_PREFLIGHT_LANG_GATE=0 → 关门，语言不匹配也照发（回退旧行为）。"""
    monkeypatch.setenv("QWEN3_ASR_PREFLIGHT_LANG_GATE", "0")
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))

    async def scenario():
        stream = _make_stream("cantonese")
        try:
            await _drive_window(stream, {"text": "hello how are", "language": "en"})
            ev2 = await _drive_window(
                stream, {"text": "hello how are you my friend", "language": "en"}
            )
            return ev2, stream._stable
        finally:
            await _close(stream)

    ev2, stable = asyncio.run(scenario())
    assert ev2 == ["INTERIM_TRANSCRIPT", "PREFLIGHT_TRANSCRIPT"], ev2
    assert stable == "hello how are"


def test_lang_gate_pinned_state_unchanged_by_final_path(monkeypatch):
    """FINAL 路径不动：门只挡 PREFLIGHT——锚定语言状态仍由 FINAL 正常更新（回归位）。"""
    state = LanguageState(lang="cantonese")
    state.update("en", "hello there my friend")  # FINAL 的 update 逻辑照旧强证据切换
    assert state.lang == "en"
