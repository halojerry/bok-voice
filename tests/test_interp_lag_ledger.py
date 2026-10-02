"""B 线感知延迟账本(RC-8,2026-10-02 刀1)。

生产同传到底多慢此前无账可查:CP turns 行 latency_ms 只装 MT 墙钟,
started/ended/perceived_ms 三列 A 线在写、B 线全空。本档钉住:

- `_LagLedger` FIFO 配对(源句 final → MT 完成 → 译文 item 落地)顺序语义;
- `_lag_turn_timing` 三列口径(通话相对毫秒,镜像 A 线;perceived>0 单调桩);
- worker 接线源级 pin(INTERP_LAG 观测行、add_turn 三列转发、非交付路径 drop_src);
- `ControlPlaneClient.add_turn` 三字段透传(monkeypatched 客户端捕获)。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.control_plane import ControlPlaneClient  # noqa: E402

from agent_runtime.interpret import (  # noqa: E402
    _lag_turn_timing,
    _LagLedger,
)

INTERP_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py").read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------------------------
# _LagLedger:FIFO 配对
# ---------------------------------------------------------------------------


def test_lag_ledger_order_preservation():
    """两条 FIFO 同序配对:先到的源句先被 done_mt,先配对先 pop。"""
    clock = {"v": 10.0}
    led = _LagLedger(clock=lambda: clock["v"])
    led.note_src("first")  # 5 字
    clock["v"] = 10.5
    led.note_src("second!!")  # 8 字
    clock["v"] = 11.0
    led.done_mt(120)  # 弹 first
    clock["v"] = 11.2
    led.done_mt(90)  # 弹 second
    assert led.pop_pending() == (10.0, 5, 120)
    assert led.pop_pending() == (10.5, 8, 90)
    assert led.pop_pending() is None


def test_lag_ledger_empty_pop_returns_none():
    """空 pop=None(兜底句/异常轮不落时间列);空账上的 drop/done 都是 no-op。"""
    led = _LagLedger(clock=lambda: 1.0)
    assert led.pop_pending() is None
    led.drop_src()
    led.done_mt(5)
    assert led.pop_pending() is None


def test_lag_ledger_drop_src_keeps_alignment():
    """未产出译文的句(摘译/空译文/失败)消费 src 头——后续句配对不错位。"""
    led = _LagLedger(clock=lambda: 1.0)
    led.note_src("skipped")
    led.note_src("delivered")
    led.drop_src()  # skipped 不给译文
    led.done_mt(55)  # delivered 才是配对的头
    rec = led.pop_pending()
    assert rec is not None
    assert rec[1] == len("delivered")
    assert rec[2] == 55
    assert led.pop_pending() is None


def test_lag_ledger_note_src_counts_chars_not_bytes():
    led = _LagLedger(clock=lambda: 0.0)
    led.note_src("译文abc")
    led.done_mt(1)
    assert led.pop_pending()[1] == len("译文abc") == 5  # str 长度(中英混排按字符)


def test_lag_ledger_fallback_say_gets_own_pairing():
    """MT 兜底句也配对记账(2026-10-02 错位根修回归钉)。

    旧 bug:超时/异常分支 drop_src 后 session.say(_mt_fail_line(...)) 出声——
    兜底 item 落地时 _on_item 的 pop_pending **偷弹下一条真译文的 pending**,
    三列时间轴整体错一位、末条永不弹。修法=say 成功即 done_mt(0)(mt_ms=0=
    非真译但 perceived 诚实);say 失败才 drop_src(无 item 不留孤儿)。"""
    led = _LagLedger(clock=lambda: 1.0)
    led.note_src("超时句")
    led.note_src("真译文句")
    # 超时轮:say 出声 → done_mt(0)(兜底句自己的配对);不再是 drop_src
    led.done_mt(0)
    rec_fallback = led.pop_pending()
    assert rec_fallback is not None
    assert rec_fallback[1] == len("超时句")
    assert rec_fallback[2] == 0, "兜底句 mt_ms=0(非真译)"
    # 下一句真译文的配对不被偷弹
    led.done_mt(240)
    rec_real = led.pop_pending()
    assert rec_real == (1.0, len("真译文句"), 240)
    assert led.pop_pending() is None


def test_lag_turn_timing_relative_ms_mirrors_aline():
    """三列口径:started/ended=通话相对毫秒(减当通基线 t0),perceived=墙钟差。"""
    rec = (100.0, 7, 250)
    started_ms, ended_ms, perceived_ms = _lag_turn_timing(rec, now=103.4, t0=90.0)
    assert started_ms == 10000  # (100.0-90.0)*1000
    assert ended_ms == 13400  # (103.4-90.0)*1000
    assert perceived_ms == 3400  # 100.0→103.4
    assert perceived_ms > 0


def test_lag_turn_timing_monotonic_stub_positive():
    """单调桩:perceived 随 now 前进单调不减(源句时间戳固定)。"""
    rec = (50.0, 3, 10)
    prev = -1
    for now in (50.1, 51.0, 52.5):
        _s, _e, perceived = _lag_turn_timing(rec, now=now, t0=0.0)
        assert perceived > prev
        prev = perceived


# ---------------------------------------------------------------------------
# ControlPlaneClient.add_turn:三字段透传
# ---------------------------------------------------------------------------


def test_cp_client_add_turn_forwards_timing_fields():
    """add_turn 把 started/ended/perceived_ms 原样放进请求 params(缺省 0 兼容)。"""

    async def _main() -> dict:
        client = ControlPlaneClient("http://127.0.0.1:1", call_id="call-x")
        captured: dict = {}

        async def _fake_post(url: str, params: dict):
            captured["url"] = url
            captured["params"] = params
            return object()

        client._post_turn_once = _fake_post  # type: ignore[method-assign]
        try:
            await client.add_turn(
                "call-x", "me", "译文：hello",
                provider="interpret", latency_ms=7, language="en",
                line="b", speaker="me",
                started_ms=1000, ended_ms=3200, perceived_ms=2200,
            )
        finally:
            await client.aclose()
        return captured

    captured = asyncio.run(_main())
    assert captured["url"] == "/api/calls/call-x/turns"
    params = captured["params"]
    assert params["latency_ms"] == 7
    assert params["started_ms"] == 1000
    assert params["ended_ms"] == 3200
    assert params["perceived_ms"] == 2200
    assert params["line"] == "b" and params["speaker"] == "me"


def test_cp_client_add_turn_defaults_zero_unchanged():
    """不传三字段=0(旧调用逐字节同,CP 侧按 0 当缺省)。"""

    async def _main() -> dict:
        client = ControlPlaneClient("http://127.0.0.1:1", call_id="call-x")
        captured: dict = {}

        async def _fake_post(url: str, params: dict):
            captured["params"] = params
            return object()

        client._post_turn_once = _fake_post  # type: ignore[method-assign]
        try:
            await client.add_turn("call-x", "me", "原文：hi", language="zh")
        finally:
            await client.aclose()
        return captured

    params = asyncio.run(_main())["params"]
    assert params["started_ms"] == 0
    assert params["ended_ms"] == 0
    assert params["perceived_ms"] == 0


# ---------------------------------------------------------------------------
# worker 接线源级 pin
# ---------------------------------------------------------------------------


def test_interp_lag_wiring_source_pins():
    """接线 pin:账本三钩子 + INTERP_LAG 观测行 + 非交付路径头对齐。"""
    assert "[interp] INTERP_LAG src_chars=" in INTERP_SRC
    assert "perceived_ms={_perceived_ms}" in INTERP_SRC
    assert "_lag.note_src(text)" in INTERP_SRC  # 源句入账(入队成功后)
    assert "_lag.done_mt(" in INTERP_SRC  # MT 成功→pending
    assert "_lag.pop_pending()" in INTERP_SRC  # item 落地→配对
    assert "_lag_turn_timing(" in INTERP_SRC  # 三列换算单点
    assert "_t0 = time.monotonic()" in INTERP_SRC  # 当通单调基线
    # 非交付路径(积压摘译/MT 空/超时/异常)必须消费 src 头,否则后续句全错配。
    assert INTERP_SRC.count("_lag.drop_src()") == 4


def test_interp_add_turn_forwards_three_fields_source_pins():
    """译文行 add_turn 带三列;原文行/无配对路径保持缺省 0(旧行为)。"""
    assert "started_ms=started_ms, ended_ms=ended_ms, perceived_ms=perceived_ms" in INTERP_SRC
    assert "started_ms=_started_ms, ended_ms=_ended_ms, perceived_ms=_perceived_ms" in INTERP_SRC
    assert "started_ms: int = 0" in INTERP_SRC
    assert "ended_ms: int = 0" in INTERP_SRC
    assert "perceived_ms: int = 0" in INTERP_SRC
