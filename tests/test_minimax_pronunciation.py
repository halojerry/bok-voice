"""MiniMax TTS 按通话注入发音词典（pronunciation_dict）单测。

覆盖两条 task_start 构造点（bidi `_task_start_payload` / classic 两条内联
`_handshake`）与 HTTP 整段 payload：非空 pronunciation → 载荷含
`pronunciation_dict.tone`；空 → 完全不含该键（现行为零变化）。

替身/直接构造，无网络（构造函数轻量已验证）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    MiniMaxTTS,
    _MiniMaxSynthesizeStream,
    _MiniMaxTTSStream,
)

_ENTRIES = ["陳大文/(can4)(daai6)(man4)", "张伟/(zhang1)(wei3)"]


def _make_tts(pronunciation=None):
    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse", "cantonese": "Cantonese_Male_news_anchor_vv2"},
        sample_rate=24000,
        api_key="test" + "-key",
        pronunciation=pronunciation,
    )


def test_task_start_payload_has_pronunciation_dict():
    """bidi task_start 载荷（_task_start_payload）：非空 → 带 tone 条目。"""
    tts = _make_tts(_ENTRIES)
    payload = tts._task_start_payload("male-qn-qingse", 24000)
    assert payload["pronunciation_dict"] == {"tone": _ENTRIES}


def test_task_start_payload_omits_key_when_empty():
    """空/None → 完全不含 pronunciation_dict 键（现行为零变化）。"""
    for arg in (None, [], [""], ["   "]):
        payload = _make_tts(arg)._task_start_payload("male-qn-qingse", 24000)
        assert "pronunciation_dict" not in payload, arg


def test_ctor_drops_blank_entries():
    """构造层只清空串/纯空白，拼法原文（括号/空格）原样保留。"""
    tts = _make_tts(["", "   ", "陳大文/(can4)(daai6)(man4)"])
    payload = tts._task_start_payload("v", 24000)
    assert payload["pronunciation_dict"]["tone"] == ["陳大文/(can4)(daai6)(man4)"]


def _fake_ws(sent: list[dict]):
    """recv 脚本：connected_success → task_started → 抛错（结束流）。"""

    class FakeWS:
        def __init__(self):
            self._n = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            self._n += 1
            if self._n == 1:
                return json.dumps({"event": "connected_success"})
            if self._n == 2:
                return json.dumps({"event": "task_started"})
            raise Exception("fake ws closed")

        async def send(self, payload):
            sent.append(json.loads(payload))

        async def close(self):
            pass

    return FakeWS()


def _drive_synth_stream(tts, sent):
    async def run():
        from livekit.agents import APIConnectOptions

        s = _MiniMaxSynthesizeStream(tts, APIConnectOptions())
        s.push_text("你好。")
        s.end_input()
        for _ in range(100):
            if any(m.get("event") == "task_start" for m in sent):
                break
            await asyncio.sleep(0.05)
        s._task.cancel()

    asyncio.run(run())


def test_classic_synthesize_stream_handshake_carries_pronunciation(monkeypatch):
    """classic 真流式（_MiniMaxSynthesizeStream._handshake）task_start 带词典。"""
    monkeypatch.setenv("MINIMAX_WS_POOL", "0")  # 禁热连接池：每测试恒全新连接
    sent: list[dict] = []
    monkeypatch.setattr("websockets.connect", lambda *a, **kw: _coro(_fake_ws(sent)))
    _drive_synth_stream(_make_tts(_ENTRIES), sent)
    starts = [m for m in sent if m.get("event") == "task_start"]
    assert starts, sent
    assert starts[0]["pronunciation_dict"] == {"tone": _ENTRIES}


def test_classic_synthesize_stream_handshake_omits_when_empty(monkeypatch):
    monkeypatch.setenv("MINIMAX_WS_POOL", "0")
    sent: list[dict] = []
    monkeypatch.setattr("websockets.connect", lambda *a, **kw: _coro(_fake_ws(sent)))
    _drive_synth_stream(_make_tts(None), sent)
    starts = [m for m in sent if m.get("event") == "task_start"]
    assert starts and "pronunciation_dict" not in starts[0]


async def _coro(value):
    return value


class _RecordingEmitter:
    def __init__(self):
        self.pushed = 0
        self.ended = False

    def initialize(self, **kw):
        pass

    def start_segment(self, **kw):
        pass

    def push(self, data):
        self.pushed += len(data)

    def flush(self):
        pass

    def end_segment(self):
        self.ended = True


def test_classic_chunked_run_ws_handshake_carries_pronunciation(monkeypatch):
    """classic 整段（_MiniMaxTTSStream._run_ws._handshake）task_start 带词典。

    与 SynthesizeStream 是两条独立内联 handshake，共用 _apply_pronunciation 单点。
    """
    monkeypatch.setenv("MINIMAX_WS_POOL", "0")
    sent: list[dict] = []
    audio = "00" * 2000  # ≥200ms 帧门槛

    class FakeWS:
        def __init__(self):
            self._script = [
                json.dumps({"event": "connected_success"}),
                json.dumps({"event": "task_started"}),
                json.dumps({"data": {"audio": audio}, "is_final": True}),
            ]

        async def recv(self):
            if self._script:
                return self._script.pop(0)
            raise Exception("fake ws closed")

        async def send(self, payload):
            sent.append(json.loads(payload))

        async def close(self):
            pass

    monkeypatch.setattr("websockets.connect", lambda *a, **kw: _coro(FakeWS()))
    tts = _make_tts(_ENTRIES)

    async def run():
        from livekit.agents import APIConnectOptions

        stream = _MiniMaxTTSStream(tts, "你好", APIConnectOptions())
        ok = await stream._run_ws(_RecordingEmitter(), "k", "male-qn-qingse", 24000)
        assert ok is True

    asyncio.run(run())
    starts = [m for m in sent if m.get("event") == "task_start"]
    assert starts, sent
    assert starts[0]["pronunciation_dict"] == {"tone": _ENTRIES}


def _fake_http_client(captured: dict):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"audio": "00" * 200}}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(json or {})
            return _Resp()

    return _Client


def test_http_synth_payload_carries_pronunciation(monkeypatch):
    """HTTP 整段兜底（_minimax_http_synth）payload 带词典。"""
    captured: dict = {}
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_fake_http_client(captured)))
    tts = _make_tts(_ENTRIES)

    async def run():
        ok = await lp._minimax_http_synth(
            tts, "你好", _RecordingEmitter(), key="k", voice="male-qn-qingse", sample_rate=24000
        )
        assert ok is True

    asyncio.run(run())
    assert captured["pronunciation_dict"] == {"tone": _ENTRIES}


def test_http_synth_payload_omits_key_when_empty(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_fake_http_client(captured)))
    tts = _make_tts(None)

    async def run():
        await lp._minimax_http_synth(
            tts, "你好", _RecordingEmitter(), key="k", voice="v", sample_rate=24000
        )

    asyncio.run(run())
    assert "pronunciation_dict" not in captured
