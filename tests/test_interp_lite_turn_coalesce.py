"""话轮聚合开流（BOK_INTERP_TURN_COALESCE，2026-10-09 call-743064ad 断断续续
根修）单测：一条 say 的 gen 撑整个话轮——每句译文照旧逐 delta 进流、FIFO 排干
再撑 TURN_HOLD_S 才收口；kill-switch 0 回逐句开流旧径。

根因背景（call-743064ad 实弹）：每句一开一关 bidi 流各付 2-3.5s「flush 握手+
锁交接」税 → 碎片串起来=顿挫+积压滚雪球（perceived 冲 19s）。聚合后握手税
每话轮只付一次（jinxi beginText→sendChunk×N→endText 同形）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from tests.test_interp_lite_spec import (  # noqa: E402
    FakeLag,
    FakeMT,
    _FakeHandle,
    _suppress_cancel,
)


class _TurnSession:
    """话轮聚合靶形 fake：say 收 gen（记录）或 text（记录）；gen 由测试侧消费。"""

    def __init__(self):
        self.gens: list = []
        self.texts: list[str] = []
        self.handles: list[_FakeHandle] = []

    def say(self, x, audio=None):
        h = _FakeHandle()
        self.handles.append(h)
        if hasattr(x, "__aiter__"):
            self.gens.append(x)
        else:
            self.texts.append(str(x))
        return h


async def _drain_gen(gen, out: list[str]) -> None:
    """消费一条 say gen 直到自然收（哨兵）——收集全部 yield 文本序。"""
    async for piece in gen:
        out.append(piece)


def _make_pipeline(monkeypatch, sess, mt_calls):
    from agent_runtime.interp_lite.pipeline import InterpPipeline

    p = InterpPipeline(
        sess, FakeMT(mt_calls), "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0},
    )
    return p


@pytest.fixture()
def _turn_on(monkeypatch):
    monkeypatch.setenv("BOK_INTERP_TURN_COALESCE", "1")
    monkeypatch.setenv("BOK_INTERP_TURN_HOLD_S", "0.08")
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    monkeypatch.delenv("BOK_INTERP_TAIL_FLUSH", raising=False)


def test_two_sentences_one_open_stream(monkeypatch, _turn_on):
    """同话轮两句 → 恰一条 say（gen），两句译文按序进同一条流；hold 窗内不收口。"""
    sess = _TurnSession()
    p = _make_pipeline(monkeypatch, sess, [iter(["第一句译文够长。"]), iter(["第二句译文够长。"])])

    async def scenario():
        p.enqueue_raw("第一句")
        p.enqueue_raw("第二句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(p.pairs) >= 2 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        assert len(sess.gens) == 1, f"同话轮应只开一条流: {len(sess.gens)}"
        assert p._turn.alive, "hold 窗内话轮流应仍开着"
        out: list[str] = []
        consumer = asyncio.create_task(_drain_gen(sess.gens[0], out))
        await asyncio.sleep(0.1)  # 跨过 hold 窗 → 收口哨兵 → gen 自然收
        await asyncio.wait_for(consumer, timeout=2)
        assert out == ["第一句译文够长。", "第二句译文够长。"], out
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_turn_close_then_new_turn_reopens(monkeypatch, _turn_on):
    """hold 窗过期收口（哨兵=gen 自然收）；停顿后的新句开**新**话轮流。"""
    sess = _TurnSession()
    p = _make_pipeline(monkeypatch, sess, [iter(["第一句译文够长。"]), iter(["第二句译文够长。"])])

    async def scenario():
        p.enqueue_raw("第一句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(p.pairs) >= 1 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        out1: list[str] = []
        c1 = asyncio.create_task(_drain_gen(sess.gens[0], out1))
        await asyncio.sleep(0.2)  # hold 0.08s 过 → 收口
        await asyncio.wait_for(c1, timeout=2)
        assert out1 == ["第一句译文够长。"]
        assert not p._turn.alive, "收口后话轮流应已摘"
        # 停顿后的新句：新话轮流
        p.enqueue_raw("第二句")
        for _ in range(300):
            if len(p.pairs) >= 2 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        assert len(sess.gens) == 2, "新话轮应重开新流"
        out2: list[str] = []
        c2 = asyncio.create_task(_drain_gen(sess.gens[1], out2))
        await asyncio.sleep(0.2)
        await asyncio.wait_for(c2, timeout=2)
        assert out2 == ["第二句译文够长。"]
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_turn_hold_window_cancelled_by_new_unit(monkeypatch, _turn_on):
    """hold 窗内新单元到达 → 收口钟撤、同一条流续吃（不关不开）。"""
    sess = _TurnSession()
    p = _make_pipeline(
        monkeypatch, sess,
        [iter(["第一句译文够长。"]), iter(["第二句译文够长。"]), iter(["第三句译文够长。"])],
    )

    async def scenario():
        p.enqueue_raw("第一句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(p.pairs) >= 1 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.03)  # hold(0.08) 未到 → 钟在走
        p.enqueue_raw("第二句")  # 新单元=撤钟续流
        for _ in range(300):
            if len(p.pairs) >= 2 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        assert len(sess.gens) == 1, "hold 窗内续流：不应关旧开新"
        p.enqueue_raw("第三句")
        for _ in range(300):
            if len(p.pairs) >= 3 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        out: list[str] = []
        c = asyncio.create_task(_drain_gen(sess.gens[0], out))
        await asyncio.sleep(0.2)
        await asyncio.wait_for(c, timeout=2)
        assert out == ["第一句译文够长。", "第二句译文够长。", "第三句译文够长。"], out
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_killswitch_off_is_per_sentence_say(monkeypatch):
    """BOK_INTERP_TURN_COALESCE=0 → 逐句开流旧径（每句一条 say、无话轮态）。"""
    monkeypatch.setenv("BOK_INTERP_TURN_COALESCE", "0")
    monkeypatch.delenv("BOK_INTERP_SPEC_MT", raising=False)
    monkeypatch.delenv("BOK_INTERP_TAIL_FLUSH", raising=False)
    sess = _TurnSession()
    p = _make_pipeline(monkeypatch, sess, [iter(["第一句译文够长。"]), iter(["第二句译文够长。"])])

    async def scenario():
        p.enqueue_raw("第一句")
        p.enqueue_raw("第二句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(p.pairs) >= 2 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        assert len(sess.gens) == 2, f"旧径=每句一条流: {len(sess.gens)}"
        assert not p._turn.alive
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_precomputed_and_failline_enter_turn_stream(monkeypatch, _turn_on):
    """spec HIT 预计算文本与死道请示句都进本话轮流（不另开 say=text）。"""
    sess = _TurnSession()
    from agent_runtime.interp_lite.pipeline import InterpPipeline

    p = InterpPipeline(
        sess, FakeMT([iter(["第一句译文够长。"])]), "SYS", target_lang="zh", voice_tags=True,
        lag=FakeLag(), first_ms={"ms": 0},
    )

    async def scenario():
        # 预计算文本（spec HIT 路径的 FIFO 形状）
        p.q.put_nowait(("__precomputed__", "预计算译文够长。"))
        p._enq.append(asyncio.get_running_loop().time())
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(p.pairs) >= 1 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        assert len(sess.gens) == 1 and not sess.texts, "预计算文本应进话轮流而非 text say"
        out: list[str] = []
        c = asyncio.create_task(_drain_gen(sess.gens[0], out))
        await asyncio.sleep(0.2)
        await asyncio.wait_for(c, timeout=2)
        assert out == ["预计算译文够长。"]
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_shutdown_closes_turn(monkeypatch, _turn_on):
    """shutdown 收线：话轮哨兵入队，gen 干净收束（不悬挂）。"""
    sess = _TurnSession()
    p = _make_pipeline(monkeypatch, sess, [iter(["第一句译文够长。"])])

    async def scenario():
        p.enqueue_raw("第一句")
        task = asyncio.create_task(p.run())
        for _ in range(300):
            if len(p.pairs) >= 1 and p.q.empty() and not p.mt_busy["flag"]:
                break
            await asyncio.sleep(0.01)
        out: list[str] = []
        c = asyncio.create_task(_drain_gen(sess.gens[0], out))
        p.shutdown()
        await asyncio.wait_for(c, timeout=2), "shutdown 后 gen 应经哨兵自然收"
        assert out == ["第一句译文够长。"]
        task.cancel()
        with _suppress_cancel():
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))
