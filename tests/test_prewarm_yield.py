"""prefix prewarm 并发让位(2026-09-30 call-9af18da5 × call-2ec8eec6 定案)。

病灶:A 线 worker 单进程多 job,第二通电话冷启动的 prefix prewarm(~6k token
全量 prefill,+6.2s 实测)排进 LLM 队列代理单槽,把在途通话的交互轮 header
拖到 4-5s、TTFT 5.3-6.8s、tps 崩到 5.8,连锁 MiniMax bidi FLUSH_TIMEOUT。

契约:entrypoint 注册/回收 ``_ACTIVE_CALLS``;prewarm 任务体在「另有在途
通话」时跳过(``BOK_PREFIX_PREWARM_YIELD=0`` 回旧档);判据纯函数单点。"""
from __future__ import annotations

import os
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


def test_ghost_marker_dead_pid_ignored_and_cleaned(tmp_path, monkeypatch):
    """幽灵让位根修(2026-10-02 实弹):异常收线通话不跑 release,marker 残留
    死 pid——旧 mtime 窗让它压制后续所有通话 prewarm 长达 2h(每通首 LLM 轮
    cached=0 全量 prefill 9.8s)。死 pid 判据:跳过+顺手清文件。

    pid 判活走测试缝 monkeypatch(_pid_alive 本体由纯函数测试覆盖),marker
    解析/跳过/清理是被钉的行为。"""
    import agent_runtime.agent as agent_mod
    from agent_runtime.agent import _other_active_calls, _register_active_call

    monkeypatch.setenv("BOK_ACTIVE_CALLS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_mod, "_pid_alive", lambda pid: False)
    ghost = tmp_path / "call-ghost.marker"
    ghost.write_text("4194303\nAJ_dead", encoding="utf-8")
    _register_active_call("call-me", "AJ_me")
    # 注册面写的是本进程 pid,被同 monkeypatch 判死也无妨——被测对象是 ghost
    assert _other_active_calls("call-me") == [], "死 pid marker 不得让位"
    assert not ghost.exists(), "死 pid marker 应被顺手清"
    _release_active_call("call-me")


def test_live_pid_marker_still_yields(tmp_path, monkeypatch):
    """活 pid marker(真在途通话)照常让位——判活回归面。"""
    import agent_runtime.agent as agent_mod
    from agent_runtime.agent import _other_active_calls

    monkeypatch.setenv("BOK_ACTIVE_CALLS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_mod, "_pid_alive", lambda pid: True)
    (tmp_path / "call-live.marker").write_text(f"{os.getpid()}\nAJ_live", encoding="utf-8")
    assert _other_active_calls("call-me") == ["call-live"]


def test_legacy_marker_falls_back_to_mtime(tmp_path, monkeypatch):
    """老格式 marker（内容非 pid,升级前写入）回落旧 mtime 窗:新鲜=算在途。"""
    from agent_runtime.agent import _other_active_calls

    monkeypatch.setenv("BOK_ACTIVE_CALLS_DIR", str(tmp_path))
    (tmp_path / "call-legacy.marker").write_text("call-legacy\n", encoding="utf-8")
    assert _other_active_calls("call-me") == ["call-legacy"]


def test_pid_alive_pure_function():
    from agent_runtime.agent import _pid_alive

    assert _pid_alive(0) is False
    assert _pid_alive(-5) is False
