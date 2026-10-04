"""judge 闲时让路闸单测——纯 asyncio 时间语义,零 LLM 零真栈。

背景(docs/superpowers/plans/2026-09-24-a-line-flow-latency-intent.md W8 争用画像;
2026-10-02 编排梳理波复标):
- 旧空闲窗只认 `agent=="listening"`(播报也结束),2026-09-22 实测 9/9 capped——
  修正=**播报中也算闲**(LLM 生成完+TTS 云端放音中=GPU 真空闲窗)。
- 2026-10-02 复标:judge 与 reply 已分端点(十七波 a_reply=:1237 / judge=:1235),
  同槽互斥消失只剩 GPU 错峰 → **用户话中窗放行**(不再要求 user!=speaking)、
  floor 3→1 / idle cap 6→3;capped→skip 收窄(仅 agent==thinking 才跳)。

本文件钉:
- 空闲窗四象限:speaking=闲、listening+客户静=闲、**listening/speaking+客户讲=闲(新)**、
  thinking=忙;
- cap=0 回纯固定让路(旧行为逐字节);capped 到点照开火(判定永不负损);
- `_judge_yield_env` 默认 (floor=1, cap=3) 与覆盖/回退;
- capped→skip 只认「capped 且明示生成中」(收窄矩阵见 test_orch_cleanup_agent)。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from _bok_src import bok_source  # noqa: E402

from agent_runtime.agent import (  # noqa: E402
    _judge_capped_skip_enabled,
    _judge_yield_env,
    _wait_link_idle,
)

_SRC = (
    Path(__file__).resolve().parents[1] / "apps" / "agent" / "agent_runtime" / "agent.py"
).read_text(encoding="utf-8")


def test_speaking_window_now_counts_as_idle():
    """播报中(agent==speaking)=4B 闲 → 可开火。旧版此态永不可开火。"""
    link = {"agent": "speaking", "user": ""}
    verdict = asyncio.run(_wait_link_idle(link, floor_s=0, cap_s=1))
    assert verdict == "idle"


def test_listening_and_silent_user_is_idle():
    link = {"agent": "listening", "user": ""}
    assert asyncio.run(_wait_link_idle(link, floor_s=0, cap_s=1)) == "idle"


def test_user_speaking_window_allowed():
    """2026-10-02 复标:客户插话不再阻塞(分端点后无同槽互斥,用户话中窗直接跑)。"""
    assert asyncio.run(_wait_link_idle({"agent": "listening", "user": "speaking"}, floor_s=0, cap_s=0.3)) == "idle"
    assert asyncio.run(_wait_link_idle({"agent": "speaking", "user": "speaking"}, floor_s=0, cap_s=0.3)) == "idle"


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


def test_thinking_with_user_speaking_is_idle():
    """用户话中窗放行是**无条件**的:thinking+客户讲也放(预生成窗不阻 judge)。"""
    link = {"agent": "thinking", "user": "speaking"}
    assert asyncio.run(_wait_link_idle(link, floor_s=0, cap_s=0.3)) == "idle"


def test_busy_until_cap_fires_capped():
    """恒忙(thinking 且客户静)→ 到 cap 照开火(capped):判定永不负损的兜底语义。"""
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
    assert _judge_yield_env() == (1.0, 3.0)  # 2026-10-02 复标:3/6 → 1/3(分端点后放宽)
    monkeypatch.setenv("FLOW_JUDGE_DELAY", "3")
    monkeypatch.setenv("FLOW_JUDGE_IDLE_CAP", "6")
    assert _judge_yield_env() == (3.0, 6.0)  # env 覆盖=恢复排队时代旧闸
    monkeypatch.setenv("FLOW_JUDGE_IDLE_CAP", "0")  # kill-switch 回旧行为
    assert _judge_yield_env() == (3.0, 0.0)
    monkeypatch.setenv("FLOW_JUDGE_IDLE_CAP", "abc")
    monkeypatch.setenv("FLOW_JUDGE_DELAY", "xyz")
    assert _judge_yield_env() == (1.0, 3.0)  # 坏值回默认


# ---- capped→skip（2026-09-25 车道卫生;2026-10-02 收窄） -----------------------


def test_capped_skip_env_default_on(monkeypatch):
    """总闸默认开（允许收窄跳过）;BOK_JUDGE_CAPPED_SKIP=0 回「到点照开火」。"""
    monkeypatch.delenv("BOK_JUDGE_CAPPED_SKIP", raising=False)
    assert _judge_capped_skip_enabled() is True
    monkeypatch.setenv("BOK_JUDGE_CAPPED_SKIP", "0")
    assert _judge_capped_skip_enabled() is False


def test_both_judges_gate_on_capped_narrowed():
    """接线 pin:flow judge 与 intent judge 两路都走收窄判定（纯函数矩阵另行钉）。"""
    assert _SRC.count("verdict = await _judge_yield()") >= 2, "两路 judge 都应捕获 verdict"
    assert _SRC.count("_judge_capped_should_skip(_yield_verdict, _link)") == 2
    assert "[judge] skipped reason=capped" in _SRC
    assert "FLOW_GRAPH judge_skipped reason=capped" in _SRC
    # env 立法:新键必须进 _FORWARD_ENV(prod 封闭 env 面可达)。
    bok_src = bok_source()
    assert '"BOK_JUDGE_CAPPED_SKIP",' in bok_src
