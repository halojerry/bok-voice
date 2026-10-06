"""FillerDirector 单测(2026-09-10 资产化改版;2026-10-02 播放排序翻转为让路):
manifest 池/语言铁律(绝不跨语言)/限次/轮换防重/首音频停播+hold 归零(新政策,
BOK_FILLER_YIELD=0 回「播完+hold 扣压」旧档)/用户插话停播/kill-switch。

资产契约:垫话=随源码分发的 wav+manifest(apps/agent/agent_runtime/assets/fillers/),
运行时只播文件绝不云合成;语言=装配时钉死的通话语言,池缺失明文跳过。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.fillers import FillerDirector, filler_gap_s, load_manifest  # noqa: E402

_PCM = (1000).to_bytes(2, "little", signed=True) * 480  # 20ms @24k


def _make_assets(
    tmp_path: Path,
    pools: dict[str, list[str]],
    cats: dict[str, list[str]] | None = None,
) -> Path:
    """微型 wav+manifest;音频本身 20ms,时长断言走 manifest dur_s(权威)。

    cats:每语言与 texts 对齐的 cat 标签表(缺省不打标,与旧用例零变化)。
    """
    assets = tmp_path / "fillers"
    assets.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, list[dict]] = {}
    for lang, texts in pools.items():
        entries = []
        for i, text in enumerate(texts, 1):
            name = f"{lang}-{i:02d}.wav"
            with wave.open(str(assets / name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(_PCM)
            entry = {"text": text, "file": name, "dur_s": 1.0}
            if cats and cats.get(lang):
                entry["cat"] = cats[lang][i - 1]
            entries.append(entry)
        manifest[lang] = entries
    (assets / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return assets


class _FakePlayHandle:
    """与官方 PlayHandle 同形:future 驱动 done/stop/wait_for_playout(播完即醒)。"""

    def __init__(self):
        self.stopped = False
        self._done = asyncio.get_running_loop().create_future()

    def done(self):
        return self._done.done()

    def stop(self):
        self.stopped = True
        if not self._done.done():
            self._done.set_result(True)

    def complete(self):
        """模拟自然播完(不经 stop)。"""
        if not self._done.done():
            self._done.set_result(True)

    async def wait_for_playout(self):
        await asyncio.shield(self._done)


class _DoneHandle:
    def done(self):
        return True


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


_NO_PLAYER = object()  # 哨兵:显式传 None(无 player)与不传(自动 FakePlayer)区分


def _director(
    tmp_path,
    *,
    pools: dict[str, list[str]] | None = None,
    cats: dict[str, list[str]] | None = None,
    lang="cantonese",
    player=_NO_PLAYER,
    guards=None,
    cache=None,
    voice_model_resolver=None,
    backfill=None,
):
    pools = pools if pools is not None else {
        "cantonese": ["好，等我睇下。", "好，等一陣。"],
        "zh": ["好的，您稍等。"],
        "en": ["Sure."],
    }
    assets = _make_assets(tmp_path, pools, cats)
    if player is _NO_PLAYER:
        player = _FakePlayer()
    d = FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: lang,
        player=player,
        guards=guards or (lambda: False),
        assets_dir=assets,
        cache=cache,
        voice_model_resolver=voice_model_resolver,
        backfill=backfill,
    )
    return d, player


class _Recorder:
    """backfill 替身:记录调用文本(async 兼容 sync callable 都可——Director 只 await 结果)。"""

    def __init__(self):
        self.texts: list[str] = []

    async def __call__(self, text: str) -> None:
        self.texts.append(text)


class _FakeCache:
    """与 TtsAudioCache.lookup 同形:文本→pcm bytes 或 None。"""

    def __init__(self, pcm_by_text=None, sample_rate: int = 24000):
        self.pcm_by_text = pcm_by_text or {}
        self.sample_rate = sample_rate
        self.lookups: list[tuple[str, str, str]] = []

    def lookup(self, text: str, *, voice: str, model: str, speed: float = 1.0):
        self.lookups.append((text, voice, model))
        return self.pcm_by_text.get(text)


def _run(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_fires_from_assets_when_no_reply_audio(tmp_path):
    async def _case():
        d, player = _director(tmp_path)
        await d._fire(0)
        assert len(player.plays) == 1
        assert d._fired_lines and d._fired_lines[0] in {"好，等我睇下。", "好，等一陣。"}

    _run(_case())


def test_player_none_disables(tmp_path):
    async def _case():
        d, player = _director(tmp_path, player=None)
        await d._fire(0)
        assert player is None and d._fired_lines == []

    _run(_case())


def test_reply_first_audio_stops_playing(tmp_path):
    """播放排序契约(2026-10-02 政策翻转):首音频到达即停垫话,hold 归零。

    旧契约「垫话播完」的反例=call-4e8d58c1(真答案首音频 18.4s≈垫话2 播完 18.5s);
    旧档回归由 BOK_FILLER_YIELD=0 臂覆盖(见 tests/test_filler_yield.py)。"""

    async def _case():
        d, player = _director(tmp_path)
        await d._fire(0)
        handle = player.handles[-1]
        d.on_reply_first_audio()
        assert handle.stopped is True, "首音频应停掉在播垫话(让路政策)"
        assert d.hold_if_playing() == 0.0, "让路后回复不再扣压"

    _run(_case())


def test_hold_if_playing_legacy_timeline_under_kill_switch(tmp_path, monkeypatch):
    """旧档(BOK_FILLER_YIELD=0)hold 时间轴契约原样保留:剩余+gap、播完保 gap 窗。"""
    monkeypatch.setenv("BOK_FILLER_YIELD", "0")

    async def _case():
        d, player = _director(tmp_path)
        assert d.hold_if_playing() == 0.0  # 未播
        await d._fire(0)
        hold = d.hold_if_playing()
        # 2026-09-12: dur 恒用实际 PCM(测试资产 20ms)——manifest dur_s 仅资产层参考。
        assert 0.3 <= hold <= 0.65, f"hold 应≈dur(0.02)+gap(0.3-0.6随机): {hold}"
        # 2026-09-11 语义升级:播完(handle done)不再立即归零——回复恰在垫话尾后
        # 到达=零间隔硬接(用户实证生硬),须保住余下 gap 窗;时间轴超出才归零。
        import time as _t

        d._cur_dur = 1.0
        d._play_started = _t.monotonic() - 1.0  # 1s 前开播,dur=1.0 → 刚播完
        hold_done = d.hold_if_playing()
        assert 0.25 < hold_done <= 0.62, f"播完 gap 窗(随机): {hold_done}"
        d._play_started = _t.monotonic() - 10.0  # 远超窗
        assert d.hold_if_playing() == 0.0
        await d._fire(0)  # 重新开一把再 cancel:用户插话清窗
        d.cancel()
        assert d.hold_if_playing() == 0.0

    _run(_case())


def test_cancel_stops_playing_filler_on_new_user_turn(tmp_path):
    """用户插话优先:新用户轮仍立即掐垫话(与回复首音频行为相区别)。"""

    async def _case():
        d, player = _director(tmp_path)
        await d._fire(0)
        handle = player.handles[-1]
        d.cancel()
        assert handle.stopped is True
        assert d.hold_if_playing() == 0.0

    _run(_case())


def test_wrong_lang_never_falls_back(tmp_path):
    """语言铁律:en 会话 + 只有 zh 池 → 明文跳过,绝不放中文垫话。"""

    async def _case():
        d, player = _director(tmp_path, pools={"zh": ["好的，您稍等。"]}, lang="en")
        await d._fire(0)
        assert player.plays == [], "跨语言发声=语言铁律被破"
        assert d._fired_lines == []

    _run(_case())


def test_manifest_missing_disables(tmp_path):
    async def _case():
        d, player = _director(tmp_path, pools={"cantonese": ["好。"]})
        d._assets = tmp_path / "no-such-dir"  # 资产目录被删/缺失
        await d._fire(0)
        assert player.plays == [] and d._fired_lines == []

    _run(_case())


def test_max_per_call_and_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("BOK_FILLER_MAX", "2")  # 默认已改 3(task-5):限次用例显式钉 2

    async def _case():
        d, player = _director(tmp_path)
        await d._fire(0)
        d._handle = None  # 模拟垫话播完(handle 释放)
        await d._fire(0)
        assert len(player.plays) == 2
        assert len(set(d._recent[-2:])) == 2, "相邻两轮不应重复同一垫话"
        d._handle = None
        await d._fire(0)  # BOK_FILLER_MAX=2
        assert len(player.plays) == 2

    _run(_case())


def test_guards_abort(tmp_path):
    async def _case():
        d, player = _director(tmp_path, guards=lambda: True)
        await d._fire(0)
        assert player.plays == []

    _run(_case())


def test_kill_switch(tmp_path, monkeypatch):
    async def _case():
        d, player = _director(tmp_path)
        await d._fire(0)
        assert player.plays == []

    monkeypatch.setenv("BOK_FILLER", "0")
    _run(_case())


def test_gap_env_default_and_override(monkeypatch):
    monkeypatch.delenv("BOK_FILLER_GAP_MS", raising=False)
    # 2026-09-12 用户定档:默认 300-600ms 均匀随机(固定值机械感)。
    vals = {filler_gap_s() for _ in range(30)}
    assert all(0.3 <= v <= 0.6 for v in vals)
    assert len(vals) > 1, "随机窗应产生变化值"
    monkeypatch.setenv("BOK_FILLER_GAP_MS", "300")
    assert filler_gap_s() == 0.3


def test_filler_max_per_lang_defaults(monkeypatch):
    monkeypatch.delenv("BOK_FILLER_MAX", raising=False)
    from agent_runtime.fillers import filler_max_per_call
    # 2026-10-03 I2(en 垫话审计):旧全局 6 在 en 长通话(9 arm 轮)尾部裸奔——
    # 默认改按语言给档(en=10 / zh/粤=8 / 未知=8);env 显式=全局硬覆写
    # (见 test_filler_max_per_lang_with_env_override)。
    assert filler_max_per_call("en") == 10
    assert filler_max_per_call("zh") == 8
    assert filler_max_per_call("cantonese") == 8
    assert filler_max_per_call() == 8


def test_single_entry_category_pool_widens_on_recent_dedup(tmp_path):
    """2026-09-14 call-c76832ac 实证:分类池只有 1 条时旧 `or list(pool)` 兜底
    把同一条放回,「冇問題，你稍等多一陣…」4 分钟连播 3 次(voice_hit=1)。
    去重清空必须向整池放宽,不得落回单条池。"""

    async def _case():
        pools = {"cantonese": ["默认垫话。", "应承一。", "应承二。"]}
        cats = {"cantonese": ["default", "ack", "ack"]}
        d, _ = _director(tmp_path, pools=pools, cats=cats)
        e1 = d._pick("cantonese", "default")
        assert e1["text"] == "默认垫话。"  # 首抽仍优先 default 类(既有行为)
        e2 = d._pick("cantonese", "default")
        assert e2["text"] != "默认垫话。", "优先池唯一条已在去重窗内 → 放宽整池,不得复播"
        e3 = d._pick("cantonese", "default")
        assert len({e1["text"], e2["text"], e3["text"]}) == 3  # 三连必不同

    _run(_case())


def test_tiny_pool_allows_repeat_without_error(tmp_path):
    """池全部落在去重窗内(池=1)时允许重复——永不因去重没垫话播/抛错。"""

    async def _case():
        d, _ = _director(tmp_path, pools={"cantonese": ["唯一垫话。"]})
        assert d._pick("cantonese")["text"] == "唯一垫话。"
        assert d._pick("cantonese")["text"] == "唯一垫话。"

    _run(_case())


def test_real_manifest_cantonese_default_no_three_peat():
    """真 manifest 回归:cantonese `default` 类只有 1 条(call-c76832ac 现场),
    连抽 3 次不得同句连播——分类去重必须吃真资产数据。"""
    d = FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: "cantonese",
        player=_FakePlayer(),
        guards=lambda: False,
    )
    lines = [d._pick("cantonese", "default")["text"] for _ in range(3)]
    assert len(set(lines)) == 3, f"同句连播回归: {lines}"


def test_load_manifest_shape(tmp_path):
    assets = _make_assets(tmp_path, {"zh": ["好的，您稍等。"]})
    m = load_manifest(assets)
    assert m["zh"][0]["text"] == "好的，您稍等。"
    assert m["zh"][0]["file"] == "zh-01.wav"
    assert m["zh"][0]["dur_s"] == 1.0


def test_filler_assets_manifest_shape():
    """真实资产契约:随源码分发的 manifest 三语齐全、10 短+3 长池子不回退,
    长句 tier(覆盖窗目标 1.7-2.3s)真实存在且 wav 文件在位。"""
    from agent_runtime.fillers import FILLER_ASSETS_DIR, load_manifest

    m = load_manifest(FILLER_ASSETS_DIR)
    assert set(m) == {"zh", "cantonese", "en"}
    for lang, entries in m.items():
        assert len(entries) >= 13, f"{lang} 池应含 10 短 + 3 长"
        durs = sorted(e["dur_s"] for e in entries)
        assert durs[-1] >= 1.6, f"{lang} 应含 ≥1.6s 长句"
        for e in entries:
            assert (FILLER_ASSETS_DIR / e["file"]).exists()


# ---- 链发(首条播完回复仍未出声 → 自动补第二发,2026-09-10 task-4) ----

_CHAIN_ENV = {
    "BOK_FILLER_DELAY_MS": "1",  # arm→首发起播即刻
    "BOK_FILLER_GAP_MS": "10",  # 垫话1播完→垫话2 的呼吸窗
    "BOK_FILLER_MAX": "3",  # 放宽预算,隔离 depth 门与 budget 门
    "BOK_FILLER_CHAIN": "1",
}


def test_chain_fires_second_filler_when_reply_late(tmp_path, monkeypatch):
    """首条播完、回复首音频仍未到 → gap 后自动补第二发(不重样、计数同源)。

    旧链发档(BOK_FILLER_CHAIN=1)整体走旧契约:hold 时间轴随之(让路闸关)。"""
    for k, v in _CHAIN_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BOK_FILLER_YIELD", "0")

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await asyncio.sleep(0.05)  # 首发起播
        assert len(player.plays) == 1 and d._count == 1
        player.handles[0].complete()  # 垫话1 自然播完
        await asyncio.sleep(0.15)  # gap + 观察者补位
        assert len(player.plays) == 2 and d._count == 2, "播完无回复应链发第二发"
        assert d._fired_lines[0] != d._fired_lines[1], "链发两条不重样"
        # 测试资产仅 20ms 实际音频,断言前手动置「在播」窗验证 hold 契约
        # (2026-09-12: _cur_dur 恒用实际 PCM 口径,20ms 已自然播完)。
        d._cur_dur = 1.0
        d._play_started = time.monotonic() - 0.1
        assert d.hold_if_playing() > 0, "垫话2 在播:hold 扣压契约照常生效"

    _run(_case())


def test_chain_skipped_when_reply_first_audio_arrived(tmp_path, monkeypatch):
    """回复首音频已到 → 垫话1 播完即止,不链发(hold 扣压自会衔接回复)。"""
    for k, v in _CHAIN_ENV.items():
        monkeypatch.setenv(k, v)

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await asyncio.sleep(0.05)
        d.on_reply_first_audio()  # 回复首音频到达(只作废定时器,不掐在播)
        player.handles[0].complete()
        await asyncio.sleep(0.15)
        assert len(player.plays) == 1, "回复已出声还链发=叠音"

    _run(_case())


def test_chain_depth_capped_at_one_per_turn(tmp_path, monkeypatch):
    """第二条播完仍无回复 → 不发第三条(每轮链发封顶 1,MAX 未耗尽也一样)。"""
    for k, v in _CHAIN_ENV.items():
        monkeypatch.setenv(k, v)

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await asyncio.sleep(0.05)
        player.handles[0].complete()
        await asyncio.sleep(0.15)
        assert len(player.plays) == 2 and d._chain_depth == 1
        player.handles[1].complete()  # 垫话2 也播完,回复仍没来
        await asyncio.sleep(0.15)
        assert len(player.plays) == 2, "链发封顶 1 次/轮"
        d.arm()  # 新一轮:深度复位,链发额度恢复
        assert d._chain_depth == 0

    _run(_case())


def test_chain_cancelled_on_new_turn(tmp_path, monkeypatch):
    """观察者挂起中 cancel()(新用户轮) → 链发任务取消,后续播完不补发。"""
    for k, v in _CHAIN_ENV.items():
        monkeypatch.setenv(k, v)

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await asyncio.sleep(0.05)
        d.cancel()  # 用户插话优先:停播 + 取消观察者
        assert player.handles[0].stopped is True
        assert d._chain_task is None
        player.handles[0].complete()  # 停播已 resolve;不再唤醒任何链发
        await asyncio.sleep(0.15)
        assert len(player.plays) == 1, "取消后不得链发"

    _run(_case())


# ---- 人设音色双层出声(2026-09-10 task-14a):cache 命中=与通话完全同人声,
# miss=资产兜底(永不哑)+异步补物化(off 关键路径);铁律「全场同一音色」。 ----

_VOICE_KW = {"voice_model_resolver": lambda: ("VoiceX", "speech-2.8-hd")}


def test_voice_cache_hit_plays_cached_never_reads_asset(tmp_path, monkeypatch):
    """命中层:cache 有运行时人设物化版 → 播缓存 PCM,完全不读资产 wav、不补物化。"""

    async def _case():
        d, player = _director(tmp_path, cache=_FakeCache(), **_VOICE_KW)
        # 池随机选句:全部条目都物化进 cache,命中层断言与随机结果无关。
        d._cache.pcm_by_text = {e["text"]: _PCM for e in d._pools()["cantonese"]}
        recorder = _Recorder()
        d._backfill = recorder

        import agent_runtime.fillers as fillers_mod

        def _boom(path):
            raise AssertionError("cache 命中时不应读资产 wav")

        monkeypatch.setattr(fillers_mod, "load_wav_pcm", _boom)
        await d._fire(0)
        assert len(player.plays) == 1, "命中层照常出声"
        fired = d._fired_lines[0]
        assert (fired, "VoiceX", "speech-2.8-hd") in d._cache.lookups, "按运行时人设 voice/model 查"
        assert all(row[1:] == ("VoiceX", "speech-2.8-hd") for row in d._cache.lookups)
        assert recorder.texts == [], "命中层无需补物化"

    _run(_case())


def test_voice_cache_miss_plays_asset_and_backfills(tmp_path):
    """miss 层:资产兜底出声(永不哑)+ backfill 用该句文本异步补物化恰好一次。"""

    async def _case():
        recorder = _Recorder()
        d, player = _director(tmp_path, cache=_FakeCache(), backfill=recorder, **_VOICE_KW)
        await d._fire(0)
        assert len(player.plays) == 1, "miss 必须资产兜底,绝不哑"
        await asyncio.sleep(0.05)  # 让异步补物化任务跑完
        assert d._fired_lines and recorder.texts == [d._fired_lines[0]]

    _run(_case())


def test_none_cache_or_resolver_pure_asset(tmp_path):
    """cache/resolver 任一缺省 → 纯资产模式(既有行为零变化,零 lookup 零 backfill)。"""

    async def _case():
        for kwargs in ({}, {"cache": _FakeCache()}, {"backfill": _Recorder(), **_VOICE_KW}):
            recorder = _Recorder()
            cache = _FakeCache()
            kw = dict(kwargs)
            if "cache" in kw:
                kw["cache"] = cache
            if "backfill" in kw:
                kw["backfill"] = recorder
            d, player = _director(tmp_path, **kw)
            await d._fire(0)
            assert len(player.plays) == 1, f"纯资产照播: {list(kwargs)}"
            assert cache.lookups == [] and recorder.texts == []

    _run(_case())


def test_resolver_failure_pure_asset_no_backfill(tmp_path):
    """resolver 抛异常 → 纯资产兜底,不 lookup 不 backfill(try 包住,不毒化开火)。"""

    async def _case():
        def _boom():
            raise RuntimeError("voice resolver down")

        recorder = _Recorder()
        cache = _FakeCache()
        d, player = _director(tmp_path, cache=cache, voice_model_resolver=_boom, backfill=recorder)
        await d._fire(0)
        assert len(player.plays) == 1, "resolver 失败=纯资产照播"
        assert cache.lookups == [] and recorder.texts == []

    _run(_case())


def test_backfill_failure_does_not_poison(tmp_path):
    """backfill 抛异常 → 吞掉打日志,本次出声不受影响,后续 fire 照常。"""

    async def _case():
        async def _boom(text):
            raise RuntimeError("cloud down")

        d, player = _director(tmp_path, cache=_FakeCache(), backfill=_boom, **_VOICE_KW)
        await d._fire(0)
        assert len(player.plays) == 1, "补物化失败不毒化本次出声"
        await asyncio.sleep(0.05)  # 让任务吞异常
        d._handle = None  # 模拟播完
        await d._fire(0)
        assert len(player.plays) == 2, "异常后编排器照常工作"

    _run(_case())


def test_backfill_env_off(tmp_path, monkeypatch):
    """BOK_FILLER_BACKFILL=0 → miss 照播资产但不补物化(物化交还 tts-pregen)。"""
    monkeypatch.setenv("BOK_FILLER_BACKFILL", "0")

    async def _case():
        recorder = _Recorder()
        cache = _FakeCache()
        d, player = _director(tmp_path, cache=cache, backfill=recorder, **_VOICE_KW)
        await d._fire(0)
        assert len(player.plays) == 1, "env 关只关补物化,兜底照播"
        assert cache.lookups != [], "命中层查找照做(下通物化后仍命中)"
        await asyncio.sleep(0.05)
        assert recorder.texts == [], "env 关不触发云合成补物化"

    _run(_case())


# ---- 2026-09-17 call-11132bdd 判污:让话家族清污 + 连轮冷却 ----


def test_filter_deflect_entries_drops_keep_talk_family():
    from agent_runtime.fillers import filter_deflect_entries

    entries = [
        {"text": "好嘅，你稍等一陣，我即刻幫你核實。", "file": "a"},
        {"text": "明白，你繼續講，我聽緊。", "file": "b"},
        {"text": "嗯好，你繼續講，我聽住。", "file": "c"},
        {"text": "Got it please go ahead I am listening.", "file": "d"},
        {"text": "嗯嗯好嘅，我記住喇。", "file": "e"},
        {"text": "Understood I am noting that down.", "file": "f"},
    ]
    kept = filter_deflect_entries(entries)
    assert [e["file"] for e in kept] == ["a", "e", "f"]


def test_load_manifest_filters_deflect_family(tmp_path):
    assets = _make_assets(
        tmp_path,
        {"cantonese": ["好嘅，你等我。", "嗯好，你繼續講，我聽住。"]},
        cats={"cantonese": ["check", "minimal"]},
    )
    m = load_manifest(assets)
    assert [e["text"] for e in m["cantonese"]] == ["好嘅，你等我。"]


def test_consecutive_round_cooldown(tmp_path, monkeypatch):
    """时间窗连发冷却(2026-09-29):窗内跳过、出窗放行——真实通话轮间隔
    (>10s)普遍出窗=慢轮全覆盖;旧「相邻轮歇一轮」把慢轮覆盖打穿已废。"""
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "0")

    async def _case():
        d, player = _director(tmp_path)
        d.arm()  # r1
        await d._fire(0)
        d._handle = None
        assert len(player.plays) == 1
        assert d.fired_this_round() is True
        d.arm()  # r2:上次垫话 10s 窗内 → 跳过
        await d._fire(0)
        assert len(player.plays) == 1, "冷却窗内必须跳过(急连发防轰炸)"
        assert d.fired_this_round() is False
        d.arm()  # r3:模拟真实通话轮间隔(>10s,出窗)→ 放行=慢轮全覆盖
        d._last_fire_at -= 11.0
        await d._fire(0)
        assert len(player.plays) == 2, "出窗后必须放行(旧 seq 交替在此打穿慢轮)"

    _run(_case())


def test_filler_cooldown_disabled_by_env(tmp_path, monkeypatch):
    """BOK_FILLER_COOLDOWN_S=0 → 冷却全关:连发也不跳(懒 delay+每通上限兜)。"""
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "0")
    monkeypatch.setenv("BOK_FILLER_COOLDOWN_S", "0")

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await d._fire(0)
        d._handle = None
        d.arm()
        await d._fire(0)
        assert len(player.plays) == 2, "0=关窗,不得跳过"

    _run(_case())


# ---- W2a 分层犹豫垫音(2026-09-23):hesitation 第六类资产标签,分类器五类
# 永不产出 → _pick 概率混入;犹豫池缺失=零行为变化(短路)。 ----


def _patch_roll(monkeypatch, value: float) -> None:
    import agent_runtime.fillers as fm

    # _RNG=SystemRandom 实例（CWE-338 收编后）：钉实例方法而非 random 模块
    monkeypatch.setattr(fm._RNG, "random", lambda: value)


def test_hesitation_blend_hit_picks_from_hes_pool(tmp_path, monkeypatch):
    """抽签命中(roll < PROB)→ 从 hesitation 池选,即使分类器给了别的类。"""
    _patch_roll(monkeypatch, 0.0)

    async def _case():
        pools = {"cantonese": ["正常垫话一。", "嗯——呃——", "呃——嗯——"]}
        cats = {"cantonese": ["check", "hesitation", "hesitation"]}
        d, _ = _director(tmp_path, pools=pools, cats=cats)
        e = d._pick("cantonese", "check")
        assert e["cat"] == "hesitation", "混入抽签命中必须出自犹豫池"

    _run(_case())


def test_hesitation_blend_miss_keeps_normal_path(tmp_path, monkeypatch):
    """抽签未中(roll ≥ PROB)→ 既有分类池路径逐字节不变。"""
    _patch_roll(monkeypatch, 0.99)

    async def _case():
        pools = {"cantonese": ["正常垫话一。", "嗯——呃——"]}
        cats = {"cantonese": ["check", "hesitation"]}
        d, _ = _director(tmp_path, pools=pools, cats=cats)
        e = d._pick("cantonese", "check")
        assert e["cat"] == "check", "抽签未中不得动分类池路径"

    _run(_case())


def test_no_hesitation_pool_short_circuits(tmp_path, monkeypatch):
    """池里无 hesitation 条目 → 混合分支短路,旧 manifest 行为零变化
    (roll 恒 0 也不得改道——短路靠池空,不靠抽签值)。"""
    _patch_roll(monkeypatch, 0.0)

    async def _case():
        pools = {"cantonese": ["默认垫话。", "应承一。"]}
        cats = {"cantonese": ["default", "ack"]}
        d, _ = _director(tmp_path, pools=pools, cats=cats)
        e = d._pick("cantonese", "default")
        assert e["cat"] == "default"

    _run(_case())


def test_hesitation_blend_respects_recent_dedup(tmp_path, monkeypatch):
    """连续混入抽签同走去重窗(相邻选取不重复);犹豫池 3 条 > 窗 2 必不重。"""
    _patch_roll(monkeypatch, 0.0)

    async def _case():
        pools = {"cantonese": ["嗯——呃——", "呃——嗯——", "呃——哦——"]}
        cats = {"cantonese": ["hesitation"] * 3}
        d, _ = _director(tmp_path, pools=pools, cats=cats)
        picks = [d._pick("cantonese", "check")["text"] for _ in range(3)]
        assert len(set(picks)) == 3, f"犹豫池连续抽签重复: {picks}"

    _run(_case())


def test_real_manifest_hesitation_tier():
    """真实资产契约(W2a):三语各 7 条 hesitation,文件名 h 档前缀,
    时长全在犹豫独立窗 [0.8,2.6]s,wav 在位。"""
    import re

    from agent_runtime.fillers import FILLER_ASSETS_DIR, HESITATION_CAT, load_manifest

    m = load_manifest(FILLER_ASSETS_DIR)
    for lang, entries in m.items():
        hes = [e for e in entries if e.get("cat") == HESITATION_CAT]
        assert len(hes) == 7, f"{lang} hesitation 应 7 条,得 {len(hes)}"
        for e in hes:
            assert re.match(rf"^{lang}-h\d{{2}}\.wav$", e["file"]), e["file"]
            assert 0.8 <= e["dur_s"] <= 2.6, f"{e['file']} dur={e['dur_s']} 出独立窗"
            assert (FILLER_ASSETS_DIR / e["file"]).exists()


# —— W2b 思考态键盘环境音(fillers.thinking_sound_configs) ——————————————


def test_thinking_sound_configs_default(monkeypatch):
    """默认开:两条内置打字 burst,音量 0.6/概率 0.30/fade_out 0.05。"""
    from livekit.agents.voice.background_audio import BuiltinAudioClip

    from agent_runtime.fillers import thinking_sound_configs

    monkeypatch.delenv("BOK_AMBIENT_KEYBOARD", raising=False)
    monkeypatch.delenv("BOK_AMBIENT_KEYBOARD_VOL", raising=False)
    cfgs = thinking_sound_configs()
    assert cfgs is not None and len(cfgs) == 2
    srcs = {c.source for c in cfgs}
    assert srcs == {BuiltinAudioClip.KEYBOARD_TYPING, BuiltinAudioClip.KEYBOARD_TYPING2}
    for c in cfgs:
        assert c.volume == 0.6
        assert c.probability == 0.30
        assert c.fade_out == 0.05


def test_thinking_sound_configs_kill_switch(monkeypatch):
    """BOK_AMBIENT_KEYBOARD=0 → None(构造不带 thinking_sound,行为与旧版同)。"""
    from agent_runtime.fillers import thinking_sound_configs

    monkeypatch.setenv("BOK_AMBIENT_KEYBOARD", "0")
    assert thinking_sound_configs() is None


def test_thinking_sound_configs_volume_clamp(monkeypatch):
    """音量 env:非法值回落 0.6,越界夹 [0,1]。"""
    from agent_runtime.fillers import thinking_sound_configs

    for raw, want in (("1.7", 1.0), ("-0.2", 0.0), ("abc", 0.6), ("0.25", 0.25)):
        monkeypatch.setenv("BOK_AMBIENT_KEYBOARD_VOL", raw)
        cfgs = thinking_sound_configs()
        assert cfgs is not None and all(c.volume == want for c in cfgs), raw


# ---- 2026-10-03 I2/I3(en 垫话审计修复:分类器英文补缺/退额/语言配额/英文桶) ----


def test_classify_filler_category_en_audit_table():
    """en 漏网补缺表驱动(late→empathy、where is my→check、Pinduoduo→ack、
    Okay okay fine→minimal、how do I get compensated→check、无关键词→default)。"""
    from agent_runtime.fillers import classify_filler_category

    cases = {
        "My parcel is a week late.": "empathy",
        "Where is my parcel?": "check",
        "How do I get compensated?": "check",
        "I ordered it on Pinduoduo.": "ack",
        "My tracking number is 5523108.": "ack",
        "Okay okay, fine.": "minimal",
        "Mm-hmm, okay.": "minimal",
        "I will call you back tomorrow.": "default",
    }
    for text, want in cases.items():
        assert classify_filler_category(text) == want, text


def test_filler_max_per_lang_with_env_override(monkeypatch):
    """env 显式=全局硬覆写;坏值=回落语言档(不吞成 0)。"""
    from agent_runtime.fillers import filler_max_per_call

    monkeypatch.setenv("BOK_FILLER_MAX", "9")
    assert filler_max_per_call("en") == 9
    assert filler_max_per_call("zh") == 9
    monkeypatch.setenv("BOK_FILLER_MAX", "bad")
    assert filler_max_per_call("en") == 10


def test_context_bucket_en_keywords():
    """I2:英文 goal/ref 也能拿 comp/query 桶(三通 fired 行 bucket 全空修复)。"""
    from agent_runtime.fillers import derive_context_bucket

    assert derive_context_bucket(has_steps=True, goal="Compensation plan", step_say_done=False) == "query"
    assert derive_context_bucket(has_steps=True, goal="Compensation plan", step_say_done=True) == "comp"
    assert derive_context_bucket(has_steps=True, goal="Track the parcel status", step_index=3) == "query"
    assert derive_context_bucket(has_steps=True, goal="Say hi", step_index=1) == ""


def test_cancel_refunds_short_cut(tmp_path):
    """I3 退额:开播 <0.6s 被新用户轮掐掉的垫话退配额+清冷却账;≥0.6s 不退。"""

    async def _case():
        d, _player = _director(
            tmp_path,
            pools={"cantonese": ["好，等我睇下。", "收到，等陣。", "冇問題。"]},
        )
        await d._fire(0.0)  # 直调开火(绕 arm 定时器)
        assert d._count == 1 and d._play_started > 0
        d.cancel()  # 立即掐(<0.6s)
        assert d._count == 0, "短掐必须退额"
        assert d._last_fire_seq == -2 and d._last_fire_at == 0.0
        await d._fire(0.0)  # 第二发:已播 ≥0.6s 再掐
        assert d._count == 1
        d._play_started = time.monotonic() - 1.0  # 模拟已播 1s
        d.cancel()
        assert d._count == 1, "已播 ≥0.6s 算真出声,不退"

    _run(_case())


def test_set_enabled_instance_override(tmp_path):
    """云档免垫话（2026-10-03）：实例级总闸——False 强制关（装配点按 a_reply
    云档自动置位）、True 强制开（env=0 也开，A/B 用）、None 回 env 模块闸。"""

    async def _case():
        d_off, p_off = _director(tmp_path)
        d_off.set_enabled(False)
        await d_off._fire(0)
        assert p_off.plays == [] and d_off._fired_lines == []
        d_on, p_on = _director(tmp_path)
        d_on.set_enabled(True)
        await d_on._fire(0)
        assert len(p_on.plays) == 1

    _run(_case())


def test_set_enabled_none_follows_env_gate(tmp_path, monkeypatch):
    """None=回 env 模块闸：覆盖优先于 env；回模块闸后 BOK_FILLER=0 生效（旧闸
    语义零变化）。"""

    async def _case():
        d, player = _director(tmp_path)
        d.set_enabled(True)
        assert d._on() is True
        monkeypatch.setenv("BOK_FILLER", "0")
        assert d._on() is True, "实例覆盖优先于 env"
        d.set_enabled(None)
        assert d._on() is False, "回模块闸"
        await d._fire(0)
        assert player.plays == []

    _run(_case())


def test_cloud_auto_gate_wired_in_agent():
    """源级 pin：W1c（2026-10-06）云车道垫话门解禁——装配点走纯函数
    _filler_cloud_gate，默认云车道也 arm，"0" 回旧 auto-off，双向打点在场。"""
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(encoding="utf-8")
    assert "_filler_cloud_gate(" in src
    assert 'os.environ.get("BOK_FILLER_CLOUD", "").strip()' in src
    assert "[agent] filler armed (cloud lane, BOK_FILLER_CLOUD=1)" in src
    assert "[agent] filler auto-off (cloud lane, BOK_FILLER_CLOUD=0)" in src
