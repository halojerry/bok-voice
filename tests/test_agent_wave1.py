"""2026-09-19 审计修复（agent 运行时 Wave1）回归钉：

- max_step_reached 账本：走完不越界、后退跳不缩水（step_max 快照语义修正）；
- judge 专线健康回退：连续空回 ≥2 次降级主 LLM 链（降级不回头）；
- _qa_note_played 容量账本（图播放补记账走同一函数）。

离线纯函数面，不依赖真栈。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))
sys.path.insert(0, str(ROOT / "packages" / "core"))

os.environ.setdefault("BOK_FLOW_GRAPH", "1")

import pytest

agent = pytest.importorskip("agent_runtime.agent")
flow_mod = pytest.importorskip("agent_runtime.flow")

from agent_runtime.flow import FlowController, FlowStep  # noqa: E402


def _fc(n: int = 3) -> FlowController:
    steps = [
        FlowStep(goal=f"g{i}", ref=f"g{i}：正稿。\n如果客户含糊→礼貌重问")
        for i in range(n)
    ]
    return FlowController(steps=steps)


# ---- max_step_reached 账本 ----


def test_max_step_reached_walk_to_end_no_overflow():
    fc = _fc(3)
    for _ in range(10):  # 超步数多推:advance 自钳制,唔越界
        fc.advance()
    assert fc.current == len(fc.steps)
    # 到达的最大步=最后一步步号(len),唔係旧版的 len+1(不存在的步)。
    assert fc.max_step_reached == 3


def test_max_step_reached_backward_jump_keeps_max():
    fc = _fc(3)
    fc.jump_to(2)  # 0-based 索引=第 3 步
    assert fc.max_step_reached == 3
    fc.jump_to(0)  # 后退跳:current 缩水,max 唔跟缩
    assert fc.current == 0 and fc.max_step_reached == 3


def test_max_step_reached_default_one():
    assert _fc(5).max_step_reached == 1


# ---- judge 专线健康回退 ----


def test_judge_base_url_dedicated_then_fallback(monkeypatch):
    monkeypatch.setenv("FLOW_JUDGE_LLM_BASE_URL", "http://127.0.0.1:1237/v1")
    monkeypatch.setenv("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
    cfg = {"base_url": "http://cfg-url/v1"}
    agent._judge_health.update(streak=0, fell_back=False)
    assert agent._judge_base_url(cfg) == "http://127.0.0.1:1237/v1"
    agent._note_judge_outcome(False)
    assert agent._judge_health["fell_back"] is False  # 单次失败唔降级
    agent._note_judge_outcome(False)
    assert agent._judge_health["fell_back"] is True
    # 降级后:env 在也回落主链(llm_cfg base_url 优先于 MLX env——与原解析链同序)。
    assert agent._judge_base_url(cfg) == "http://cfg-url/v1"
    # 降级后成功不升回(降级不回头),重启才复位。
    agent._note_judge_outcome(True)
    assert agent._judge_health["fell_back"] is True
    agent._judge_health.update(streak=0, fell_back=False)  # 还原模块态,勿污染他测试


def test_judge_base_url_no_env_chain_unchanged(monkeypatch):
    monkeypatch.delenv("FLOW_JUDGE_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("MLX_LLM_BASE_URL", "http://127.0.0.1:1235/v1")
    agent._judge_health.update(streak=0, fell_back=False)
    assert agent._judge_base_url({}) == "http://127.0.0.1:1235/v1"
    assert agent._judge_base_url({"base_url": "http://cfg-url/v1"}) == "http://cfg-url/v1"


# ---- 图播放补记轮换账本(容量函数) ----


def test_qa_note_played_cap():
    played: list[str] = []
    for i in range(agent._QA_PLAYED_CAP + 10):
        agent._qa_note_played(played, f"qa-{i}")
    assert len(played) == agent._QA_PLAYED_CAP
    assert played[-1] == f"qa-{agent._QA_PLAYED_CAP + 9}"  # 丢最旧,保最新
