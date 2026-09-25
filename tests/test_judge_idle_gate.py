"""judge 闲时让路闸（2026-09-24 修正版）单测——纯 asyncio 时间语义,零 LLM 零真栈。

背景(docs/superpowers/plans/2026-09-24-a-line-flow-latency-intent.md W8 争用画像):
旧空闲窗只认 `agent=="listening"`(播报也结束),2026-09-22 实测 9/9 capped——回复播报
10-19s 普遍超闸上限,窗口结构性够不着。修正=**播报中也算闲**(LLM 生成完+TTS 云端
放音中=GPU 真空闲窗),`agent in (listening, speaking)` 即可开火。本文件钉:

- 空闲窗四象限:speaking=闲(新)、listening+客户静=闲、thinking=忙、客户插话=忙;
- cap=0 回纯固定让路(旧行为逐字节);capped 到点照开火(判定永不负损);
- `_judge_yield_env` 默认 (floor=3, cap=6) 与覆盖/回退。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import _judge_yield_env, _wait_link_idle  # noqa: E402


def test_speaking_window_now_counts_as_idle():
    """新语义核心:播报中(agent==speaking)=4B 闲 → 可开火。旧版此态永不可开火。"""
    link = {"agent": "speaking", "user": ""}
    verdict = asyncio.run(_wait_link_idle(link, floor_s=0, cap_s=1))
    assert verdict == "idle"


def test_listening_and_silent_user_is_idle():
    link = {"agent": "listening", "user": ""}
    assert asyncio.run(_wait_link_idle(link, floor_s=0, cap_s=1)) == "idle"


def test_thinking_is_busy_until_speaking():
    """生成中(thinking)=忙:floor 后仍忙 → 等窗口;转 speaking 才放行。"""
    link = {"agent": "thinking", "user": ""}

    async def run():
        async def flip():
            await asyncio.sleep(0.35)
            link["agent"] = "speaking"

        flip_task = asyncio.create_task(flip())
        verdict = await _wait_link_idle(link, floor_s=0.1, cap_s=3)
        await flip_task
        return verdict

    assert asyncio.run(run()) == "idle"


def test_user_interrupting_blocks_fire():
    """客户插话(user==speaking)=ASR 在解码,忙——即使 AI 在播报也等。"""
    link = {"agent": "speaking", "user": "speaking"}

    async def run():
        async def flip():
            await asyncio.sleep(0.35)
            link["user"] = ""

        flip_task = asyncio.create_task(flip())
        verdict = await _wait_link_idle(link, floor_s=0.1, cap_s=3)
        await flip_task
        return verdict

    assert asyncio.run(run()) == "idle"


def test_busy_until_cap_fires_capped():
    """恒忙(thinking 不转)→ 到 cap 照开火(capped):判定永不负损的兜底语义。"""
    link = {"agent": "thinking", "user": ""}
    t0 = time.monotonic()
    verdict = asyncio.run(_wait_link_idle(link, floor_s=0.05, cap_s=0.4))
    dt = time.monotonic() - t0
    assert verdict == "capped"
    assert dt >= 0.4


def test_cap_zero_is_legacy_fixed_delay():
    """cap=0=旧固定让路:不看链路态,sleep(floor) 直返 disabled。"""
    link = {"agent": "thinking", "user": "speaking"}  # 任何态都唔影响
    t0 = time.monotonic()
    verdict = asyncio.run(_wait_link_idle(link, floor_s=0.05, cap_s=0))
    dt = time.monotonic() - t0
    assert verdict == "disabled"
    assert dt >= 0.05


def test_empty_agent_state_is_busy():
    """装配初期 agent 状态空串=未知 → 保守当忙(只认 listening/speaking 两态)。"""
    link = {"agent": "", "user": ""}
    assert asyncio.run(_wait_link_idle(link, floor_s=0, cap_s=0.3)) == "capped"


def test_yield_env_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("FLOW_JUDGE_DELAY", raising=False)
    monkeypatch.delenv("FLOW_JUDGE_IDLE_CAP", raising=False)
    assert _judge_yield_env() == (3.0, 6.0)  # 2026-09-24:cap 默认 0→6 转正
    monkeypatch.setenv("FLOW_JUDGE_DELAY", "1.5")
    monkeypatch.setenv("FLOW_JUDGE_IDLE_CAP", "9")
    assert _judge_yield_env() == (1.5, 9.0)
    monkeypatch.setenv("FLOW_JUDGE_IDLE_CAP", "0")  # kill-switch 回旧行为
    assert _judge_yield_env() == (1.5, 0.0)
    monkeypatch.setenv("FLOW_JUDGE_IDLE_CAP", "abc")
    monkeypatch.setenv("FLOW_JUDGE_DELAY", "xyz")
    assert _judge_yield_env() == (3.0, 6.0)  # 坏值回默认
