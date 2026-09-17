"""RC3 垫话×响应看门狗顺延单测(2026-09-17)。

根因:垫话走 BackgroundAudioPlayer out-of-band 音轨,框架与 watchdog 均不可见
——「垫话盖耳+系统慢」轮被 4s 闸 force-interrupt(50 轮实测开火 14 次、多次
掐掉在途真回复)。修法:FillerDirector.set_on_fired(真正开播即回调)→
agent 侧一次性顺延(_watchdog_extend 纯逻辑:未武装不延/只延一次/拆弹后不延,
arm 复位 extended 旗标);BOK_RESPONSE_WATCHDOG_FILLER_EXT_S 默认 2.0,0=关。
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

from agent_runtime.agent import (  # noqa: E402
    _response_watchdog_filler_ext_s,
    _watchdog_extend,
)
from agent_runtime.fillers import FillerDirector  # noqa: E402

_PCM = (1000).to_bytes(2, "little", signed=True) * 480  # 20ms @24k


# ---- _watchdog_extend 旗标语义(纯逻辑,stub spawn)----


def _armed_state(deadline_in: float = 4.0) -> dict:
    return {
        "task": None,
        "disarmed": False,
        "deadline": time.monotonic() + deadline_in,
        "extended": False,
    }


def _stub_spawn(rec: list):
    def _spawn(delay: float):
        handle = object()  # 哨兵 task 替身(stub 不需要真 task)
        rec.append((delay, handle))
        return handle

    return _spawn


def test_extend_happy_path_remaining_plus_extra():
    rec: list = []
    state = _armed_state(4.0)
    now = time.monotonic()
    assert _watchdog_extend(state, _stub_spawn(rec), 2.0, now) is True
    assert len(rec) == 1
    # 剩余截止(4s)+extra(2s)=6s(时钟误差容限)
    assert 5.8 <= rec[0][0] <= 6.05, rec
    assert state["extended"] is True
    assert state["task"] is rec[0][1], "task 已换成 spawn 产物(spawn 记 (delay, handle))"


def test_extend_not_armed_is_noop():
    rec: list = []
    state = _armed_state()
    state["disarmed"] = True  # 未武装/已拆弹
    assert _watchdog_extend(state, _stub_spawn(rec), 2.0, time.monotonic()) is False
    assert rec == [] and state["extended"] is False


def test_extend_only_once_per_turn():
    rec: list = []
    state = _armed_state()
    now = time.monotonic()
    assert _watchdog_extend(state, _stub_spawn(rec), 2.0, now) is True
    assert _watchdog_extend(state, _stub_spawn(rec), 2.0, now) is False
    assert len(rec) == 1, "每轮只许顺延一次(extended 旗标)"


def test_extend_invalid_extra_and_missing_deadline():
    rec: list = []
    state = _armed_state()
    assert _watchdog_extend(state, _stub_spawn(rec), 0.0, time.monotonic()) is False
    assert _watchdog_extend(state, _stub_spawn(rec), -1.0, time.monotonic()) is False
    broken = _armed_state()
    broken["deadline"] = 0.0  # 旧结构/未记 deadline → 不延(防御)
    assert _watchdog_extend(broken, _stub_spawn(rec), 2.0, time.monotonic()) is False
    assert rec == []


def test_extend_cancels_live_timer_and_reschedules():
    async def _case():
        async def _sleeper():
            await asyncio.sleep(30)

        old = asyncio.create_task(_sleeper())
        rec: list = []
        state = _armed_state(4.0)
        state["task"] = old

        def _spawn(delay: float):
            rec.append(delay)
            return asyncio.create_task(_sleeper())

        assert _watchdog_extend(state, _spawn, 2.0, time.monotonic()) is True
        await asyncio.sleep(0)  # 让取消投递
        assert old.cancelled(), "现行 timer 必须被取消"
        assert rec and 5.8 <= rec[0] <= 6.05, rec
        try:
            await old
        except asyncio.CancelledError:
            pass

    asyncio.run(asyncio.wait_for(_case(), 5))


def test_extend_deadline_passed_floors_at_50ms():
    rec: list = []
    state = _armed_state(-3.0)  # 截止已过(理论上轮已被 fire,防御性)
    assert _watchdog_extend(state, _stub_spawn(rec), 2.0, time.monotonic()) is True
    assert rec and rec[0][0] == 0.05, rec  # 地板 50ms,绝不为负


# ---- env 解析 ----


def test_env_default_two_seconds(monkeypatch):
    monkeypatch.delenv("BOK_RESPONSE_WATCHDOG_FILLER_EXT_S", raising=False)
    assert _response_watchdog_filler_ext_s() == 2.0


def test_env_override_and_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_RESPONSE_WATCHDOG_FILLER_EXT_S", "3.5")
    assert _response_watchdog_filler_ext_s() == 3.5
    monkeypatch.setenv("BOK_RESPONSE_WATCHDOG_FILLER_EXT_S", "0")
    assert _response_watchdog_filler_ext_s() == 0.0
    monkeypatch.setenv("BOK_RESPONSE_WATCHDOG_FILLER_EXT_S", "garbage")
    assert _response_watchdog_filler_ext_s() == 2.0


# ---- FillerDirector.set_on_fired 触发路径(真开播点)----


def _make_assets(tmp_path: Path, pools: dict[str, list[str]]) -> Path:
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
    def __init__(self):
        self.stopped = False
        self._done = asyncio.get_running_loop().create_future()

    def done(self):
        return self._done.done()

    def stop(self):
        self.stopped = True
        if not self._done.done():
            self._done.set_result(True)

    async def wait_for_playout(self):
        await asyncio.shield(self._done)


class _FakePlayer:
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


def _director(tmp_path: Path, *, player=None, guards=None) -> FillerDirector:
    assets = _make_assets(tmp_path, {"cantonese": ["好，等我睇下。"]})
    return FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: "cantonese",
        player=player if player is not None else _FakePlayer(),
        guards=guards or (lambda: False),
        assets_dir=assets,
    )


def _run(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_filler_on_fired_fires_on_real_play(tmp_path):
    """垫话真正开播(player.play 已提交)必须触发 on-fired 回调。"""
    fired: list = []

    async def _case():
        d = _director(tmp_path)
        d.set_on_fired(lambda: fired.append(1))
        await d._fire(0)
        assert d._fired_lines, "垫话应已开播"

    _run(_case())
    assert fired == [1], fired


def test_filler_on_fired_silent_when_guards_abort(tmp_path):
    """未开播(guards 拦下)绝不触发——顺延只跟真出声走。"""
    fired: list = []

    async def _case():
        d = _director(tmp_path, guards=lambda: True)
        d.set_on_fired(lambda: fired.append(1))
        await d._fire(0)

    _run(_case())
    assert fired == [], fired


def test_filler_on_fired_default_none_no_crash(tmp_path):
    async def _case():
        d = _director(tmp_path)
        await d._fire(0)
        assert d._fired_lines, "未注册回调=零行为变化,照常出声"

    _run(_case())


def test_filler_on_fired_exception_never_poisons_playback(tmp_path):
    """回调抛异常 → 吞掉,垫话照常出声(顺延失败绝不阻垫话)。"""
    fired: list = []

    def _boom():
        fired.append(1)
        raise RuntimeError("extend down")

    async def _case():
        d = _director(tmp_path)
        d.set_on_fired(_boom)
        await d._fire(0)
        assert d._fired_lines, "回调炸了垫话也必须出声"

    _run(_case())
    assert fired == [1], fired
