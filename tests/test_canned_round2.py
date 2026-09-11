"""罐头音频第二轮修复回归（2026-09-11 深夜用户复测四反馈）。

1. F1 开场白语速≠回复语速: `_say_script`(直念线:开场白/心跳/收线/WA 复述)的
   缓存查找不带 speed → 命中旧 1.0 条目(call-a2705ed2 实证:开场白 1.0、回复 1.2)。
2. F2 词表回声穿透: ASR 把热词词表抄成转写时用了**繁体**(顺豐速運/賠償/單號),
   词表是简体 → 逐字匹配断链;且「拼多多。+回声尾」全有全无丢弃会把真实答案
   一起丢——须改「剥尾保头,纯回声才丢」。
3. F3 垫话→回复硬接: hold_if_playing 在垫话播完后返回 0,回复恰在垫话尾后到达
   =零间隔生硬;默认 gap 300ms 用户定档偏短。
4. F4 垫话字幕: out-of-band 音轨走 lk.transcription 数据包(不进 chat_ctx)。
"""

from __future__ import annotations

import asyncio
import sys
import time
import wave
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _say_script  # noqa: E402
from agent_runtime.fillers import FillerDirector, filler_gap_s  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    _is_hotword_vocab_echo,
    _strip_vocab_echo_tail,
    _StripTailAnchorStream,
)

_VOCAB = (
    "Vocabulary: 顺丰速运,运通,理赔,京东,拼多多,单号,运单,赔偿,运费,专员,集运,"
    "时效,上门,追踪,核实"
)


# ---- F1: 直念线缓存键带语速 ----


class _RecCache:
    def __init__(self, pcm=None):
        self.pcm = pcm
        self.lookups: list[dict] = []

    def lookup(self, text, *, voice, model, speed=1.0):
        self.lookups.append({"text": text, "voice": voice, "model": model, "speed": speed})
        return self.pcm


class _TTSFake:
    sample_rate = 24000

    def resolved_voice(self):
        return "voice-a"

    def resolved_model(self):
        return "speech-2.8-hd"

    def resolved_speed(self):
        return 1.2


class _SessionFake:
    def __init__(self):
        self.said: list[dict] = []

    async def say(self, text, audio=None):
        self.said.append({"text": text, "audio": audio is not None})


def test_say_script_lookup_carries_speed():
    """开场白/心跳/收线/WA 复述的缓存查找必须带运行时语速(1.2)——旧 1.0 条目
    键不同自然 miss,重合成 1.2;不带 speed 则命中旧 1.0 音频=开场白慢半档。"""
    cache = _RecCache(pcm=b"\x01\x00" * 4800)  # 命中路径
    session = _SessionFake()
    asyncio.run(asyncio.wait_for(_say_script(session, _TTSFake(), cache, "您好"), 5))
    assert cache.lookups and cache.lookups[0]["speed"] == 1.2
    assert session.said and session.said[0]["audio"] is True


def test_say_script_miss_path_uses_cached_tts_synthesize():
    """miss → 走 CachedTTS.synthesize(内部已 speed-aware)——这里只验证不再裸查
    旧键:lookup 带 speed 且 miss 后 session.say 走文本合成路径。"""
    cache = _RecCache(pcm=None)

    class _SynthTTS(_TTSFake):
        def synthesize(self, text, conn_options=None):
            raise AssertionError("synthesize by provider is CachedTTS's job; agent not here")

    class _SynthStream:
        def __init__(self):
            self._emitted_beep = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _SynthTTS2(_TTSFake):
        def synthesize(self, text, conn_options=None):
            return _SynthStream()

    session = _SessionFake()
    asyncio.run(asyncio.wait_for(_say_script(session, _SynthTTS2(), cache, "您好"), 5))
    assert cache.lookups[0]["speed"] == 1.2


# ---- F2: 词表回声剥尾(繁简不敏感) ----


def test_strip_vocab_echo_tail_traditional():
    """call-a2705ed2 实证形态:真话头(拼多多。)+繁体词表回声尾——剥尾保头。"""
    text = "拼多多。顺豐速運，運通，理賠，京東，拼多多，單號，運單，賠償，運費，專員，集運，時效，上門，追蹤，核實，"
    out = _strip_vocab_echo_tail(text, _VOCAB)
    assert out.strip("。，, ") == "拼多多"


def test_full_traditional_echo_detected():
    """纯繁体回声(无真话头)也要被全量判定拦住(旧逐字简体匹配在此断链)。"""
    text = "單號，運單，賠償，運費，專員，集運，時效，上門，追蹤，核實"
    assert _is_hotword_vocab_echo(text, _VOCAB) is True
    assert _strip_vocab_echo_tail(text, _VOCAB) == ""


