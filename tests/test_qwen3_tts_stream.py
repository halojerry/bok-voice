"""Qwen3TTSTTS 真流式(streaming=True + SynthesizeStream)契约与增量语义。

对齐 test_minimax_stream.py 的防回归套路:
1. capabilities.streaming 必须为 True —— livekit 才会走 stream()(SynthesizeStream),
   而不是被 StreamAdapter+SentenceTokenizer 包(等整句边界,中文切句不可靠,
   实测首段文本多等 150-790ms)。
2. stream() 返回 SynthesizeStream;synthesize() 仍回 ChunkedStream(兼容路径)。
3. 增量语义:按句切任务,一句一次 POST(与 synthesize 同端点同 JSON);
   连续数字串唔被切开(WhatsApp 单号安全)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from agent_runtime.providers.livekit_plugins import Qwen3TTSTTS  # noqa: E402

_PCM = b"\x01\x02" * 1000  # 2000B 假音频(> 40ms 早推门槛,不足一整 200ms 帧)


def _make_tts():
    return Qwen3TTSTTS(
        voice={"zh": "zh-female", "cantonese": "cantonese-female"},
        sample_rate=24000,
    )


def _install_fake_sidecar(monkeypatch, posts: list[dict]):
    """把 livekit_plugins 的 httpx 换成 MockTransport 工厂:记录每次 POST 的 JSON。"""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        posts.append(json.loads(request.content.decode()))
        return httpx.Response(200, content=_PCM)

    real_async_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), timeout=120)

    import agent_runtime.providers.livekit_plugins as lk

    monkeypatch.setattr(lk, "httpx", SimpleNamespace(AsyncClient=factory))


def test_qwen3_tts_capabilities_streaming_true():
    # 真流式:streaming=True 让 voice 管线走 stream(),唔再包 StreamAdapter 句子切分。
    tts = _make_tts()
    assert tts.capabilities.streaming is True


def test_qwen3_stream_returns_synthesize_stream_and_synth_stays_chunked():
    from livekit.agents import tts as lk_tts

    tts = _make_tts()

    async def _make():
        synth = tts.synthesize("你好")
        stream = tts.stream()
        # SynthesizeStream 构造即起后台任务:测试收尾要取消,防悬挂警告。
        stream._task.cancel()
        return synth, stream

    synth, stream = asyncio.run(_make())
    assert isinstance(synth, lk_tts.ChunkedStream)
    assert not isinstance(synth, lk_tts.SynthesizeStream)
    assert isinstance(stream, lk_tts.SynthesizeStream)
    assert not isinstance(stream, lk_tts.ChunkedStream)


def test_qwen3_stream_posts_per_sentence(monkeypatch):
    """按句切任务:两句话 = 两次 POST,各自带完整请求 JSON(voice/情绪逐任务解析)。"""
    from agent_runtime.providers.livekit_plugins import _Qwen3SynthesizeStream

    posts: list[dict] = []
    _install_fake_sidecar(monkeypatch, posts)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = _Qwen3SynthesizeStream(tts, APIConnectOptions())
        s.push_text("你好。")
        s.push_text("我係林先生。")
        s.end_input()
        events = []
        async for ev in s:  # _run 完成后事件通道关闭,自然退出
            events.append(ev)
        return events

    events = asyncio.run(asyncio.wait_for(run(), timeout=10))
    inputs = [p["input"] for p in posts]
    assert inputs == ["你好。", "我係林先生。"], inputs
    # 请求契约与 synthesize 整段路径一致(sidecar 同端点同 JSON);
    # voice 每任务即时解析(LanguageState 缺省 lang=zh → zh 音色)。
    for p in posts:
        assert p["streaming"] is True and p["chunk_ms"] == 200
        assert p["voice"] == "zh-female"
        assert p["response_format"] == "pcm"
    # 至少推过一段音频(两句话各回一包 PCM)。
    assert len(events) >= 1


def test_qwen3_stream_never_splits_digit_runs(monkeypatch):
    """无句号的连续号码文本:整段一次 POST,唔被 overlap 切开腰斩单号。"""
    from agent_runtime.providers.livekit_plugins import _Qwen3SynthesizeStream

    posts: list[dict] = []
    _install_fake_sidecar(monkeypatch, posts)

    tts = _make_tts()
    text = "我想查下我張單12345678幾時送到"

    async def run():
        from livekit.agents import APIConnectOptions

        s = _Qwen3SynthesizeStream(tts, APIConnectOptions())
        s.push_text(text)
        s.end_input()
        async for _ in s:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert len(posts) == 1, [p["input"] for p in posts]
    assert posts[0]["input"] == text  # 全文一次合成,单号完整


def test_qwen3_chunked_stream_still_works(monkeypatch):
    """synthesize() 兼容路径回归:单任务单 POST + end_segment 由任务自己收。"""
    from agent_runtime.providers.livekit_plugins import _Qwen3TTSStream

    posts: list[dict] = []
    _install_fake_sidecar(monkeypatch, posts)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        synth = tts.synthesize("你好呀")
        # ChunkedStream 构造即起 _main_task(重试外环):走官方 async for 消费,
        # 唔直接调 _run(那会双跑,重试语义归 base 类管)。
        async for _ev in synth:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert [p["input"] for p in posts] == ["你好呀"]


def test_qwen3_stream_overlap_flushes_cjk_fragment(monkeypatch):
    """overlap 对 CJK 生效:≥12 字带软停顿的片段喺 end_input 前提前送出。

    旧 _flushable 用裸 isalpha()/isdigit() 拦尾——CJK 汉字 isalpha()==True,
    中文片段全被拦,overlap 对中文流量全死。
    """
    from agent_runtime.providers.livekit_plugins import _Qwen3SynthesizeStream

    posts: list[dict] = []
    _install_fake_sidecar(monkeypatch, posts)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = _Qwen3SynthesizeStream(tts, APIConnectOptions())
        s.push_text("今日天氣好好，")  # 7 字 < 12:唔送
        await asyncio.sleep(0.35)  # 超过默认 QWEN3_TTS_OVERLAP_MS=300
        s.push_text("我哋去飲茶啦")  # 凑够 13 字,软停顿喺后半 → 可送
        for _ in range(100):
            if posts:
                break
            await asyncio.sleep(0.05)
        assert posts, "CJK 片段应喺 end_input 前由 overlap 提前送出(唔好等全文)"
        s.end_input()
        async for _ev in s:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert [p["input"] for p in posts] == ["今日天氣好好，我哋去飲茶啦"]


def test_qwen3_stream_still_blocks_ascii_digit_run_tail(monkeypatch):
    """ascii 数字串结尾照旧拦住:唔好把单号腰斩(修复唔准放宽呢个保护)。"""
    from agent_runtime.providers.livekit_plugins import _Qwen3SynthesizeStream

    posts: list[dict] = []
    _install_fake_sidecar(monkeypatch, posts)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = _Qwen3SynthesizeStream(tts, APIConnectOptions())
        s.push_text("你個單號就係，")
        await asyncio.sleep(0.35)
        s.push_text("1234567890 幫你查")  # 之前片段以数字结尾 → 呢刻唔应提前送
        for _ in range(40):
            if posts:
                break
            await asyncio.sleep(0.05)
        # 提前送出的片段若以数字结尾,应该被拦(收尾残句先一次过送)。
        frag = posts[0]["input"] if posts else ""
        assert not frag.rstrip("。！？!?，、；;：: \t")[-1:].isascii(), frag
        s.end_input()
        async for _ev in s:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_qwen3_beep_initializes_emitter_before_push():
    """beep 前必须 initialize+start_segment:真 AudioEmitter.push 喺未启动时抛
    "AudioEmitter isn't started"(tts.py:900),旧实现被上层静默吞掉 → 静音。"""
    import agent_runtime.providers.livekit_plugins as lk

    class _StrictEmitter:
        def __init__(self):
            self.started = False
            self.frames: list[bytes] = []

        def initialize(self, **kw):
            self.started = True

        def start_segment(self, **kw):
            assert self.started, "start_segment before initialize"

        def push(self, data):
            assert self.started, "push before initialize == 真 emitter 会炸"
            self.frames.append(data)

        def flush(self):
            pass

        def end_segment(self, **kw):
            pass

    em = _StrictEmitter()
    asyncio.run(asyncio.wait_for(lk._qwen3_tts_beep(_make_tts(), em), timeout=5))
    assert em.frames, "beep 应有音频推出"


def test_qwen3_post_frames_no_retry_after_partial_audio(monkeypatch):
    """半途断流:本句已有音频落地 → 唔重试(重 POST 会从 byte 0 重推同句=重读)。

    唔使真 httpx:post() 会缓冲 body,网络级断流喺 post() 内抛(推帧之前),
    模拟唔到「推咗一半先断」。用假 client 返多 chunk 流式响应 + emitter 喺
    第 2 次 push 先爆,精确复现「音频已落地后下游断」= pushed_any 门要拦的姿势。
    """
    import agent_runtime.providers.livekit_plugins as lk

    attempts = {"n": 0}

    class _FakeResp:
        def raise_for_status(self):
            pass

        async def aiter_bytes(self):
            yield b"\x01\x02" * 4800  # 9600B 整帧:early push#1 落地
            yield b"\x01\x02" * 4800  # 凑满下一帧:push#2 爆
            raise RuntimeError("mid-stream cut")

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            attempts["n"] += 1
            return _FakeResp()

    monkeypatch.setattr(
        lk, "httpx", SimpleNamespace(AsyncClient=lambda **kw: _FakeClient())
    )

    class _BreaksMidway(_RecordingEmitter):
        def __init__(self):
            self.pushes = 0

        def push(self, data):
            self.pushes += 1
            if self.pushes >= 2:
                raise RuntimeError("downstream broke after audio flowed")

    tts = _make_tts()
    em = _BreaksMidway()
    state = {"started": False}

    async def run():
        return await lk._qwen3_tts_post_frames(tts, "你好", em, state)

    ok = asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert em.pushes >= 1, "前置条件:至少一次音频已落地"
    assert attempts["n"] == 1, "有音频落地后必须中止重试"
    assert ok is False


def test_qwen3_post_frames_retries_when_no_audio_flowed(monkeypatch):
    """零音频时重试语义保持:3 次尝试先放弃(修复唔准砍掉正常重试)。"""
    import agent_runtime.providers.livekit_plugins as lk

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500)

    real_async_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), timeout=120)

    monkeypatch.setattr(lk, "httpx", SimpleNamespace(AsyncClient=factory))

    tts = _make_tts()

    async def run():
        return await lk._qwen3_tts_post_frames(tts, "你好", _RecordingEmitter(), {"started": False})

    ok = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert attempts["n"] == 3
    assert ok is False


def test_qwen3_chunked_no_beep_after_audio_flowed(monkeypatch):
    """整段兼容路径 beep 门:_qwen3_tts_post_frames 失败时,只有 state["started"]
    仍为 False(全场零音频)先补 beep;音频出过再失败唔叠 beep。

    直调 _run(绕过 _main_task 的 end_input/no-audio 外环,呢啲系框架语义,
    唔喺呢个单测范围)。
    """
    import agent_runtime.providers.livekit_plugins as lk

    beeps: list[bool] = []

    async def fake_beep(tts_, output_emitter):
        beeps.append(True)

    monkeypatch.setattr(lk, "_qwen3_tts_beep", fake_beep)

    tts = _make_tts()

    class _StubEmitter(_RecordingEmitter):
        def __init__(self):
            self.started = False

        def initialize(self, **kwargs):
            self.started = True

    async def drive(started_after: bool):
        async def fake_post(tts_, text, output_emitter, state, *, end_segment=True):
            # 契约对齐真 helper:有音频落地 = emitter 已 initialize(state=True);
            # 唔 initialize 就翻 state 会让 _run finally 的 flush 喺未启动 emitter 上炸。
            if started_after:
                output_emitter.initialize(
                    request_id="stub",
                    sample_rate=tts_.sample_rate,
                    num_channels=tts_.num_channels,
                    mime_type="audio/pcm",
                    stream=True,
                )
                output_emitter.start_segment(segment_id="stub")
                state["started"] = True
            return False

        monkeypatch.setattr(lk, "_qwen3_tts_post_frames", fake_post)
        synth = tts.synthesize("你好呀")
        await synth._run(_StubEmitter())
        synth._synthesize_task.cancel()

    asyncio.run(asyncio.wait_for(drive(False), timeout=10))
    assert beeps == [True], "零音频失败要补 beep"
    asyncio.run(asyncio.wait_for(drive(True), timeout=10))
    assert beeps == [True], "音频已出过就唔该再补 beep"


class _RecordingEmitter:
    """_qwen3_tts_post_frames 直调用的轻量 emitter 桩。"""

    def initialize(self, **kwargs):
        pass

    def start_segment(self, **kwargs):
        pass

    def end_segment(self, **kwargs):
        pass

    def push(self, data):
        pass

    def flush(self):
        pass


class _CountingEmitter(_RecordingEmitter):
    """记录 push 字节数的 emitter 桩(爆段护栏测试用)。"""

    def __init__(self):
        self.bytes_pushed = 0
        self.started = False

    def initialize(self, **kwargs):
        self.started = True

    def push(self, data):
        self.bytes_pushed += len(data)

    def flush(self):
        pass


# ---- P4-B 回归:纯标点段绝不单独 POST(Qwen3-TTS 对纯标点 hallucinate 4-30s 爆段) ----


def test_qwen3_stream_skips_punctuation_only_segments(monkeypatch):
    """旧代码会把「。」单独成任务 POST(input='。' 实证)→ 现在必须丢弃。"""
    from agent_runtime.providers.livekit_plugins import _Qwen3SynthesizeStream

    posts: list[dict] = []
    _install_fake_sidecar(monkeypatch, posts)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = _Qwen3SynthesizeStream(tts, APIConnectOptions())
        s.push_text("你好。")
        s.push_text("。")  # 复现:句界 flush 后多出的纯标点段
        s.push_text("我係林先生。")
        s.push_text("？！")
        s.end_input()
        async for _ev in s:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    inputs = [p["input"] for p in posts]
    assert inputs == ["你好。", "我係林先生。"], inputs
    for p in posts:
        assert p["input"].strip("。！？!?，、；;：: \t"), f"纯标点任务混入: {p['input']!r}"


def test_qwen3_stream_drops_punctuation_only_tail(monkeypatch):
    """收尾残句只有纯标点(「。。」):整场一个任务都唔应该多发。"""
    from agent_runtime.providers.livekit_plugins import _Qwen3SynthesizeStream

    posts: list[dict] = []
    _install_fake_sidecar(monkeypatch, posts)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = _Qwen3SynthesizeStream(tts, APIConnectOptions())
        s.push_text("好。")
        s.push_text("。。")  # 尾部纯标点残句
        s.end_input()
        async for _ev in s:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert [p["input"] for p in posts] == ["好。"], [p["input"] for p in posts]


# ---- P4-A 回归:单任务爆段护栏(音频时长到顶即断流,唔再读剩余 body) ----


def test_qwen3_post_frames_caps_burst_audio(monkeypatch):
    """TTS 对异常输入连环合成 20-30s 爆段(P4 实测 1MB+):护栏到顶断流、唔重试。

    爆段霸住 playout 会把下一轮回复压喺官方语音队列后面——en 轮「LLM 挂死」
    嘅根因链最后一环。护栏保证单任务音频有上界。
    """
    import agent_runtime.providers.livekit_plugins as lk

    monkeypatch.setenv("QWEN3_TTS_MAX_TASK_AUDIO_SEC", "1")  # 24000Hz*1s*2 = 48000B
    attempts = {"n": 0}
    sent = {"bytes": 0}

    class _BurstResp:
        def raise_for_status(self):
            pass

        async def aiter_bytes(self):
            # 模拟 sidecar 连环 hallucinate:共 96KB(=2s)音频分 16KB 吐出。
            while sent["bytes"] < 96000:
                sent["bytes"] += 16000
                yield b"\x01\x02" * 8000

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            attempts["n"] += 1
            return _BurstResp()

    monkeypatch.setattr(
        lk, "httpx", SimpleNamespace(AsyncClient=lambda **kw: _FakeClient())
    )

    tts = _make_tts()  # sample_rate=24000
    em = _CountingEmitter()
    state = {"started": False}

    async def run():
        return await lk._qwen3_tts_post_frames(tts, "你好", em, state)

    ok = asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert ok is False, "爆段护栏触发要判失败(上层 broken 停后续任务)"
    assert attempts["n"] == 1, "护栏断流已有音频落地,绝唔重试重读"
    cap_bytes = tts.sample_rate * 1 * 2
    assert cap_bytes <= em.bytes_pushed <= cap_bytes + 16000, em.bytes_pushed
