"""PrefillSpeculator 单测（2026-09-10 抢跑防抖替代件）。

契约核心=严格前缀：投机 messages 必须等于「上一条真实请求 messages +
assistant 回复历史原文 + user(稳定前缀+尾部)」——真请求在 user 文本分叉前
逐字节一致,mlx 前缀缓存才命中。门控：busy 不开火/每轮限次/间隔/前缀增长。
"""

from __future__ import annotations

import asyncio
import os

from agent_runtime.prefill_speculator import PrefillSpeculator


class _FakeCtx:
    def __init__(self, tail: str = "【第1/5步】"):
        self._tail = tail

    def render_context_tail(self) -> str:
        return self._tail


class _FakePrewarm:
    def __init__(self):
        self.calls: list[list[dict]] = []

    async def __call__(self, messages: list[dict]) -> None:
        self.calls.append(messages)


def _spec(tail="【第1/5步】") -> tuple[PrefillSpeculator, _FakePrewarm]:
    prewarm = _FakePrewarm()
    return PrefillSpeculator(prewarm, _FakeCtx(tail)), prewarm


_REQ = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "你好。\n\n【尾部】"},
    {"role": "assistant", "content": "<expr>开场白"},
]


def _arm(spec: PrefillSpeculator) -> None:
    """喂快照+回复+空闲,并清掉时间戳令间隔门全开。"""
    spec.on_request_messages([dict(m) for m in _REQ])
    spec.on_reply_history_text("<expr>好的客户")
    spec.set_busy(False)
    spec._last_fire_ts = 0.0


def test_fire_message_shape_is_strict_prefix():
    """投机 prompt = 快照 + assistant 历史原文 + user(前缀+尾部)。"""

    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")  # create_task 需在 loop 内
        assert spec._task is not None
        await spec._task
        return prewarm

    prewarm = asyncio.run(run())
    assert len(prewarm.calls) == 1
    msgs = prewarm.calls[0]
    assert msgs[:3] == _REQ, "快照段必须逐字节等于上一条真实请求"
    assert msgs[3] == {"role": "assistant", "content": "<expr>好的客户"}
    assert msgs[4]["role"] == "user"
    assert msgs[4]["content"] == "你好我想查下我個\n\n【第1/5步】"


def test_busy_gate():
    async def run():
        spec, prewarm = _spec()
        spec.on_request_messages(list(_REQ))
        spec.on_reply_history_text("回复")
        spec.set_busy(True)
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls, "busy 不开火"


def test_dedupe_budget_and_new_turn_reset():
    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await spec._task
        assert len(prewarm.calls) == 1

        # 同长度/更短前缀:去重不开火
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下")
        assert spec._task is None

        # 增长前缀:第二轮开火后烧穿预算(默认 2)
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個集運件")
        await spec._task
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個集運件而家去咗")
        assert spec._task is None, "预算烧穿(默认 2)不再开火"

        # new_turn 归还预算
        spec.new_turn()
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個集運件而家去咗邊")
        await spec._task
        return prewarm

    prewarm = asyncio.run(run())
    assert len(prewarm.calls) == 3


def test_gap_gate_blocks_rapid_refire():
    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await spec._task
        # 刚开火完(时间戳=now),增长前缀被间隔门拦
        spec.on_stable_prefix("你好我想查下我個集運件而家")
        assert spec._task is None
        return prewarm

    asyncio.run(run())


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("BOK_PREFILL_SPEC", "0")

    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想查下我個")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls


def test_no_snapshot_no_fire():
    async def run():
        spec, prewarm = _spec()
        spec.set_busy(False)
        spec._last_fire_ts = 0.0
        spec.on_stable_prefix("你好我想查下我個")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls


def test_short_prefix_no_fire():
    """与 PREFLIGHT 稳定前缀门槛(≥6 字)一致,太短不值得一次请求。"""

    async def run():
        spec, prewarm = _spec()
        _arm(spec)
        spec.on_stable_prefix("你好我想")
        await asyncio.sleep(0)
        return prewarm, spec

    prewarm, spec = asyncio.run(run())
    assert spec._task is None and not prewarm.calls
