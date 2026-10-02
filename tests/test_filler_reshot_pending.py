"""reshot pending 时间窗(2026-10-02 P2 二次立法)单测(全离线,零音频零会话)。

背景:政策翻转(hold=0)后 reshot 的 pending 门被一刀切——只要回复流「已开未
出声」就跳过补发;post-fix 实弹 4 skip/0 fire=真 bidi 卡顿窗裸静默(watchdog
闸前死区重新露出来)。二次立法=时长判据:
- 在途 <RESHOT_PENDING_GRACE_S(2s)=新近在途,首音频将至——补发会被
  on_reply_first_audio 的停播立刻掐掉,白烧额度,让路;
- 在途 >=2s=真卡顿(pending 本身即病理)——放行第二发填死区(裸静默更贵)。
skip 观测行保留旧前缀(既有子串断言/soak grep 兼容),新理由追加在后;
放行路径打 pending 值归因。测试姿势镜像 tests/test_filler_yield.py。
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
    RESHOT_MIN_ELAPSED_S,
    RESHOT_PENDING_GRACE_S,
    FillerDirector,
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


_DEFAULT_POOL = [
    {"text": "普通应承一句。", "file": "cantonese-01.wav", "dur_s": 2.0, "cat": "default"},
    {"text": "嗯，我而家睇下", "file": "cantonese-h01.wav", "dur_s": 1.1, "cat": "hesitation"},
    {"text": "呃，你等我一陣", "file": "cantonese-h02.wav", "dur_s": 1.2, "cat": "hesitation"},
]


def _director(tmp_path, *, reply_pending_provider=None) -> tuple[FillerDirector, _FakePlayer]:
    assets = _make_assets(tmp_path, _DEFAULT_POOL)
    player = _FakePlayer()
    d = FillerDirector(
        _FakeSession(),
        lang_resolver=lambda: "cantonese",
        player=player,
        guards=lambda: False,
        assets_dir=assets,
        reply_pending_provider=reply_pending_provider,
    )
    return d, player


def _fast_env(monkeypatch) -> None:
    """隔离 env:起播/gap 即刻、链发显式关、reshot/yield 走默认。"""
    monkeypatch.setenv("BOK_FILLER_DELAY_MS", "1")
    monkeypatch.setenv("BOK_FILLER_GAP_MS", "10")
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


async def _payload_round(d: FillerDirector, player: _FakePlayer) -> None:
    """驱动「首发已播完」现场:arm→首发起播→arm 时戳回拨(模拟载荷轮 elapsed)→播完。"""
    d.arm()
    await _wait_first_fire(d, player)
    d._arm_time -= RESHOT_MIN_ELAPSED_S + 0.5  # elapsed 过闸
    player.handles[0].complete()


def _run(coro, timeout=5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ---- 常量 / 纯判据 ----


def test_pending_grace_constant_pinned():
    # 宽限=2s:与 elapsed 闸(2.2s)同量级——在途久悬才视为真卡顿窗。
    assert RESHOT_PENDING_GRACE_S == 2.0


def test_pending_hold_returns_duration(tmp_path, monkeypatch):
    """判据纯面:在途时长返回秒数;闸关/provider 缺席/无 arm/早于 arm → 0.0。"""
    _fast_env(monkeypatch)

    async def _case():
        d, _ = _director(tmp_path, reply_pending_provider=lambda: time.monotonic() - 0.5)
        assert d._reply_pending_hold_s() == 0.0, "无 arm 账本不适用"
        d._arm_time = time.monotonic() - 3.0
        hold = d._reply_pending_hold_s()
        assert 0.4 < hold < 0.8, f"应返回在途时长: {hold}"
        # 早于 arm 的残值(上一轮)→ 0.0
        d2, _ = _director(tmp_path / "b", reply_pending_provider=lambda: time.monotonic() - 100.0)
        d2._arm_time = time.monotonic()
        assert d2._reply_pending_hold_s() == 0.0
        # provider 缺席 → 0.0
        d3, _ = _director(tmp_path / "c")
        d3._arm_time = time.monotonic()
        assert d3._reply_pending_hold_s() == 0.0
        # yield 闸关 → 0.0(不看 provider)
        monkeypatch.setenv("BOK_FILLER_YIELD", "0")
        d._arm_time = time.monotonic() - 3.0
        assert d._reply_pending_hold_s() == 0.0

    _run(_case())


# ---- 观察段矩阵(pending 0.5s 拦 / 2.5s 放) ----


def test_reshot_pending_fresh_blocks_with_new_reason(tmp_path, monkeypatch, capsys):
    """在途 0.5s(首音频将至)→ 让路;文案带新理由,旧前缀保持兼容。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(
            tmp_path, reply_pending_provider=lambda: time.monotonic() - 0.5
        )
        await _payload_round(d, player)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 1, "新近在途时补发=白烧额度(立刻被停播)"
        assert d._reshot_done is False, "跳过=计数未耗"
        out = capsys.readouterr().out
        assert "FILLER_YIELD reshot skipped (reply pending)" in out, "旧前缀兼容"
        assert "first audio imminent" in out, "新理由=首音频将至"

    _run(_case())


