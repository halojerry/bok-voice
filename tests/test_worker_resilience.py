"""C6 worker 韧性(2026-09-13):

- _vocab_* 属性完整性门禁:9/12 `_vocab_echo` AttributeError 双向全聋 bug
  (c0fd5b3 引入→5a44e22 修,call-d086678a 整通 0 轮)属「引用了从未赋值的
  self._vocab_* 属性」——源码级扫描一秒拦在 CI。
- worker 端口单例守卫:重复 spawn 撞显式端口从 Errno 48 崩溃变良性退出 0。
- bok.py monitor/worker_specs:三 worker 描述单源(A 线 8081/B 线 8082·8083)。
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))


# ---- C6-3:_vocab_* 属性完整性门禁 ----


def test_qwen3_stream_vocab_attributes_all_assigned():
    """_Qwen3ASRLiveStream 引用的每个 self._vocab* 属性必须有赋值点。

    9/12 bug:_run 里读了 self._vocab_echo(从未赋值,只有 _vocab_echo_seen)
    → AttributeError 炸穿 _stt_pump → 双向全聋。源码扫描:引用集合 ⊆ 赋值集合。
    """
    from agent_runtime.providers.livekit_plugins import _Qwen3ASRLiveStream

    src = inspect.getsource(_Qwen3ASRLiveStream)
    referenced = set(re.findall(r"self\.(_vocab\w+)", src))
    assigned = set(re.findall(r"self\.(_vocab\w+)\s*(?::[^=\n]+)?=", src))
    missing = referenced - assigned
    assert not missing, (
        f"_Qwen3ASRLiveStream 引用了从未赋值的属性: {sorted(missing)} ——"
        "运行时必炸 AttributeError(_vocab_echo 全聋 bug 同族),请在 __init__ 补赋值"
    )


def test_vocab_gate_catches_the_912_bug_shape():
    """门禁自证:复刻 9/12 bug 形状(读 _vocab_echo、只赋 _vocab_echo_seen)必红。"""
    src = "self._vocab_echo_seen = False\n        if self._vocab_echo:\n            pass\n"
    referenced = set(re.findall(r"self\.(_vocab\w+)", src))
    assigned = set(re.findall(r"self\.(_vocab\w+)\s*(?::[^=\n]+)?=", src))
    assert referenced - assigned == {"_vocab_echo"}


# ---- C6-2:端口单例守卫 ----


def test_worker_guard_exits_clean_when_port_occupied(capsys):
    from agent_runtime.worker_guard import worker_port_singleton_guard

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            worker_port_singleton_guard(port, "test-worker")
            raise AssertionError("should have exited")
        except SystemExit as e:
            assert e.code == 0
        assert "WORKER_ALREADY_RUNNING" in capsys.readouterr().out


def test_worker_guard_passes_when_port_free(capsys):
    from agent_runtime.worker_guard import worker_port_singleton_guard

    # 找一个确定空闲的端口(绑定后立刻释放,竞态窗可忽略)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    worker_port_singleton_guard(port, "test-worker")  # 不应退出
    assert "WORKER_ALREADY_RUNNING" not in capsys.readouterr().out


# ---- C6-1:bok.py monitor/worker_specs 单源 ----


def _import_bok():
    spec = importlib.util.spec_from_file_location("bok_tools", ROOT / "tools" / "bok.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_worker_specs_three_workers_ports_and_env():
    bok = _import_bok()
    specs = bok._worker_specs(sys.executable)
    assert [s["port"] for s in specs] == [8081, 8082, 8083]
    assert [s["name"] for s in specs] == ["agent", "interp-fwd", "interp-rev"]
    for s in specs:
        assert "LIVEKIT_URL" in s["env"] and "PYTHONPATH" in s["env"]
        assert s["argv"][1:] == ["-m", "agent_runtime.main"] or s["argv"][1:] == ["-m", "agent_runtime.interpret"]
    # interp env 带方向
    assert specs[1]["env"]["INTERP_DIRECTION"] == "fwd"
    assert specs[2]["env"]["INTERP_DIRECTION"] == "rev"
