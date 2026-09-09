"""CachedTTS 包装层单测:命中/未命中落盘/beep 防毒化/空音色透传/stream 透传+首音频回调。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from livekit.agents import APIConnectOptions, tts  # noqa: E402

from agent_runtime.tts_cache import CachedTTS, TtsAudioCache  # noqa: E402

# 振幅 1000(> trim 静音门限 120),避免被首包静音修剪清零。
_LOUD = (1000).to_bytes(2, "little", signed=True) * 4800  # 0.2s@24k


class _FakeChunkedStream(tts.ChunkedStream):
    """合成 N 帧;可模拟 beep(置 _emitted_beep)。"""

    def __init__(self, tts_, text, *, n_frames=3, beep=False):
        super().__init__(tts=tts_, input_text=text, conn_options=APIConnectOptions(max_retry=0))
        self._n = n_frames
        self._beep = beep
        self._emitted_beep = False

    async def _run(self, output_emitter):
        output_emitter.initialize(
            request_id="fake",
            sample_rate=self._tts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
            stream=False,
        )
        if self._beep:
            self._emitted_beep = True
            return
        for _ in range(self._n):
            output_emitter.push(_LOUD)
        output_emitter.flush()


class _FakeTTS(tts.TTS):
    model = "fake"
    provider = "fake"

    def __init__(self, *, beep=False, voice="voice-a"):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=24000,
            num_channels=1,
        )
        self.beep = beep
        self._voice = voice
        self.synth_calls: list[str] = []

    def synthesize(self, text, *, conn_options=None):
        self.synth_calls.append(text)
        return _FakeChunkedStream(self, text, beep=self.beep)

    def resolved_voice(self) -> str:
        return self._voice

    def resolved_model(self) -> str:
        return "model-x"


def _cache(tmp_path) -> TtsAudioCache:
    return TtsAudioCache(root=tmp_path / "tts-cache", sample_rate=24000)


def _collect_chunks(make_stream, limit=64):
    """synthesize/stream 工厂必须在事件循环内调用(基类构造要起 task)。"""

    async def _run():
        frames = []
        async with make_stream() as s:
            async for ev in s:
                frames.append(ev.frame)
                if len(frames) >= limit:
                    break
        return frames

    return asyncio.run(_run())


def _wrapped(tmp_path, inner):
    cache = _cache(tmp_path)
    return CachedTTS(
        inner, cache=cache, voice_provider=inner.resolved_voice, model_provider=inner.resolved_model
    ), cache


def test_hit_serves_cached_pcm_without_inner_call(tmp_path):
    inner = _FakeTTS()
    wrapped, cache = _wrapped(tmp_path, inner)
    key = cache.key_for("你好", voice="voice-a", model="model-x")
    cache.store(key, _LOUD, text="你好", voice="voice-a", model="model-x")
    frames = _collect_chunks(lambda: wrapped.synthesize("你好"))
    assert frames, "cached stream must yield frames"
    assert inner.synth_calls == []  # 未打内芯
    total = sum(f.samples_per_channel for f in frames)
    # emitter 会把尾部对齐到 10ms 量子(240 样本@24k),允许一个量子的补齐误差
    assert abs(total - len(_LOUD) // 2) <= 240


def test_miss_streams_then_stores(tmp_path):
    inner = _FakeTTS()
    wrapped, cache = _wrapped(tmp_path, inner)
    frames = _collect_chunks(lambda: wrapped.synthesize("第一通"))
    assert frames, "未命中照常出声"
    key = cache.key_for("第一通", voice="voice-a", model="model-x")
    assert cache.get(key) is not None, "完整消费后必须落盘"
    frames2 = _collect_chunks(lambda: wrapped.synthesize("第一通"))
    assert frames2
    assert inner.synth_calls == ["第一通"], "第二通命中,内芯不再被调"


def test_beep_never_stored(tmp_path):
    from livekit.agents import APIError

    inner = _FakeTTS(beep=True)
    wrapped, cache = _wrapped(tmp_path, inner)
    try:
        _collect_chunks(lambda: wrapped.synthesize("坏档"))
    except APIError:
        pass  # beep 分支零音频,基类会抛 no-audio——两条路径都不允许落盘
    key = cache.key_for("坏档", voice="voice-a", model="model-x")
    assert cache.get(key) is None, "beep 音频绝不能入缓存"


def test_empty_voice_passthrough_no_store(tmp_path):
    inner = _FakeTTS(voice="")
    wrapped, cache = _wrapped(tmp_path, inner)
    frames = _collect_chunks(lambda: wrapped.synthesize("无音色"))
    assert frames  # 内芯照常出声(透传)
    assert list(cache.root.glob("*.pcm")) == [] if cache.root.exists() else True
    assert cache.get(cache.key_for("无音色", voice="", model="model-x")) is None


def test_stream_relay_forwards_and_fires_first_audio_once(tmp_path):
    class _FakeSynStream(tts.SynthesizeStream):
        def __init__(self, tts_):
            super().__init__(tts=tts_, conn_options=APIConnectOptions(max_retry=0))
            self.pushed: list[str] = []

        async def _run(self, output_emitter):
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    continue
                self.pushed.append(str(item))
            output_emitter.initialize(
                request_id="fake",
                sample_rate=self._tts.sample_rate,
                num_channels=1,
                mime_type="audio/pcm",
                stream=True,
            )
            output_emitter.start_segment(segment_id="s1")
            output_emitter.push(_LOUD)
            output_emitter.flush()

    class _StreamTTS(_FakeTTS):
        def __init__(self):
            super().__init__()
            self.last_stream: _FakeSynStream | None = None

        def stream(self, *, conn_options=None):
            self.last_stream = _FakeSynStream(self)
            return self.last_stream

    inner = _StreamTTS()
    wrapped, _cache2 = _wrapped(tmp_path, inner)
    fired = {"n": 0}
    wrapped.add_first_audio_listener(lambda: fired.__setitem__("n", fired["n"] + 1))

    async def _run():
        out = []
        async with wrapped.stream() as s:
            s.push_text("你")
            s.push_text("好")
            s.flush()
            s.end_input()
            async for ev in s:
                out.append(ev.frame)
        return out

    frames = asyncio.run(_run())
    assert frames, "透传音频必须出帧"
    assert inner.last_stream is not None and inner.last_stream.pushed == ["你", "好"], "输入逐条透传"
    assert fired["n"] == 1, "首音频回调只触发一次"
