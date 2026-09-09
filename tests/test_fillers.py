"""FillerDirector 单测:门控矩阵/缓存未命中跳过/限次/轮换/首音频停播/用户再开口停播。

通道铁律(2026-09-09):垫话走 BackgroundAudioPlayer out-of-band 音轨,不走
session.say()——1.8 speech 队列严格串行,垫话会排在回复后面(实机实证)。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.fillers import FillerDirector, filler_lines  # noqa: E402
from agent_runtime.tts_cache import TtsAudioCache  # noqa: E402

_PCM = (1000).to_bytes(2, "little", signed=True) * 4800


class _FakePlayHandle:
    def __init__(self):
        self.stopped = False

    def done(self):
        return False

    def stop(self):
        self.stopped = True


class _FakePlayer:
    """替身:与 livekit BackgroundAudioPlayer.play() 同形——同步返回 PlayHandle。"""

    def __init__(self):
        self.plays: list[object] = []
        self.handles: list[_FakePlayHandle] = []

    def play(self, source):
        self.plays.append(source)
        handle = _FakePlayHandle()
        self.handles.append(handle)
        return handle


class _FakeSession:
    agent_state = "thinking"


class _FakeTTS:
    def __init__(self, voice="voice-a"):
        self._voice = voice

    def resolved_voice(self):
        return self._voice

    def resolved_model(self):
        return "model-x"


_NO_PLAYER = object()  # 哨兵:显式传 None(无 player)与不传(自动 FakePlayer)区分


def _director(tmp_path, *, session=None, tts=None, player=_NO_PLAYER, guards=None, voice="voice-a"):
    cache = TtsAudioCache(root=tmp_path / "tts-cache", sample_rate=24000)
    session = session or _FakeSession()
    tts = tts or _FakeTTS(voice=voice)
    if player is _NO_PLAYER:
        player = _FakePlayer()
    d = FillerDirector(
        session,
        tts,
        cache,
        lang_resolver=lambda: "cantonese",
        player=player,
        guards=guards or (lambda: False),
    )
    return d, player, session, tts, cache


def _seed(cache: TtsAudioCache, lang: str):
    voice, model = "voice-a", "model-x"
    for line in filler_lines()[lang]:
        cache.store(cache.key_for(line, voice=voice, model=model), _PCM, text=line, voice=voice, model=model)


def _run(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_fires_when_no_reply_audio(tmp_path):
    async def _case():
        d, player, _session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert len(player.plays) == 1, "out-of-band 播放(不排 speech 队列)"
        assert d._fired_lines == [d._fired_lines[0]] and d._fired_lines[0] in filler_lines()["cantonese"]

    _run(_case())


def test_player_none_disables(tmp_path):
    async def _case():
        d, player, _session, _tts, cache = _director(tmp_path, player=None)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert player is None and d._timer is None, "无 player 垫话整体失效(arm 即返)"

    _run(_case())


def test_cancelled_by_reply_first_audio(tmp_path):
    async def _case():
        d, player, _session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "400"
        try:
            d.arm()
            await asyncio.sleep(0.02)
            d.on_reply_first_audio()  # 快轮:真回复先出声
            await asyncio.sleep(0.05)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert player.plays == [], "快轮永远不垫"

    _run(_case())


def test_stop_playing_filler_on_reply_audio(tmp_path):
    async def _case():
        d, player, _session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)  # 已开火、句柄已记(out-of-band 播放中)
            assert len(player.plays) == 1
            d.on_reply_first_audio()  # 真回复出声 → 停垫话
            assert player.handles[0].stopped, "真回复出声必须停垫话"
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert d._handle is None, "停播后句柄清档"

    _run(_case())


def test_cancel_stops_playing_filler_on_new_user_turn(tmp_path):
    async def _case():
        d, player, _session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)
            d.cancel()  # 用户再开口:out-of-band 音轨框架打断管不到,必须自己停
            assert player.handles[0].stopped, "新用户轮必须停掉在播垫话"
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)

    _run(_case())


def test_cache_miss_skips_never_synthesizes(tmp_path):
    async def _case():
        d, player, _session, _tts, _cache = _director(tmp_path, voice="other-voice")  # 音色不同 → miss
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert player.plays == [], "缓存未命中必须跳过,绝不触发云合成"

    _run(_case())


def test_max_per_call_and_rotation(tmp_path):
    async def _case():
        d, player, _session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        os.environ["BOK_FILLER_MAX"] = "2"
        try:
            d.arm()
            await asyncio.sleep(0.06)
            d._handle = None  # 上一句已播完(句柄清档),下一句先至可以叠上
            d.arm()
            await asyncio.sleep(0.06)
            d.arm()  # 第三次:超限,唔播
            await asyncio.sleep(0.06)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
            os.environ.pop("BOK_FILLER_MAX", None)
        assert len(player.plays) == 2, "每通限次"
        assert d._fired_lines[0] != d._fired_lines[1], "轮换不重样"

    _run(_case())


def test_pick_line_random_avoids_consecutive_repeat(tmp_path, monkeypatch):
    # 随机不重样:剔除最近 2 句后随机——连续取 12 次永不与上一句相同,且都出自池
    monkeypatch.setenv(
        "BOK_FILLER_LINES",
        json.dumps({"cantonese": ["好，等我睇下。", "好，你等陣。", "好嘅，幫你跟緊。"]}, ensure_ascii=False),
    )
    d, _player, _session, _tts, _cache = _director(tmp_path)
    picks = [d._pick_line("cantonese") for _ in range(12)]
    pool = set(filler_lines()["cantonese"])
    assert set(picks) <= pool
    for prev, cur in zip(picks, picks[1:]):
        assert prev != cur, "相邻两句垫话不得重复"


def test_pick_line_falls_back_when_pool_small(tmp_path, monkeypatch):
    # 池=2 且 fired 已含两句:候选空 → 回落全池,唔会无句可拣
    monkeypatch.setenv(
        "BOK_FILLER_LINES",
        json.dumps({"cantonese": ["好，等我睇下。", "好，你等陣。"]}, ensure_ascii=False),
    )
    d, _player, _session, _tts, _cache = _director(tmp_path)
    d._fired_lines = ["好，等我睇下。", "好，你等陣。"]
    assert d._pick_line("cantonese") in {"好，等我睇下。", "好，你等陣。"}


def test_guards_abort(tmp_path):
    async def _case():
        d, player, _session, _tts, cache = _director(tmp_path, guards=lambda: True)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        try:
            d.arm()
            await asyncio.sleep(0.08)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
        assert player.plays == [], "guards(closing/收线等)命中不开火"

    _run(_case())


def test_kill_switch(tmp_path):
    async def _case():
        d, player, _session, _tts, cache = _director(tmp_path)
        _seed(cache, "cantonese")
        os.environ["BOK_FILLER_DELAY_MS"] = "10"
        os.environ["BOK_FILLER"] = "0"
        try:
            d.arm()
            await asyncio.sleep(0.08)
        finally:
            os.environ.pop("BOK_FILLER_DELAY_MS", None)
            os.environ.pop("BOK_FILLER", None)
        assert player.plays == [], "BOK_FILLER=0 全关"

    _run(_case())
