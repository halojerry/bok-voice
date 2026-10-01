"""垫话让路(2026-10-02 政策翻转,call-4e8d58c1)单测(全离线,零音频零会话)。

旧契约(2026-09-10 拍板)「垫话一旦开播必须播完」,回复由 tts_cache hold 扣到
垫话时间轴结束——实弹账本:真答案首音频 18.4s≈第二发垫话播完 18.5s,垫话把
真答案整段顶到末尾。新契约三件:①on_reply_first_audio 停播 ②hold_if_playing
恒 0 ③reshot 查回复在途让路;总闸 BOK_FILLER_YIELD(缺省 "1",=0 三件全回
旧行为)。测试姿势镜像 tests/test_filler_reshot.py。
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

import agent_runtime.fillers as fillers_mod  # noqa: E402
from agent_runtime.fillers import (  # noqa: E402
    FillerDirector,
    RESHOT_MIN_ELAPSED_S,
    _yield_enabled,
)

_PCM = (1000).to_bytes(2, "little", signed=True) * 480  # 20ms @24k


def _make_assets(tmp_path: Path, entries: list[dict]) -> Path:
    assets = tmp_path / "fillers"
    assets.mkdir(parents=True, exist_ok=True)
    for e in entries:
        with wave.open(str(assets / e["file"]), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(_PCM)
    (assets / "manifest.json").write_text(
        json.dumps({"cantonese": entries}, ensure_ascii=False), encoding="utf-8"
    )
    return assets


class _FakePlayHandle:
    """与官方 PlayHandle 同形:future 驱动 done/stop/wait_for_playout。"""

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


_NO_PLAYER = object()

_DEFAULT_POOL = [
    {"text": "普通应承一句。", "file": "cantonese-01.wav", "dur_s": 2.0, "cat": "default"},
    {"text": "嗯，我而家睇下", "file": "cantonese-h01.wav", "dur_s": 1.1, "cat": "hesitation"},
    {"text": "呃，你等我一陣", "file": "cantonese-h02.wav", "dur_s": 1.2, "cat": "hesitation"},
]


def _director(
    tmp_path,
    *,
    entries: list[dict] | None = None,
    lang="cantonese",
    player=_NO_PLAYER,
    guards=None,
    reply_pending_provider=None,
) -> tuple[FillerDirector, _FakePlayer | None]:
    assets = _make_assets(tmp_path, entries if entries is not None else _DEFAULT_POOL)
    if player is _NO_PLAYER:
        player = _FakePlayer()
    d = FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: lang,
        player=player,
        guards=guards or (lambda: False),
        assets_dir=assets,
        reply_pending_provider=reply_pending_provider,
    )
    return d, player


def _fast_env(monkeypatch) -> None:
    """隔离 env:起播/gap 即刻、链发显式关、reshot/yield 走默认。"""
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "1")
    monkeypatch.setenv("BOK_FILLER_GAP_MS", "50")
    monkeypatch.setenv("BOK_FILLER_MAX", "6")
    monkeypatch.setenv("BOK_FILLER_CHAIN", "0")
    monkeypatch.delenv("BOK_FILLER_RESHOT", raising=False)
    monkeypatch.delenv("BOK_FILLER_YIELD", raising=False)


async def _wait_first_fire(d: FillerDirector, player: _FakePlayer) -> None:
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(player.plays) >= 1:
            return
    raise AssertionError("首发未起播")


async def _payload_round(d: FillerDirector, player: _FakePlayer, arm_shift: float) -> None:
    """驱动「首发已播完」现场:arm→首发起播→arm 时戳回拨→播完。"""
    d.arm()
    await _wait_first_fire(d, player)
    d._arm_time -= arm_shift
    player.handles[0].complete()


def _run(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ---- env 闸 ----


def test_yield_env_default_on_and_kill_switch(monkeypatch):
    monkeypatch.delenv("BOK_FILLER_YIELD", raising=False)
    assert _yield_enabled() is True
    monkeypatch.setenv("BOK_FILLER_YIELD", "0")
    assert _yield_enabled() is False
    monkeypatch.setenv("BOK_FILLER_YIELD", "garbage")
    assert _yield_enabled() is False  # 非 "1" 一律关(同仓 =='1' 读法)


# ---- I1 首音频停播 ----


def test_first_audio_stops_playing_filler(tmp_path, monkeypatch, capsys):
    """yield 默认档:真答案首音频一到即停垫话(0.05s fade 由 PlayHandle.stop 担)。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await _wait_first_fire(d, player)
        assert player.handles[0].stopped is False
        d.on_reply_first_audio()
        assert player.handles[0].stopped is True, "首音频应停掉在播垫话"
        assert d._handle is None
        assert d._reply_audio_seen is True
        assert "FILLER_YIELD stopped at_first_audio" in capsys.readouterr().out

    _run(_case())


