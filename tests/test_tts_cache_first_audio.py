"""缓存命中路径首音频回调(RC2,2026-09-17):watchdog 盲区修复。

CachedTTS.synthesize 缓存 hit 走 _CachedChunkedStream——此前只推帧不 fire
on_first_audio,watchdog/垫话排序对缓存命中直念隐身,4s force-interrupt 会
掐死在途真回复。本文件钉死:hit 首帧 fire 恰好一次、miss 行为不变、
纯后台读(不传回调)零变化。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from livekit.agents import APIConnectOptions, tts  # noqa: E402

from agent_runtime.tts_cache import CachedTTS, TtsAudioCache  # noqa: E402

# 振幅 1000(> trim 静音门限 120),避免被首包静音修剪清零。
_LOUD = (1000).to_bytes(2, "little", signed=True) * 4800  # 0.2s@24k


class _FakeChunkedStream(tts.ChunkedStream):
    """合成 N 帧(未命中路径的内芯替身)。"""

    def __init__(self, tts_, text, *, n_frames=3):
        super().__init__(tts=tts_, input_text=text, conn_options=APIConnectOptions(max_retry=0))
        self._n = n_frames

    async def _run(self, output_emitter):
        output_emitter.initialize(
            request_id="fake",
            sample_rate=self._tts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
            stream=False,
        )
        for _ in range(self._n):
            output_emitter.push(_LOUD)
        output_emitter.flush()


class _FakeTTS(tts.TTS):
    model = "fake"
    provider = "fake"

    def __init__(self, *, voice="voice-a"):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._voice = voice
        self.synth_calls: list[str] = []

    def synthesize(self, text, *, conn_options=None):
        self.synth_calls.append(text)
        return _FakeChunkedStream(self, text)

    def resolved_voice(self) -> str:
        return self._voice

    def resolved_model(self) -> str:
        return "model-x"


def _cache(tmp_path: Path) -> TtsAudioCache:
    return TtsAudioCache(root=tmp_path / "tts-cache", sample_rate=24000)


def _wrapped(tmp_path: Path, inner: _FakeTTS) -> tuple[CachedTTS, TtsAudioCache]:
    cache = _cache(tmp_path)
    return (
        CachedTTS(
            inner,
            cache=cache,
            voice_provider=inner.resolved_voice,
            model_provider=inner.resolved_model,
        ),
        cache,
    )


def _consume(make_stream, limit=64) -> list:
    """synthesize 工厂必须在事件循环内调用(基类构造要起 task)。"""

    async def _run():
        frames = []
        async with make_stream() as s:
            async for ev in s:
                frames.append(ev.frame)
                if len(frames) >= limit:
                    break
        return frames

    return asyncio.run(_run())


def _fire_counter() -> tuple[dict, Callable[[], None]]:
    fired = {"n": 0}

    def _cb() -> None:
        fired["n"] += 1

    return fired, _cb


def test_cache_hit_fires_first_audio_exactly_once(tmp_path):
    # RC2 主断言:hit 路径消费流时监听器恰好 fire 一次(此前恒 0=watchdog 盲区)。
    inner = _FakeTTS()
    wrapped, cache = _wrapped(tmp_path, inner)
    key = cache.key_for("你好", voice="voice-a", model="model-x")
    cache.store(key, _LOUD, text="你好", voice="voice-a", model="model-x")
    fired, cb = _fire_counter()
    wrapped.add_first_audio_listener(cb)

    frames = _consume(lambda: wrapped.synthesize("你好"))
    assert frames, "cached stream must yield frames"
    assert inner.synth_calls == [], "命中唔打内芯"
    assert fired["n"] == 1, "缓存命中首帧必须 fire 首音频回调恰好一次"


def test_cache_hit_repeat_consumption_fires_each_play_once(tmp_path):
    # 同一条目播两次(两轮同文直念)=两次独立播放,各 fire 一次(流级幂等,
    # 唔係全局幂等)。
    inner = _FakeTTS()
    wrapped, cache = _wrapped(tmp_path, inner)
    key = cache.key_for("你好", voice="voice-a", model="model-x")
    cache.store(key, _LOUD, text="你好", voice="voice-a", model="model-x")
    fired, cb = _fire_counter()
    wrapped.add_first_audio_listener(cb)

    _consume(lambda: wrapped.synthesize("你好"))
    _consume(lambda: wrapped.synthesize("你好"))
    assert fired["n"] == 2
    assert inner.synth_calls == []


def test_cache_miss_still_no_fire_via_synthesize(tmp_path):
    # miss 路径(_StoreChunkedStream tee)行为不变:唔 fire(与修复前一致)。
    inner = _FakeTTS()
    wrapped, _cache2 = _wrapped(tmp_path, inner)
    fired, cb = _fire_counter()
    wrapped.add_first_audio_listener(cb)

    frames = _consume(lambda: wrapped.synthesize("第一通"))
    assert frames, "未命中照常出声"
    assert fired["n"] == 0, "synthesize miss 路径行为不变(不 fire)"


def test_background_read_without_listener_is_unchanged(tmp_path):
    # 纯后台读姿势(不注册监听器/直接构造不传回调):零行为变化,fire 不炸。
    from agent_runtime.tts_cache import _CachedChunkedStream

    inner = _FakeTTS()
    wrapped, cache = _wrapped(tmp_path, inner)
    key = cache.key_for("后台读", voice="voice-a", model="model-x")
    cache.store(key, _LOUD, text="后台读", voice="voice-a", model="model-x")

    fired, cb = _fire_counter()

    async def _run():
        frames = []
        # 直接构造、不传 on_first_audio(垫话 backfill 同款姿势)
        stream = _CachedChunkedStream(tts_=wrapped, text="后台读", pcm=cache.get(key))
        async with stream as s:
            async for ev in s:
                frames.append(ev.frame)
        return frames

    frames = asyncio.run(_run())
    assert frames
    assert fired["n"] == 0, "不传回调=零变化"
