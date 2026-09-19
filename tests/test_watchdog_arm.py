"""D2(2026-09-20)修复单测:缓存装配失败时看门狗/垫话首音频联动整通哑掉。

根因:首音频信号口(add_first_audio_listener/set_hold_provider)只有 CachedTTS
提供——_tts_cache=None(BOK_TTS_CACHE=0 或 TtsAudioCache 装配抛异常)时 provider
是裸 FallbackAdapter/裸 MiniMax:看门狗永不武装、垫话永不撤表/扣压,仅一行
init failed 日志零告警。修法三层:
1. _FirstAudioTTS 薄透传(零缓存逻辑):stream() 复用 _RelaySynthesizeStream,
   首帧 fire 回调+询问 hold_provider;synthesize() 纯透传不 fire(直念族各
   调用点先显式 cancel 看门狗、后台补物化 _filler_backfill 不可误拆在途轮)。
2. wrap_first_audio_tts=装配点唯一入口(幂等,CachedTTS 在场原样返回)。
3. agent._has_first_audio_signal(接口存在性)取代 isinstance(…, CachedTTS)
   武装/接线判定;本地链(无信号口)照旧不武装。

纯单测/asyncio,不起真栈。CachedTTS 既有行为由 tests/test_tts_cache_first_audio.py
与 test_cached_tts.py 钉死,本文件另钉「在场时包装零变化」恒等断言。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from livekit.agents import APIConnectOptions, tts  # noqa: E402

from agent_runtime.agent import _has_first_audio_signal  # noqa: E402
from agent_runtime.tts_cache import (  # noqa: E402
    CachedTTS,
    TtsAudioCache,
    _FirstAudioTTS,
    wrap_first_audio_tts,
)

# 振幅 1000(> trim 静音门限 120),避免被首包静音修剪清零。
_LOUD = (1000).to_bytes(2, "little", signed=True) * 4800  # 0.2s@24k


class _FakeChunkedStream(tts.ChunkedStream):
    """synthesize 替身:合成 N 帧。"""

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
    """裸 provider 替身(= 缓存缺席路径的 FallbackAdapter/MiniMax 姿势:
    无 add_first_audio_listener 接口)。记录 conn_options 以钉归一化。"""

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
        self.synth_conn: list = []
        self.stream_conn: list = []

    def synthesize(self, text, *, conn_options=None):
        self.synth_calls.append(text)
        self.synth_conn.append(conn_options)
        return _FakeChunkedStream(self, text)

    def resolved_voice(self) -> str:
        return self._voice

    def resolved_model(self) -> str:
        return "model-x"


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
        self.stream_conn.append(conn_options)
        self.last_stream = _FakeSynStream(self)
        return self.last_stream


def _cache(tmp_path: Path) -> TtsAudioCache:
    return TtsAudioCache(root=tmp_path / "tts-cache", sample_rate=24000)


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


def _consume_stream(wrapped, *, texts=("你",)) -> list:
    """stream 消费器:必须先喂文本并 end_input 再迭代——不关输入通道会让
    内芯/转发任务永不结束(死锁;与 test_cached_tts 同款姿势),wait_for 兜底。"""

    async def _run():
        out = []
        async with wrapped.stream() as s:
            for t in texts:
                s.push_text(t)
            s.flush()
            s.end_input()
            async for ev in s:
                out.append(ev.frame)
        return out

    return asyncio.run(asyncio.wait_for(_run(), 10))


# ---- ① 缓存不可用路径:provider 被包装 + 武装闸放行 ----


def test_cache_unavailable_wraps_bare_provider():
    # _tts_cache=None(装配失败/BOK_TTS_CACHE=0)姿势:裸 provider 进包装层,
    # 获得首音频信号口,内芯仍收到 synthesize(透传不吞调用)。
    inner = _FakeTTS()
    wrapped = wrap_first_audio_tts(inner, None)
    assert isinstance(wrapped, _FirstAudioTTS), "缓存缺席必须包 _FirstAudioTTS"
    assert wrapped is not inner
    assert hasattr(wrapped, "add_first_audio_listener"), "包装层必须带首音频信号口"
    frames = _consume(lambda: wrapped.synthesize("你好"))
    assert frames and inner.synth_calls == ["你好"], "synthesize 透传内芯"


def test_cache_unavailable_enables_arm_gate():
    # _arm_response_watchdog 的可测缝:武装判定从 isinstance(CachedTTS) 换成
    # 接口存在性——包装后的 provider 闸放行(=看门狗会武装),裸 provider 不放行
    # (=本地链保旧不武装),CachedTTS 照旧放行。
    inner = _FakeTTS()
    assert _has_first_audio_signal(inner) is False, "裸 provider(本地链)照旧不武装"
    assert _has_first_audio_signal(wrap_first_audio_tts(inner, None)) is True
    cache = TtsAudioCache(root=Path("/tmp"), sample_rate=24000)  # 不触盘,只借构造
    cached = CachedTTS(inner, cache=cache, voice_provider=inner.resolved_voice)
    assert _has_first_audio_signal(cached) is True, "CachedTTS 在场照旧武装"


def test_arm_gate_signature_free():
    # 判定只认接口不认类型:任意对象带 add_first_audio_listener 即放行。
    assert _has_first_audio_signal(object()) is False

    class _With:
        def add_first_audio_listener(self, cb) -> None:
            pass

    assert _has_first_audio_signal(_With()) is True


# ---- ② 流式路径:首音频帧触发回调(假 provider 流 1 帧)----


def test_stream_first_frame_fires_callback_once():
    inner = _StreamTTS()
    wrapped = _FirstAudioTTS(inner)
    fired = {"n": 0}
    wrapped.add_first_audio_listener(lambda: fired.__setitem__("n", fired["n"] + 1))

    frames = _consume_stream(wrapped)
    assert frames, "透传音频必须出帧"
    assert inner.last_stream is not None and inner.last_stream.pushed == ["你"], "输入透传内芯"
    assert fired["n"] == 1, "流式首帧 fire 恰好一次(看门狗拆弹/垫话撤表信号)"


def test_stream_forwards_input_and_fires_once():
    inner = _StreamTTS()
    wrapped = _FirstAudioTTS(inner)
    fired = {"n": 0}
    wrapped.add_first_audio_listener(lambda: fired.__setitem__("n", fired["n"] + 1))

    frames = _consume_stream(wrapped, texts=("你", "好"))
    assert frames
    assert inner.last_stream.pushed == ["你", "好"], "输入逐条透传"
    assert fired["n"] == 1


def test_stream_consults_hold_provider_on_first_frame():
    # 播放排序契约:首帧到达询问 hold_provider(垫话剩余+gap),不吞不丢。
    inner = _StreamTTS()
    wrapped = _FirstAudioTTS(inner)
    asked = {"n": 0}

    def _hold() -> float:
        asked["n"] += 1
        return 0.0  # 垫话未播=不扣压,只验接线

    wrapped.set_hold_provider(_hold)
    frames = _consume_stream(wrapped)
    assert frames
    assert asked["n"] == 1, "首帧必须询问垫话扣压一次"


def test_stream_two_listeners_both_fire():
    # 装配形状(垫话撤表+看门狗拆弹两个回调)必须都收到信号。
    inner = _StreamTTS()
    wrapped = _FirstAudioTTS(inner)
    hits: list[str] = []
    wrapped.add_first_audio_listener(lambda: hits.append("filler"))
    wrapped.add_first_audio_listener(lambda: hits.append("watchdog"))
    _consume_stream(wrapped)
    assert hits == ["filler", "watchdog"], hits


def test_listener_exception_never_breaks_stream():
    # 回调炸了绝不出声失败(同 CachedTTS._fire_first_audio 吞异常纪律)。
    inner = _StreamTTS()
    wrapped = _FirstAudioTTS(inner)

    def _boom() -> None:
        raise RuntimeError("listener down")

    wrapped.add_first_audio_listener(_boom)
    frames = _consume_stream(wrapped)
    assert frames, "回调失败唔阻播放"


# ---- ②b synthesize:纯透传不 fire(后台 backfill 不可误拆在途看门狗)----


def test_synthesize_is_passthrough_and_never_fires():
    inner = _FakeTTS()
    wrapped = _FirstAudioTTS(inner)
    fired = {"n": 0}
    wrapped.add_first_audio_listener(lambda: fired.__setitem__("n", fired["n"] + 1))

    frames = _consume(lambda: wrapped.synthesize("第一通"))
    assert frames
    assert fired["n"] == 0, "synthesize 不 fire(直念族显式拆弹/后台 backfill 防误拆)"
    assert inner.synth_calls == ["第一通"]


def test_conn_options_none_normalized():
    # 内芯 FallbackAdapter 的流会读 conn_options.max_retry——透传 None 直接
    # AttributeError(与 CachedTTS._norm_conn_options 同款 hazard)。
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

    inner = _FakeTTS()
    wrapped = _FirstAudioTTS(inner)
    _consume(lambda: wrapped.synthesize("你好", conn_options=None))
    assert inner.synth_conn and inner.synth_conn[0] is DEFAULT_API_CONNECT_OPTIONS


# ---- ③ CachedTTS 在场:装配零变化 ----


def test_cached_tts_in_place_identity(tmp_path):
    # 缓存装配成功姿势:wrap 原样返回 CachedTTS(身份不变),不叠层。
    inner = _FakeTTS()
    cache = _cache(tmp_path)
    cached = CachedTTS(
        inner, cache=cache, voice_provider=inner.resolved_voice, model_provider=inner.resolved_model
    )
    assert wrap_first_audio_tts(cached, cache) is cached
    assert wrap_first_audio_tts(cached, None) is cached, "已带信号口=幂等"


def test_wrap_none_provider_safe():
    # provider 为空(理论分支)不炸,原样返回。
    assert wrap_first_audio_tts(None, None) is None