def test_reshot_pending_stalled_releases_second_fire(tmp_path, monkeypatch, capsys):
    """在途 2.5s(真 bidi 卡顿/LLM 慢窗,裸静默风险)→ 放行第二发填死区。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(
            tmp_path, reply_pending_provider=lambda: time.monotonic() - 2.5
        )
        await _payload_round(d, player)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 2 and d._count == 2, "久悬在途必须补发(裸静默更贵)"
        assert d._reshot_done is True
        out = capsys.readouterr().out
        assert "FILLER_YIELD reshot pending" in out and "stalled" in out, "放行归因行"
        assert "BOK_FILLER reshot fired" in out

    _run(_case())


def test_reshot_pending_boundary_two_seconds_still_blocks(tmp_path, monkeypatch):
    """边界:恰 2.0s 以内(含)仍让路——严格 >GRACE 才放行(判据无歧义)。"""
    _fast_env(monkeypatch)

    async def _case():
        # provider 固定返回 arm 前 2.0s 的时刻:since <= arm → 视为本轮(>arm),
        # 在途时长随调度略 >2.0——用 1.9s 保证落在让路侧。
        d, player = _director(
            tmp_path, reply_pending_provider=lambda: time.monotonic() - 1.9
        )
        await _payload_round(d, player)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 1

    _run(_case())


# ---- _fire(reshot=True) 第二道门(直调/竞态) ----


def test_fire_reshot_second_gate_blocks_fresh_pending(tmp_path, monkeypatch, capsys):
    """_fire 直调(观察段与开火之间回复新近在途)→ 第二道门拦下,不叠音。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(
            tmp_path, reply_pending_provider=lambda: time.monotonic() - 0.4
        )
        d.arm()
        await _wait_first_fire(d, player)
        d._arm_time -= 3.0  # 本轮在途(晚于 arm);首发已播完
        player.handles[0].complete()
        d._handle = None
        d._reshot_done = True  # 模拟观察段已放行(冷却豁免),直调第二道门
        before = d._count
        await d._fire(0.0, reshot=True)
        assert d._count == before, "第二道门必须拦住新近在途的补发"
        assert "first audio imminent" in capsys.readouterr().out

    _run(_case())


def test_fire_reshot_second_gate_releases_stalled_pending(tmp_path, monkeypatch, capsys):
    """_fire 直调 + 在途久悬 → 第二道门放行(与观察段同判据,分叉即回滚)。"""
    _fast_env(monkeypatch)

    async def _case():
        d, player = _director(
            tmp_path, reply_pending_provider=lambda: time.monotonic() - 2.6
        )
        d.arm()
        await _wait_first_fire(d, player)
        d._arm_time -= 3.0  # 本轮在途(晚于 arm);首发已播完
        player.handles[0].complete()
        d._handle = None
        d._reshot_done = True
        before = d._count
        await d._fire(0.0, reshot=True)
        assert d._count == before + 1, "久悬在途第二道门应放行"
        assert "BOK_FILLER reshot fired" in capsys.readouterr().out

    _run(_case())


# ---- kill-switch 第三态:yield 关 → pending 不参与判定 ----


def test_yield_off_ignores_pending_entirely(tmp_path, monkeypatch, capsys):
    """BOK_FILLER_YIELD=0:新近在途也不拦(旧门),零让路打点。"""
    _fast_env(monkeypatch)
    monkeypatch.setenv("BOK_FILLER_YIELD", "0")

    async def _case():
        d, player = _director(
            tmp_path, reply_pending_provider=lambda: time.monotonic() - 0.3
        )
        await _payload_round(d, player)
        await asyncio.sleep(0.2)
        assert len(player.plays) == 2, "闸关=reshot 不看 pending(旧行为)"
        out = capsys.readouterr().out
        assert "FILLER_YIELD" not in out, "闸关=让路观测零打点"

    _run(_case())


def test_forward_env_unchanged_no_new_knobs():
    """P2 是纯时长判据,不新增 env 旋钮(免生未登记 _FORWARD_ENV 的死键)。"""
    src = (ROOT / "apps" / "agent" / "agent_runtime" / "fillers.py").read_text(encoding="utf-8")
    assert "BOK_FILLER_PENDING" not in src and "BOK_FILLER_RESHOT_PENDING" not in src
