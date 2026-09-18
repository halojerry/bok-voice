"""worker env 白名单透传单测（2026-09-18 实弹发现）。

`_agent_worker_env`/`_agent_prod_env` 是**白名单 env**（不带 `os.environ`）——
命令行 `BOK_FLOW_GRAPH=0 python tools/bok.py serve` 到不了 agent worker，
kill 腿「全程零 FLOW_GRAPH」结构性测不出（探针实弹：worker pid env 无该键，
jump/play 照发 → 腿 FAIL）。本测钉住透传面，防同类逃生门再成死门
（同款教训：`_interp_env` 的 B 线逃生门，2026-09-16）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402


def test_flow_graph_env_reaches_dev_worker(monkeypatch):
    """serve/monitor 的 A 线 worker env 必须带上 BOK_FLOW_GRAPH=0（kill 腿前提）。"""
    monkeypatch.setenv("BOK_FLOW_GRAPH", "0")
    env = bok._agent_worker_env(bok.repo_python())
    assert env.get("BOK_FLOW_GRAPH") == "0"
    # 同源探针：白名单 env 仍保有既有键（改动是纯增量）
    assert env.get("BOK_SERVICE") == "agent"


def test_flow_graph_env_reaches_prod_env(monkeypatch):
    """prod 单元（launchd/schtasks）走的 _agent_prod_env 同款透传。"""
    monkeypatch.setenv("BOK_FLOW_GRAPH", "0")
    assert bok._agent_prod_env().get("BOK_FLOW_GRAPH") == "0"


def test_flow_graph_env_absent_injects_nothing(monkeypatch):
    """未设/空串 → 不注入（worker 侧按默认 "1" 跑，默认档零变化）。"""
    monkeypatch.delenv("BOK_FLOW_GRAPH", raising=False)
    assert "BOK_FLOW_GRAPH" not in bok._agent_worker_env(bok.repo_python())
    assert "BOK_FLOW_GRAPH" not in bok._agent_prod_env()

    monkeypatch.setenv("BOK_FLOW_GRAPH", "")
    assert "BOK_FLOW_GRAPH" not in bok._agent_worker_env(bok.repo_python())
    assert "BOK_FLOW_GRAPH" not in bok._agent_prod_env()


def test_flow_graph_env_explicit_one_also_propagates(monkeypatch):
    """显式 =1 也照传（不只 kill 档；A/B 来回切同一入口）。"""
    monkeypatch.setenv("BOK_FLOW_GRAPH", "1")
    assert bok._agent_worker_env(bok.repo_python()).get("BOK_FLOW_GRAPH") == "1"
    assert bok._agent_prod_env().get("BOK_FLOW_GRAPH") == "1"


def test_apply_flow_graph_env_is_pure_dict_fill(monkeypatch):
    """helper 本身：只填 dict，不读不写别的键。"""
    monkeypatch.setenv("BOK_FLOW_GRAPH", "0")
    env: dict[str, str] = {}
    bok._apply_flow_graph_env(env)
    assert env == {"BOK_FLOW_GRAPH": "0"}
