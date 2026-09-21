"""judge 让路闸（`_wait_link_idle`）判例。

背景（2026-09-22 `scripts/probe_gpu_contention.py` 实测）：9B judge 跑一次判据形状请求要
**4.5s**，占 GPU 期间 4B 的 prefill 往返从 853ms 涨到 **2228ms（+1375ms）**。旧实现是固定
`FLOW_JUDGE_DELAY` 睡 3 秒，回合长于 3s 时必然撞下一轮 prefill；本闸改成「等到链路真空闲」
（AI 在听 + 客户没在讲），硬上限封顶防「永不开火=流程卡死」。这里钉住四条语义：
空闲即走、忙则等到空闲、客户讲话也算忙、一直忙则到点必须放弃。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _wait_link_idle  # noqa: E402

_IDLE = {"agent": "listening", "user": ""}


def test_idle_link_returns_right_after_floor():
    """链路本来就闲 → 等满 floor 就放行，别多睡。"""
    t0 = time.monotonic()
    verdict = asyncio.run(_wait_link_idle(dict(_IDLE), floor_s=0.1, cap_s=2.0))
    dt = time.monotonic() - t0
    assert verdict == "idle"
    assert 0.1 <= dt < 0.6, f"空闲链路应在 floor 后立刻放行，实测 {dt:.2f}s"


def test_waits_until_link_frees():
    """AI 正在讲 → 等到它转 listening 才放行（这正是避开 prefill 撞车的那个窗）。"""
    link = {"agent": "speaking", "user": ""}

    async def scenario():
        async def _free_later():
            await asyncio.sleep(0.3)
            link["agent"] = "listening"

        asyncio.create_task(_free_later())
        return await _wait_link_idle(link, floor_s=0.05, cap_s=3.0)

    t0 = time.monotonic()
    verdict = asyncio.run(scenario())
    dt = time.monotonic() - t0
    assert verdict == "idle"
    assert dt >= 0.3, f"应等链路转空闲，实测 {dt:.2f}s"


def test_user_speaking_also_counts_as_busy():
    """AI 在听但客户正在讲也不算空闲：此刻开火会撞下一轮的 ASR 解码与 prefill。"""
    verdict = asyncio.run(
        _wait_link_idle({"agent": "listening", "user": "speaking"}, floor_s=0.0, cap_s=0.5)
    )
    assert verdict == "capped"


def test_gives_up_at_cap_when_never_idle():
    """一直忙必须到点放弃：judge 是模糊轮推进的补位，永不开火=流程卡死。"""
    t0 = time.monotonic()
    verdict = asyncio.run(
        _wait_link_idle({"agent": "speaking", "user": "speaking"}, floor_s=0.0, cap_s=0.5)
    )
    dt = time.monotonic() - t0
    assert verdict == "capped"
    assert 0.5 <= dt < 2.0, f"硬上限必须兜住，实测 {dt:.2f}s"


def test_cap_zero_is_disabled_and_keeps_old_semantics():
    """cap=0（默认）= 只睡 floor 就走，逐字节同旧的固定让路——即使链路正忙也照走。

    真栈实测（2026-09-22 soak）：「等空闲」在长回复轮 9/9 都够不着（全 capped），
    所以默认关；这条判例把这个默认钉住，别让「让路」悄悄变成「延迟判定」。
    """
    t0 = time.monotonic()
    verdict = asyncio.run(
        _wait_link_idle({"agent": "speaking", "user": "speaking"}, floor_s=0.2, cap_s=0.0)
    )
    dt = time.monotonic() - t0
    assert verdict == "disabled"
    assert 0.2 <= dt < 0.8, f"关闭档只该睡 floor，实测 {dt:.2f}s"
