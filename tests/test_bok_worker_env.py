"""worker env 白名单透传单测（2026-09-18 实弹发现）。

`_agent_worker_env`/`_agent_prod_env` 是**白名单 env**：**prod** launchd/schtasks
单元只带这份表（dev `serve`/`monitor` 走 `_start_proc`，本就 merge `os.environ`
在前）——不列进表，prod 命令行 `BOK_FLOW_GRAPH=0` 到不了 agent worker，
kill 腿「全程零 FLOW_GRAPH」结构性测不出（探针实弹：worker pid env 无该键，
jump/play 照发 → 腿 FAIL）。本测钉住透传面，防同类逃生门再成死门
（同款教训：`_interp_env` 的 B 线逃生门，2026-09-16）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    """helper 本身：只透传白名单键（absent 不注入），不写别的键。

    清光 `_FORWARD_ENV` 全表环境（2026-09-19 立法后表大——ambient env 会令精确
    等值断言变脆，逐键 delenv；旧版只清 4 键的重审实锤同款教训）。
    """
    monkeypatch.setenv("BOK_FLOW_GRAPH", "0")
    for key in bok._FORWARD_ENV:
        if key != "BOK_FLOW_GRAPH":
            monkeypatch.delenv(key, raising=False)
    env: dict[str, str] = {"KEEP": "1"}
    bok._apply_flow_graph_env(env)
    assert env == {"KEEP": "1", "BOK_FLOW_GRAPH": "0"}


@pytest.mark.parametrize(
    "key",
    ["BOK_QA_ROTATION", "BOK_QA_PRIORITY", "BOK_QA_FASTPATH", "BOK_FLOW_GRAPH_JUDGE"],
)
def test_qa_switch_env_reaches_dev_and_prod_workers(monkeypatch, key):
    """I1（终审）:QA 三逃生门（轮换/优先级/快路）+ 3.4 判据判定开关同款透传——
    dev `_agent_worker_env` 与 prod `_agent_prod_env` 任一漏列即「文档广告死开关」
    （3.1 PRIORITY 曾同病）。

    设定值 → 两表都在；未设/空串 → 两表都不注入（worker 侧按默认跑，零变化）。
    """
    monkeypatch.setenv(key, "0")
    assert bok._agent_worker_env(bok.repo_python()).get(key) == "0"
    assert bok._agent_prod_env().get(key) == "0"

    monkeypatch.delenv(key, raising=False)
    assert key not in bok._agent_worker_env(bok.repo_python())
    assert key not in bok._agent_prod_env()

    monkeypatch.setenv(key, "")
    assert key not in bok._agent_worker_env(bok.repo_python())
    assert key not in bok._agent_prod_env()

    monkeypatch.setenv(key, "1")  # 显式开档（A/B 来回切同一入口）也照传
    assert bok._agent_worker_env(bok.repo_python()).get(key) == "1"
    assert bok._agent_prod_env().get(key) == "1"


def test_apply_bok_passthrough_env_forwards_all_keys(monkeypatch):
    """helper 单点:表内设了的键照值填、未设的键不出现（2026-09-19 立法后全表口径，
    全表批量流到两表的端到端档在 tests/test_forward_env.py）。"""
    for key in bok._FORWARD_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOK_FLOW_GRAPH", "0")
    monkeypatch.setenv("BOK_QA_ROTATION", "0")
    env: dict[str, str] = {}
    bok._apply_bok_passthrough_env(env)
    assert env == {"BOK_FLOW_GRAPH": "0", "BOK_QA_ROTATION": "0"}
