"""judge 让路闸（`_wait_link_idle`）判例——HEAD 2026-10-02 语义。

背景（2026-09-22 `scripts/probes/probe_gpu_contention.py` 实测）：9B judge 跑一次判据形状请求要
**4.5s**，占 GPU 期间 4B 的 prefill 往返从 853ms 涨到 **2228ms（+1375ms）**。旧实现是固定
`FLOW_JUDGE_DELAY` 睡 3 秒，回合长于 3s 时必然撞下一轮 prefill；本闸改成「等到链路空闲窗」
+ 硬上限封顶防「永不开火=流程卡死」。

2026-10-02 复标（judge 与 reply 已分端点 :1235/:1237，同槽互斥消失）：空闲窗=agent
播报/倾听**或用户话中窗**（客户在讲不构成互斥，直接放行）；唯一「忙」=agent 还在生成
（thinking/未知空态且用户静默）。本文件钉住四条 HEAD 语义：空闲即走、生成中等到转闲、
**用户话中窗放行（origin「客户讲话也算忙」已翻转）**、一直忙则到点必须放弃(capped)。
旧「等链路真空闲（还要求客户静默）」语义与完整四象限矩阵见 tests/test_judge_idle_gate.py。
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
    """AI 正在生成（thinking）=忙 → 等到它转闲才放行（这正是要错开的 prefill 窗）。

    HEAD 忙态只剩「生成中」一种：分端点后用户话中窗放行，agent 转
    listening/speaking 即开火——两个时序边界都钉（等到了 + 没拖到 cap）。
    """
    link = {"agent": "thinking", "user": ""}

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
    assert dt < 1.0, f"转闲后应立即放行（不该拖到 cap），实测 {dt:.2f}s"


def test_user_speaking_window_is_allowed():
    """HEAD 翻转（origin 的 `user_speaking_also_counts_as_busy` 已作废）：

    用户话中窗**放行**——分端点后 judge 进 LLM 不再与回复同槽，客户在讲不构成
    互斥。钉住「立即 idle、不耗 cap」，即客户话中窗不再被当成忙等。
    """
    t0 = time.monotonic()
    verdict = asyncio.run(
        _wait_link_idle({"agent": "listening", "user": "speaking"}, floor_s=0.0, cap_s=0.5)
    )
    dt = time.monotonic() - t0
    assert verdict == "idle"
    assert dt < 0.3, f"用户话中窗应立即放行，实测 {dt:.2f}s"


def test_gives_up_at_cap_when_never_idle():
    """一直忙（agent 生成中且用户静默）必须到点放弃：judge 是模糊轮推进的补位，
    永不开火=流程卡死。capped 到点照开火（收窄跳过见 `_judge_capped_should_skip`）。"""
    t0 = time.monotonic()
    verdict = asyncio.run(
        _wait_link_idle({"agent": "thinking", "user": ""}, floor_s=0.1, cap_s=0.5)
    )
    dt = time.monotonic() - t0
    assert verdict == "capped"
    assert 0.5 <= dt < 2.0, f"硬上限必须兜住，实测 {dt:.2f}s"


def test_cap_zero_is_disabled_and_keeps_old_semantics():
    """cap=0（显式关，此时才回固定让路档）= 只睡 floor 就走，逐字节同旧的固定让路。

    HEAD 缺省 cap=3（见 `_judge_yield_env`）——本档是显式 kill-switch：不看链路态、
    即使 agent 正忙也照走。真栈实测（2026-09-22 soak）：「等空闲」在长回复轮 9/9
    都够不着（全 capped），当时默认关；HEAD 分端点后重开为缺省 3 并收窄 capped
    跳过，本判例钉住显式关闭档语义不被静默改回。
    """
    t0 = time.monotonic()
    verdict = asyncio.run(
        _wait_link_idle({"agent": "speaking", "user": "speaking"}, floor_s=0.2, cap_s=0.0)
    )
    dt = time.monotonic() - t0
    assert verdict == "disabled"
    assert 0.2 <= dt < 0.8, f"关闭档只该睡 floor，实测 {dt:.2f}s"
