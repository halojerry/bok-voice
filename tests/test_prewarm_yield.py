"""prefix prewarm 并发让位(2026-09-30 call-9af18da5 × call-2ec8eec6 定案)。

病灶:A 线 worker 单进程多 job,第二通电话冷启动的 prefix prewarm(~6k token
全量 prefill,+6.2s 实测)排进 LLM 队列代理单槽,把在途通话的交互轮 header
拖到 4-5s、TTFT 5.3-6.8s、tps 崩到 5.8,连锁 MiniMax bidi FLUSH_TIMEOUT。

契约:entrypoint 注册/回收 ``_ACTIVE_CALLS``;prewarm 任务体在「另有在途
通话」时跳过(``BOK_PREFIX_PREWARM_YIELD=0`` 回旧档);判据纯函数单点。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.agent import _prewarm_should_yield  # noqa: E402

AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)
BOK_SRC = (ROOT / "tools" / "bok.py").read_text(encoding="utf-8")


def test_should_yield_returns_other_room():
    assert _prewarm_should_yield({"call-a", "call-b"}, "call-a") == "call-b"
    assert _prewarm_should_yield({"call-a"}, "call-b") == "call-a"


def test_should_yield_none_when_alone_or_empty():
    assert _prewarm_should_yield(set(), "call-a") is None
    assert _prewarm_should_yield({"call-a"}, "call-a") is None
    assert _prewarm_should_yield({}, "call-a") is None


def test_wiring_source_pins():
    """注册/回收/门/env 立法四点接线源级 pin。"""
    assert "_register_active_call(room_name" in AGENT_SRC
    assert "_release_active_call(room_name)" in AGENT_SRC
    assert "_prewarm_should_yield(_ACTIVE_CALLS, room_name)" in AGENT_SRC
    assert "_other_active_calls(room_name)" in AGENT_SRC
    assert 'os.environ.get("BOK_PREFIX_PREWARM_YIELD", "1") == "1"' in AGENT_SRC
    assert "llm prefix prewarm yielded (concurrent call" in AGENT_SRC
    assert '"BOK_PREFIX_PREWARM_YIELD"' in BOK_SRC
    assert '"BOK_ACTIVE_CALLS_DIR"' in BOK_SRC


def test_other_active_calls_file_registry(tmp_path, monkeypatch):
    """跨进程标记面:一 job 一子进程(实弹验证纯 set 零触发,2026-10-01 修正)。"""
    import os
    import time as _t

    from agent_runtime.agent import (
        _other_active_calls,
        _register_active_call,
        _release_active_call,
    )

    monkeypatch.setenv("BOK_ACTIVE_CALLS_DIR", str(tmp_path))
    _register_active_call("call-a", "AJ_1")
    _register_active_call("call-b", "AJ_2")
    assert sorted(_other_active_calls("call-a")) == ["call-b"]
    assert _other_active_calls("call-c") == ["call-a", "call-b"] or sorted(
        _other_active_calls("call-c")
    ) == ["call-a", "call-b"]
    # 回收后消失
    _release_active_call("call-b")
    assert _other_active_calls("call-a") == []
    # 陈旧标记(>max_age)=崩溃残留,忽略
    _register_active_call("call-stale", "AJ_3")
    stale = tmp_path / "call-stale.marker"
    old = _t.time() - 7200.0 - 60
    os.utime(stale, (old, old))
    assert _other_active_calls("call-a") == []
    _release_active_call("call-a")
    _release_active_call("call-stale")
