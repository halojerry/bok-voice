"""A-line regression tests for the native Ollama adapter and LanguageState.

Regression target (P0): Qwen3.x models default to thinking mode via the
OpenAI-compat endpoint, which burns the token budget on `reasoning` and leaves
the reply empty/slow.  The adapter must call the native /api/chat endpoint with
`think=false` + a bounded `num_predict`.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from agent_runtime.providers.livekit_plugins import LanguageState, OllamaLLM


class _FakeStreamResponse:
    def __init__(self, lines: list[str]):
        self._lines = lines

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        pass


class _FakeClient:
    def __init__(self, lines: list[str]):
        self._lines = lines
        self.calls: list[tuple[str, str, dict]] = []

    def stream(self, method: str, url: str, *, json: dict | None = None, **kw):
        # 与 httpx.AsyncClient.stream() 一致：返回同步 context manager。
        self.calls.append((method, url, json))
        return _Ctx(_FakeStreamResponse(self._lines))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def _ndjson(*contents: str) -> list[str]:
    return [
        json.dumps({"model": "m", "message": {"role": "assistant", "content": c}}, ensure_ascii=False)
        for c in contents
    ] + [json.dumps({"model": "m", "message": {"role": "assistant", "content": ""}, "done": True})]


def test_ollama_native_stream_emits_content_chunks():
    plugin = OllamaLLM(base_url="http://127.0.0.1:11434/v1")
    fake = _FakeClient(_ndjson("你好", "！"))

    from agent_runtime.providers.livekit_plugins import _chat_messages
    from livekit.agents import llm

    chat_ctx = llm.ChatContext()
    chat_ctx.add_message(role="user", content=["你好"])

    async def _run():
        with patch("agent_runtime.providers.livekit_plugins.httpx.AsyncClient", return_value=fake):
            stream = plugin.chat(chat_ctx=chat_ctx)
            chunks = []
            async for chunk in stream:
                chunks.append(chunk.delta.content)
        return chunks

    chunks = asyncio.run(_run())

    assert "".join(chunks) == "你好！"
    assert fake.calls
    method, url, body = fake.calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:11434/api/chat"  # /v1 normalized away
    assert body["think"] is False
    assert body["options"]["num_predict"] > 0
    assert body["messages"][-1]["content"] == "你好"


def test_language_state_mapping():
    state = LanguageState()
    for raw, want in [
        ("Chinese", "zh"),
        ("Mandarin", "zh"),
        ("Cantonese", "yue"),
        ("English", "en"),
        # 空/未知语言不覆盖上一轮的语言状态
        (None, "en"),
        ("", "en"),
    ]:
        state.update(raw)
        assert state.lang == want, (raw, want)
