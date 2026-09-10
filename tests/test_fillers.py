"""FillerDirector 单测(2026-09-10 资产化改版):manifest 池/语言铁律(绝不跨语言)/
限次/轮换防重/播放排序契约(首音频不掐垫话+hold 扣压=垫话剩余+gap)/用户插话停播/kill-switch。

资产契约:垫话=随源码分发的 wav+manifest(apps/agent/agent_runtime/assets/fillers/),
运行时只播文件绝不云合成;语言=装配时钉死的通话语言,池缺失明文跳过。
"""

from __future__ import annotations

import asyncio
import json
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.fillers import FillerDirector, filler_gap_s, load_manifest  # noqa: E402

_PCM = (1000).to_bytes(2, "little", signed=True) * 480  # 20ms @24k


def _make_assets(tmp_path: Path, pools: dict[str, list[str]]) -> Path:
    """微型 wav+manifest;音频本身 20ms,时长断言走 manifest dur_s(权威)。"""
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
            entries.append({"text": text, "file": name, "dur_s": 1.0})
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
    lang="cantonese",
    player=_NO_PLAYER,
    guards=None,
):
    pools = pools if pools is not None else {
        "cantonese": ["好，等我睇下。", "好，等一陣。"],
        "zh": ["好的，您稍等。"],
        "en": ["Sure."],
    }
    assets = _make_assets(tmp_path, pools)
    if player is _NO_PLAYER:
        player = _FakePlayer()
    d = FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: lang,
        player=player,
        guards=guards or (lambda: False),
        assets_dir=assets,
    )
    return d, player


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


def test_reply_first_audio_does_not_stop_playing(tmp_path):
    """播放排序契约:首音频到达只作废定时器,在播垫话必须播完(不掐)。"""

    async def _case():
        d, player = _director(tmp_path)
        await d._fire(0)
        handle = player.handles[-1]
        d.on_reply_first_audio()
        assert handle.stopped is False, "垫话在播被掐=违反播放排序契约"
        assert d.hold_if_playing() > 0, "在播垫话应触发回复扣压"

    _run(_case())


def test_hold_if_playing_remaining_plus_gap(tmp_path):
    async def _case():
        d, player = _director(tmp_path)
        assert d.hold_if_playing() == 0.0  # 未播
        await d._fire(0)
        hold = d.hold_if_playing()
        assert 1.2 <= hold <= 1.31, f"hold 应≈dur(1.0)+gap(0.3): {hold}"
        player.handles[-1].stopped = True  # 直接置标志不改 done;用 done 模拟播完
        d._handle = type(player.handles[-1])()
        # 直接置 done 句柄:hold 归零
        class _Done:
            def done(self):
                return True

        d._handle = _Done()
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


def test_max_per_call_and_rotation(tmp_path):
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
    assert filler_gap_s() == 0.3
    monkeypatch.setenv("BOK_FILLER_GAP_MS", "500")
    assert filler_gap_s() == 0.5


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
    """首条播完、回复首音频仍未到 → gap 后自动补第二发(不重样、计数同源)。"""
    for k, v in _CHAIN_ENV.items():
        monkeypatch.setenv(k, v)

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await asyncio.sleep(0.05)  # 首发起播
        assert len(player.plays) == 1 and d._count == 1
        player.handles[0].complete()  # 垫话1 自然播完
        await asyncio.sleep(0.15)  # gap + 观察者补位
        assert len(player.plays) == 2 and d._count == 2, "播完无回复应链发第二发"
        assert d._fired_lines[0] != d._fired_lines[1], "链发两条不重样"
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
