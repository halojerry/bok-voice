"""双派发守卫:同房 flock 互斥/fail-open/kill-switch(2026-09-28,call-0105a539 实证)。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.room_claim import RoomClaim, room_claim_enabled  # noqa: E402


def _with_claim_dir(monkeypatch, tmp_path: Path) -> Path:
    d = tmp_path / "room-claims"
    monkeypatch.setenv("BOK_ROOM_CLAIM_DIR", str(d))
    return d


def test_acquire_release_roundtrip(monkeypatch, tmp_path):
    _with_claim_dir(monkeypatch, tmp_path)
    a = RoomClaim("call-abc")
    ok, holder = a.acquire("AJ_1")
    assert ok and holder == ""
    # 第二个认领同房:拒绝,能看到持有者描述
    b = RoomClaim("call-abc")
    ok2, holder2 = b.acquire("AJ_2")
    assert not ok2
    assert "pid=" in holder2 and "AJ_1" in holder2
    # 释放后可再认领
    a.release()
    ok3, _ = b.acquire("AJ_2")
    assert ok3
    b.release()


def test_different_rooms_independent(monkeypatch, tmp_path):
    _with_claim_dir(monkeypatch, tmp_path)
    a = RoomClaim("call-x").acquire("AJ_1")
    b = RoomClaim("call-y").acquire("AJ_2")
    assert a[0] and b[0]


def test_kill_switch_fails_open(monkeypatch, tmp_path):
    monkeypatch.setenv("BOK_ROOM_CLAIM_DIR", str(tmp_path))
    monkeypatch.setenv("BOK_ROOM_CLAIM", "0")
    assert room_claim_enabled() is False
    a = RoomClaim("call-k").acquire("AJ_1")
    b = RoomClaim("call-k").acquire("AJ_2")
    assert a[0] and b[0]  # 关闸=互斥消失(逃生口语义)
    assert a[1] == "kill-switch-off"


def test_unwritable_dir_fails_open(monkeypatch, tmp_path):
    # 锁目录建不出来(父路径是文件)→ 放行而非炸通话
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("BOK_ROOM_CLAIM_DIR", str(blocker / "sub"))
    ok, holder = RoomClaim("call-z").acquire("AJ_1")
    assert ok
    assert "claim-open-failed" in holder


def _holder_child(claim_dir: str, lockfile: Path) -> None:  # pragma: no cover - 子进程体
    os.environ["BOK_ROOM_CLAIM_DIR"] = claim_dir
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
    from agent_runtime.room_claim import RoomClaim

    c = RoomClaim("call-cross")
    ok, _ = c.acquire("AJ_child")
    if not ok:
        os._exit(3)
    lockfile.write_text("held", encoding="utf-8")  # 告知父进程:已持锁
    time.sleep(30)
    c.release()


def test_cross_process_mutex_and_auto_release(monkeypatch, tmp_path):
    """子进程持锁 → 父进程被拒;子进程死亡 → flock 自动释放,父进程可拿。

    fork 上下文(无 exec、无命令串):子进程体只做认领+信号+睡眠。"""
    import multiprocessing

    d = _with_claim_dir(monkeypatch, tmp_path)
    lockfile = d / "call-cross.signal"
    ctx = multiprocessing.get_context("fork")
    child = ctx.Process(target=_holder_child, args=(str(d), lockfile), daemon=True)
    child.start()
    try:
        for _ in range(100):
            if lockfile.exists():
                break
            time.sleep(0.05)
        assert lockfile.exists(), "子进程未能在 5s 内持锁"
        parent = RoomClaim("call-cross")
        ok, holder = parent.acquire("AJ_parent")
        assert not ok and "AJ_child" in holder
        child.kill()
        child.join(timeout=10)
        time.sleep(0.1)  # 内核释放 flock
        ok2, _ = parent.acquire("AJ_parent")
        assert ok2  # 持锁进程死亡=锁自动释放,重派发不受阻
        parent.release()
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=5)


def test_wiring_pinned():
    """源级 pin:entrypoint 顶守卫 + shutdown 释放 + bok 表登记。"""
    root = Path(__file__).resolve().parents[1]
    agent_src = (root / "apps/agent/agent_runtime/agent.py").read_text(encoding="utf-8")
    assert "from .room_claim import RoomClaim" in agent_src
    assert "[room-claim] duplicate dispatch stand-down" in agent_src
    assert "ctx.add_shutdown_callback(_release_room_claim)" in agent_src
    bok_src = (root / "tools/bok.py").read_text(encoding="utf-8")
    assert '"BOK_ROOM_CLAIM"' in bok_src
    assert '"BOK_ROOM_CLAIM_DIR"' in bok_src