def test_strip_vocab_echo_normal_text_untouched():
    assert _strip_vocab_echo_tail("我在京东买了个东西", _VOCAB) == "我在京东买了个东西"
    # 短词表词串(≤3 段)可能是真实回答,不剥
    assert _strip_vocab_echo_tail("拼多多，京东", _VOCAB) == "拼多多，京东"
    # 2026-09-12 call-aa86dfc9 实证:未命中回声时尾问号被误剥——原文必须原样
    assert _strip_vocab_echo_tail("哪件货啊？是哪件货？", _VOCAB) == "哪件货啊？是哪件货？"
    assert _strip_vocab_echo_tail("你单号可以。发我一下。", _VOCAB) == "你单号可以。发我一下。"


def test_strip_vocab_echo_short_run_not_stripped():
    """尾段词表词 <4 个=可能是真实回答(平台选择类),不剥。"""
    assert _strip_vocab_echo_tail("快件,运单,赔偿", _VOCAB) == "快件,运单,赔偿"


# ---- F3: 垫话→回复最小间隔 ----


class _FakePlayHandle:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done

    def stop(self):
        self._done = True

    async def wait_for_playout(self):
        return None


class _FakePlayer:
    def play(self, source):
        return _FakePlayHandle()


class _FakeSession:
    agent_state = "thinking"


def _make_assets(tmp_path: Path) -> Path:
    assets = tmp_path / "fillers"
    assets.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, text in enumerate(["好的，您稍等。"], 1):
        name = f"zh-{i:02d}.wav"
        with wave.open(str(assets / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes((1000).to_bytes(2, "little", signed=True) * 480)
        entries.append({"text": text, "file": name, "dur_s": 1.0})
    (assets / "manifest.json").write_text(json.dumps({"zh": entries}), encoding="utf-8")
    return assets


def test_filler_gap_default_random_window(monkeypatch):
    monkeypatch.delenv("BOK_FILLER_GAP_MS", raising=False)
    vals = {filler_gap_s() for _ in range(30)}
    assert all(0.3 <= v <= 0.6 for v in vals) and len(vals) > 1


def test_hold_returns_gap_window_after_filler_done(tmp_path, monkeypatch):
    """垫话播完后 hold 不得归零:回复恰在垫话尾后到达=零间隔硬接(用户实证生硬),
    必须保住剩余 gap 窗;cancel(用户插话)清窗。"""
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "1")
    monkeypatch.delenv("BOK_FILLER_GAP_MS", raising=False)
    d = FillerDirector(
        _FakeSession(), lang_resolver=lambda: "zh", player=_FakePlayer(),
        guards=lambda: False, assets_dir=_make_assets(tmp_path),
    )
    # 手动置「1s 前开播、时长 1s」=已播完,落在 gap 窗内
    d._play_started = time.monotonic() - 1.0
    d._cur_dur = 1.0
    d._handle = _FakePlayHandle(done=True)
    hold = d.hold_if_playing()
    assert 0.25 < hold <= 0.62, hold
    # 远超窗 → 0
    d._play_started = time.monotonic() - 10.0
    assert d.hold_if_playing() == 0.0
    # 播放中 → 剩余+gap
    d._play_started = time.monotonic() - 0.2
    hold2 = d.hold_if_playing()
    assert 0.8 < hold2 <= 1.45, hold2
    # 用户插话 cancel → 清窗不扣压
    d._play_started = time.monotonic() - 0.2
    d._handle = _FakePlayHandle(done=False)
    d.cancel()
    assert d.hold_if_playing() == 0.0


# ---- F4: 垫话字幕回调 ----


def test_filler_director_caption_callback(tmp_path, monkeypatch):
    """开火即发字幕回调(文本+时长)——agent 侧转 lk.transcription 数据包。"""
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "1")
    captions: list[str] = []
    d = FillerDirector(
        _FakeSession(), lang_resolver=lambda: "zh", player=_FakePlayer(),
        guards=lambda: False, assets_dir=_make_assets(tmp_path),
        caption=lambda text: captions.append(text),
    )

    async def _go():
        d.arm()
        await asyncio.sleep(0.05)

    asyncio.run(asyncio.wait_for(_go(), 5.0))
    assert captions == ["好的，您稍等。"]


# ---- 数字顿号剥离(2026-09-12「普通话念数字很奇怪」) ----


class _StreamShell:
    """_StripTailAnchorStream 只用到 _hold/_drop/_prev_digit 状态与 _feed。"""

    _hold = ""
    _drop = False
    _prev_digit = ""
    _pending_sep = ""

    _feed = _StripTailAnchorStream._feed
    _strip_digit_pauses = _StripTailAnchorStream._strip_digit_pauses


def test_digit_pause_strip_within_chunk():
    s = _StreamShell()
    assert s._feed("号码是一、一、二、二、三。") == "号码是一一二二三。"


def test_digit_pause_strip_across_chunks():
    s = _StreamShell()
    assert s._feed("号码是一、") == "号码是" + "一"  # 末位数字扣住配对
    assert s._feed("一、二") == "一二"  # 跨 chunk 顿号剥掉,不重复左文


def test_digit_pause_normal_list_untouched():
    s = _StreamShell()
    assert s._feed("拼多多、淘宝、京东") == "拼多多、淘宝、京东"
    assert s._feed("三、然后说") == "三、然后说"  # 右侧非数字=合法顿号保留
