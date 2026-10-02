"""结算闲时门（CP，2026-09-25 车道卫生）——纯 asyncio 时间语义零真栈。

背景：Summarizer 是 3-9s bg 长生成，恰撞「下一通首轮」的回复窗——:1235 队列
代理只管排队，mlx 无抢占，in-flight 的 bg 解码 reply 车道抢不走。门=等
ringing/active/paused 清零再跑（至多 BOK_SETTLE_IDLE_WAIT_S，默认 300s，0=关），
到点照跑保结算不饿死。本文件钉：

- 0 通话立即过（尾通零成本）；有通话等到清零；
- cap 到点返回 capped（结算永不负损）；env 默认/覆盖/坏值/关。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control-plane"))

from control_plane import main as cp  # noqa: E402

_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "control-plane" / "control_plane" / "main.py"
).read_text(encoding="utf-8")


def test_zero_active_returns_now():
    """无活通话=立即放行(最后一通挂断的常见态,零等待成本)。"""
    assert asyncio.run(cp._settle_idle_gate(lambda: 0, poll_s=0.05, cap_s=1)) == "now"


def test_waits_until_quiet():
    """有活通话就等,清零即放行——poll 轮询真实生效。"""
    state = {"polls": 0}

    def fn() -> int:
        state["polls"] += 1
        return 2 if state["polls"] < 3 else 0

    assert asyncio.run(cp._settle_idle_gate(fn, poll_s=0.02, cap_s=5)) == "now"
    assert state["polls"] >= 3


def test_cap_reached_returns_capped():
    """恒有活通话 → 到 cap 返回 capped(照跑,结算不饿死)。"""
    import time

    t0 = time.monotonic()
    verdict = asyncio.run(cp._settle_idle_gate(lambda: 1, poll_s=0.02, cap_s=0.08))
    assert verdict == "capped"
    assert time.monotonic() - t0 >= 0.08


def test_env_defaults_overrides_and_off(monkeypatch):
    monkeypatch.delenv("BOK_SETTLE_IDLE_POLL_S", raising=False)
    monkeypatch.delenv("BOK_SETTLE_IDLE_WAIT_S", raising=False)
    assert cp._settle_idle_env() == (15.0, 300.0)
    monkeypatch.setenv("BOK_SETTLE_IDLE_POLL_S", "5")
    monkeypatch.setenv("BOK_SETTLE_IDLE_WAIT_S", "60")
    assert cp._settle_idle_env() == (5.0, 60.0)
    monkeypatch.setenv("BOK_SETTLE_IDLE_WAIT_S", "0")  # 0=关(旧行为)
    assert cp._settle_idle_env() == (5.0, 0.0)
    monkeypatch.setenv("BOK_SETTLE_IDLE_POLL_S", "abc")
    monkeypatch.setenv("BOK_SETTLE_IDLE_WAIT_S", "xyz")
    assert cp._settle_idle_env() == (15.0, 300.0)  # 坏值回默认


def test_settle_core_wires_gate_before_summarizer():
    """接线 pin:闲时门在 _settle_core 内、且位于 Summarizer 调用之前。"""
    i_gate = _SRC.index("_settle_idle_gate(_live_call_count")
    i_summ = _SRC.index("Summarizer().build")
    assert i_gate < i_summ, "闲时门必须在 Summarizer 之前"
    # 判据覆盖 ringing(在振铃的通话随时有 dispatch 进场,最不该撞)。
    _body = _SRC[_SRC.index("def _live_call_count"): _SRC.index("def _settle_idle_env")]
    assert "CallStatus.RINGING.value" in _body


def test_reaper_uses_short_idle_cap():
    """reaper 两处 settle 传 idle_cap_s=30 短档——忙时收割循环节奏不被 300s 门拖死。"""
    assert _SRC.count("await _settle_core(c[\"id\"], idle_cap_s=30.0)") == 2
