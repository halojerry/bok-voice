from __future__ import annotations

from bok_voice_core.policies import ProviderRegistry

from .fakes import FakeASR, FakeLLM, FakeTTS, FakeVAD


def build_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("vad", "livekit", FakeVAD())
    registry.register("vad", "silero", FakeVAD())
    registry.register("asr", "sherpa", FakeASR())
    registry.register("asr", "iflytek", FakeASR())
    registry.register("asr", "volcano", FakeASR())
    registry.register("llm", "local_openai", FakeLLM())
    registry.register("llm", "deepseek", FakeLLM())
    registry.register("asr", "qwen3_asr", FakeASR())
    registry.register("tts", "qwen3_tts", FakeTTS())
    registry.register("tts", "volcano_streaming", FakeTTS())
    return registry
