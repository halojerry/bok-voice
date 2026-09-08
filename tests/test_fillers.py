"""FillerDirector 单测:门控矩阵/缓存未命中跳过/限次/轮换/首音频打断。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.fillers import FillerDirector, filler_lines  # noqa: E402
from agent_runtime.tts_cache import TtsAudioCache  # noqa: E402

_PCM = (1000).to_bytes(2, "little", signed=True) * 4800


class _FakeHandle:
    def __init__(self):
        self.interrupted = False

    def done(self):
        return False

    def interrupt(self, *, force=False):
        self.interrupted = True


class _FakeSession:
    agent_state = "thinking"

    def __init__(self):
        self.says: list[dict] = []

    def say(self, text, *, audio=None, add_to_chat_ctx=True):
        """同步返回句柄(与 livekit 1.8 API 一致;await handle 只係等播完)。"""
        self.says.append({"text": text, "audio": audio, "add_to_chat_ctx": add_to_chat_ctx})
        handle = _FakeHandle()
        self.says[-1]["_handle"] = handle
        return handle


class _FakeTTS:
    def __init__(self, voice="voice-a"):
        self._voice = voice

    def resolved_voice(self):
        return self._voice

    def resolved_model(self):
        return "model-x"


def _director(tmp_path, *, session=None, tts=None, guards=None, voice="voice-a"):
    cache = TtsAudioCache(root=tmp_path / "tts-cache", sample_rate=24000)
    session = session or _FakeSession()
    tts = tts or _FakeTTS(voice=voice)
    d = FillerDirector(
        session,
        tts,
        cache,
        lang_resolver=lambda: "cantonese",
        guards=guards or (lambda: False),
    )
    return d, session, tts, cache


def _seed(cache: TtsAudioCache, lang: str):
    voice, model = "voice-a", "model-x"
    for line in filler_lines()[lang]:
        cache.store(cache.key_for(line, voice=voice, model=model), _PCM, text=line, voice=voice, model=model)


def _run(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_fires_when_no_reply_audio(tmp_path):
    async def _case():
        d, session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert len(session.says) == 1
        assert session.says[0]["add_to_chat_ctx"] is False, "垫话绝不进 LLM 上下文"
        assert session.says[0]["text"] in filler_lines()["cantonese"]
        return d

    _run(_case())


def test_cancelled_by_reply_first_audio(tmp_path):
    async def _case():
        d, session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "400"
        try:
            d.arm()
            await asyncio.sleep(0.02)
            d.on_reply_first_audio()  # 快轮:真回复先出声
            await asyncio.sleep(0.05)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert session.says == [], "快轮永远不垫"

    _run(_case())


def test_interrupt_playing_filler_on_reply_audio(tmp_path):
    async def _case():
        d, session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)  # 已开火、句柄已记(playout 中)
            assert len(session.says) == 1
            d.on_reply_first_audio()  # 真回复出声 → 定向打断
            assert session.says[0]["_handle"].interrupted, "真回复出声必须打断垫话"
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert d._handle is None, "打断后句柄清档"

    _run(_case())


def test_cache_miss_skips_never_synthesizes(tmp_path):
    async def _case():
        d, session, _tts, _cache = _director(tmp_path, voice="other-voice")  # 音色不同 → miss
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert session.says == [], "缓存未命中必须跳过,绝不触发云合成"

    _run(_case())


def test_max_per_call_and_rotation(tmp_path):
    async def _case():
        d, session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        os.environ["BOK_FILLER_MAX"] = "2"
        try:
            d.arm()
            await asyncio.sleep(0.06)
            d.arm()
            await asyncio.sleep(0.06)
            d.arm()  # 第三次:超限,唔播
            await asyncio.sleep(0.06)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
            os.environ.pop("BOK_FILLER_MAX", None)
        assert len(session.says) == 2, "每通限次"
        assert session.says[0]["text"] != session.says[1]["text"], "轮换不重样"

    _run(_case())


def test_guards_abort(tmp_path):
    async def _case():
        d, session, _tts, cache = _director(tmp_path, guards=lambda: True)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert session.says == [], "guards(closing/收线等)命中不开火"

    _run(_case())


def test_kill_switch(tmp_path):
    async def _case():
        d, session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        os.environ["BOK_FILLER"] = "0"
        try:
            d.arm()
            await asyncio.sleep(0.08)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
            os.environ.pop("BOK_FILLER", None)
        assert session.says == [], "BOK_FILLER=0 全关"

    _run(_case())
