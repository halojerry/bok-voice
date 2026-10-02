"""down pid 复用闸（2026-10-02 编排审计第二波 · PR-E item 3）。

背景：`cmd_down` 按 pidfile 清杀只过 `_pid_origin_foreign`（他树守卫）——
stale pidfile 的 pid 被无关进程复用时来源「未知」→ fail-open 照杀 = 误杀无辜
进程。`_pidfile_alive_stamped` 已有 lstart 比对（monitor 单例判定），本波把
同款判据接到清杀路径：戳 `proc-<pid>.root` 记的 lstart 与 live 进程 ps 现值
不一致 → SKIP kill + 留痕；无戳（旧式启动）/ps 读不出 → fail-open 旧语义。

隔离纪律照 tests/test_bok_stamp_guard.py：tmp HOME + spawn 假 worker（自立
会话，killpg 安全）；ps 输出按需 monkeypatch；全量清扫路径打桩——绝不碰真栈。
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX killpg/载体语义")


def _sleeper_main() -> None:
    with contextlib.suppress(Exception):
        os.setsid()
    time.sleep(600.0)


class _FakeWorkers:
    def __init__(self) -> None:
        self._procs: list[multiprocessing.process.BaseProcess] = []
        self._ctx = multiprocessing.get_context("spawn")

    def spawn(self) -> multiprocessing.process.BaseProcess:
        proc = self._ctx.Process(target=_sleeper_main, daemon=True)
        proc.start()
        self._procs.append(proc)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                grouped = os.getpgid(proc.pid) != os.getpgrp()
            except OSError:
                grouped = False
            if grouped:
                return proc
            time.sleep(0.2)
        pytest.fail(f"假 worker pid={proc.pid} 未就绪")

    def cleanup(self) -> None:
        for proc in self._procs:
            with contextlib.suppress(Exception):
                if proc.is_alive():
                    proc.terminate()
        for proc in self._procs:
            with contextlib.suppress(Exception):
                proc.join(timeout=5)


def _tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    (bok.app_data_dir() / "run").mkdir(parents=True, exist_ok=True)
    return home


def _run_dir(home: Path) -> Path:
    return bok.app_data_dir() / "run"


def _patch_down_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bok, "_sweep_orphan_workers", lambda: [])
    monkeypatch.setattr(bok, "_sweep_orphan_listeners", lambda healthy_ok=True: [])


def _assert_alive(proc) -> None:
    assert proc.is_alive(), "pid 复用闸失效=无辜进程被误杀"


def _wait_dead(proc, timeout_s: float = 10.0) -> None:
    proc.join(timeout_s)
    assert not proc.is_alive(), "放行语义失效：该杀的没杀"


# ---------------------------------------------------------------------------
# 判定原语
# ---------------------------------------------------------------------------


def test_pid_reused_stale_helper(monkeypatch, tmp_path) -> None:
    """戳 lstart ≠ live ps lstart → True；无戳/坏戳/ps 读不出 → False。"""
    home = _tmp_home(monkeypatch, tmp_path)
    run = _run_dir(home)
    pidfile = run / "agent.pid"
    pidfile.write_text("12345")
    assert bok._pid_reused_stale(pidfile, 12345) is False  # 无戳 fail-open

    (run / "proc-12345.root").write_text(f"{bok.ROOT}\tOLD LSTART\n", encoding="utf-8")
    monkeypatch.setattr(bok, "_ps_field", lambda pid, field: "NEW LSTART" if field == "lstart=" else "")
    assert bok._pid_reused_stale(pidfile, 12345) is True

    monkeypatch.setattr(bok, "_ps_field", lambda pid, field: "OLD LSTART" if field == "lstart=" else "")
    assert bok._pid_reused_stale(pidfile, 12345) is False  # 戳对得上=不是复用

    monkeypatch.setattr(bok, "_ps_field", lambda pid, field: "")  # ps 读不出
    assert bok._pid_reused_stale(pidfile, 12345) is False

    (run / "proc-12345.root").write_text(f"{bok.ROOT}\n", encoding="utf-8")  # 坏戳
    monkeypatch.setattr(bok, "_ps_field", lambda pid, field: "NEW LSTART" if field == "lstart=" else "")
    assert bok._pid_reused_stale(pidfile, 12345) is False


# ---------------------------------------------------------------------------
# cmd_down 清杀路径
# ---------------------------------------------------------------------------


def test_cmd_down_skips_reused_pid_stamp(monkeypatch, tmp_path, capsys) -> None:
    """stale pidfile（pid 被复用）→ SKIP kill + 留痕；进程存活。"""
    home = _tmp_home(monkeypatch, tmp_path)
    _patch_down_sweeps(monkeypatch)
    pool = _FakeWorkers()
    try:
        proc = pool.spawn()
        run = _run_dir(home)
        (run / "agent.pid").write_text(str(proc.pid))
        (run / f"proc-{proc.pid}.root").write_text(
            f"{bok.ROOT}\tSTALE LSTART\n", encoding="utf-8")
        monkeypatch.setattr(
            bok, "_ps_field",
            lambda pid, field: "LIVE LSTART" if field == "lstart=" else "")
        rc = bok.cmd_down()
        assert rc == 0
        _assert_alive(proc)
        out = capsys.readouterr().out
        assert f"[bok] stale pidfile agent pid={proc.pid} reused — skip kill" in out
    finally:
        pool.cleanup()


def test_cmd_down_kills_when_stamp_matches(monkeypatch, tmp_path, capsys) -> None:
    """戳与 live lstart 一致（真本树进程）→ 照旧收割（闸不误挡）。"""
    home = _tmp_home(monkeypatch, tmp_path)
    _patch_down_sweeps(monkeypatch)
    pool = _FakeWorkers()
    try:
        proc = pool.spawn()
        run = _run_dir(home)
        (run / "tts.pid").write_text(str(proc.pid))
        (run / f"proc-{proc.pid}.root").write_text(
            f"{bok.ROOT}\tMATCH LSTART\n", encoding="utf-8")
        monkeypatch.setattr(
            bok, "_ps_field",
            lambda pid, field: "MATCH LSTART" if field == "lstart=" else "")
        rc = bok.cmd_down()
        assert rc == 0
        _wait_dead(proc)
        out = capsys.readouterr().out
        assert f"[down] stopped tts (pid {proc.pid})" in out
    finally:
        pool.cleanup()


def test_cmd_down_legacy_sidecar_pid_reuse_guard(monkeypatch, tmp_path, capsys) -> None:
    """legacy data/sidecar-*.pid 扫描同闸（戳按 app-data run 面查找）。"""
    home = _tmp_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    monkeypatch.setattr(bok, "ROOT", repo)
    _patch_down_sweeps(monkeypatch)
    pool = _FakeWorkers()
    try:
        proc = pool.spawn()
        (repo / "data" / "sidecar-asr.pid").write_text(str(proc.pid))
        (_run_dir(home) / f"proc-{proc.pid}.root").write_text(
            f"{repo}\tSTALE LSTART\n", encoding="utf-8")
        monkeypatch.setattr(
            bok, "_ps_field",
            lambda pid, field: "LIVE LSTART" if field == "lstart=" else "")
        rc = bok.cmd_down()
        assert rc == 0
        _assert_alive(proc)
        assert f"[bok] stale pidfile sidecar-asr pid={proc.pid} reused — skip kill" in capsys.readouterr().out
    finally:
        pool.cleanup()