def test_first_audio_no_stop_when_yield_off(tmp_path, monkeypatch, capsys):
    """kill-switch=0:首音频不停播(旧契约,回复由 hold 扣压衔接)。"""
    _fast_env(monkeypatch)
    monkeypatch.setenv("BOK_FILLER_YIELD", "0")

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await _wait_first_fire(d, player)
        d.on_reply_first_audio()
        assert player.handles[0].stopped is False, "闸关=停播动作零发生"
        assert d._handle is not None
        assert "FILLER_YIELD" not in capsys.readouterr().out

    _run(_case())


# ---- hold 归零 ----


def test_hold_zero_when_yield_on(tmp_path, monkeypatch, capsys):
    """yield 档:hold_if_playing 恒 0——tts_cache 的 `if hold > 0` 自然不睡。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await _wait_first_fire(d, player)
        # 旧档此刻 hold = pcm(0.02s)+gap(0.05s)-elapsed ≈ 0.07s > 0
        assert d.hold_if_playing() == 0.0
        out = capsys.readouterr().out
        assert "FILLER_YIELD hold=0" in out

    _run(_case())


def test_hold_legacy_value_when_yield_off(tmp_path, monkeypatch):
    """kill-switch=0:hold 仍按垫话时间轴算(旧档 ≥ 剩余 gap 窗)。"""
    _fast_env(monkeypatch)
    monkeypatch.setenv("BOK_FILLER_YIELD", "0")

    async def _case():
        d, player = _director(tmp_path)
        d.arm()
        await _wait_first_fire(d, player)
        hold = d.hold_if_playing()
        assert 0.0 < hold <= 0.08, f"旧档应保留剩余时间轴扣压: {hold}"

    _run(_case())


# ---- I2 reshot 让路 ----


def test_reshot_skipped_when_reply_pending(tmp_path, monkeypatch, capsys):
    """provider 返回晚于本轮 arm 的在途时刻 → 补发让路(skip 位打点)。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(
            tmp_path, reply_pending_provider=lambda: time.monotonic()
        )
        await _payload_round(d, player, arm_shift=RESHOT_MIN_ELAPSED_S + 0.5)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 1, "回复已在途还补发=把真答案继续往后顶"
        assert d._reshot_done is False, "跳过=计数未耗,不污染后续"
        assert "FILLER_YIELD reshot skipped (reply pending)" in capsys.readouterr().out

    _run(_case())


def test_reshot_pending_before_arm_does_not_block(tmp_path, monkeypatch, capsys):
    """provider 时刻早于 arm(上一轮残值/无在途)→ 不拦,reshot 照旧补发。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(
            tmp_path, reply_pending_provider=lambda: time.monotonic() - 100.0
        )
        await _payload_round(d, player, arm_shift=RESHOT_MIN_ELAPSED_S + 0.5)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 2, "早于 arm 的时刻不属于本轮,不得误拦"
        assert "FILLER_YIELD reshot skipped" not in capsys.readouterr().out

    _run(_case())


def test_reshot_provider_none_zero_drift(tmp_path, monkeypatch, capsys):
    """provider=None(裸 provider/嵌入方不传)→ 旧门零漂移:载荷轮照补。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(tmp_path)  # reply_pending_provider=None
        await _payload_round(d, player, arm_shift=RESHOT_MIN_ELAPSED_S + 0.5)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 2 and d._count == 2
        assert "BOK_FILLER reshot fired" in capsys.readouterr().out

    _run(_case())


def test_kill_switch_off_restores_all_three(tmp_path, monkeypatch, capsys):
    """BOK_FILLER_YIELD=0 三态齐回旧:不停播 / hold 原值 / reshot 不查 pending。"""
    _fast_env(monkeypatch)
    monkeypatch.setenv("BOK_FILLER_YIELD", "0")

    async def _case():
        # ①+② 起播瞬态内取证:I1 不停播、hold 仍按时间轴扣压
        d1, p1 = _director(
            tmp_path / "a", reply_pending_provider=lambda: time.monotonic()
        )
        d1.arm()
        await _wait_first_fire(d1, p1)
        assert p1.handles[0].stopped is False, "闸关=首发未被停"
        assert d1.hold_if_playing() > 0, "闸关=hold 按旧时间轴继续扣压"
        # ③ 另一通载荷轮:pending 不拦(旧门)→ 照补第二发
        d2, p2 = _director(
            tmp_path / "b", reply_pending_provider=lambda: time.monotonic()
        )
        await _payload_round(d2, p2, arm_shift=RESHOT_MIN_ELAPSED_S + 0.5)
        await asyncio.sleep(0.2)
        assert len(p2.plays) == 2, "闸关=reshot 不看 pending"
        assert "FILLER_YIELD" not in capsys.readouterr().out, "闸关=让路观测零打点"

    _run(_case())
