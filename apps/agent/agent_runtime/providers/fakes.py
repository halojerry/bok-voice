from __future__ import annotations

from typing import AsyncIterator

from bok_voice_core.providers import AudioEvent, LLMEvent, TranscriptionEvent
from bok_voice_core.types import ContextBundle


class FakeVAD:
    def detect_segments(self, audio: bytes) -> list[dict]:
        return [{"start": 0.0, "end": 0.5, "is_speech": bool(audio)}]


class FakeASR:
    async def transcribe(self, audio: bytes, language: str = "zh") -> TranscriptionEvent:
        return TranscriptionEvent(text="这是一段测试转写", emotion="neutral", is_final=True)


class FakeLLM:
    def stream_chat(self, context: ContextBundle) -> AsyncIterator[LLMEvent]:
        async def _gen():
            yield LLMEvent(text="你好，", emotion="friendly")
            yield LLMEvent(text="很高兴为您服务。", emotion="friendly", done=True)

        return _gen()


class FakeTTS:
    async def synthesize(self, text: str, voice: str = "", emotion: str = "") -> list[AudioEvent]:
        return [AudioEvent(chunk=text.encode("utf-8"), is_final=True)]
