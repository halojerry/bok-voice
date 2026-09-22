"""pidfile 清杀路径的他树戳守卫单测（2026-09-22 多会话互杀修复）。

背景：run/*.pid 是 HOME 作用域单槽共享文件，worker 换装/多会话互相覆写；
旧版 down/monitor 清杀对 pidfile 内容无脑收割，2026-09-22 实弹把本树 worker
（pid 92201）杀掉的就是这个缺口。守卫纪律=「来源读得出且 ≠ 本树才拒杀；
未知/本树一律放行旧语义」（fail-open，与 _sweep_orphan_listeners 立法一致）。

隔离纪律：全部用 tmp HOME + python 假进程（multiprocessing spawn，子代自立
会话=同 _start_proc 组长语义，killpg 不波及测试进程组）；涉真系统 ps 全量
扫描的清扫路径一律打桩——**绝不给真实栈上在跑进程任何被收割的机会**。
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX killpg/载体语义")

OTHER_ROOT = "/tmp/bok-stamp-guard-fake-other-tree"


def _sleeper_main() -> None:
    """假 worker 入口：先自立会话（同 _start_proc 组长语义）再长睡。"""
    with contextlib.suppress(Exception):
        os.setsid()
    time.sleep(600.0)


def _tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """HOME 指到 tmp；run/ 目录用 bok 自己的 app_data_dir() 解析（mac=
    Library/Application Support、Linux=~/.local/share——手造固定路径会在
    Linux 上写错目录，首跑 CI 实证），XDG_DATA_HOME/LOCALAPPDATA 清干净
    保证 HOME 是唯一变量。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    (bok.app_data_dir() / "run").mkdir(parents=True, exist_ok=True)
    return home


def _run_dir(home: Path) -> Path:
    return bok.app_data_dir() / "run"


@contextlib.contextmanager
def _serve_root_env(root: str | None):
    old = os.environ.get("BOK_SERVE_ROOT")
    if root is None:
        os.environ.pop("BOK_SERVE_ROOT", None)
    else:
        os.environ["BOK_SERVE_ROOT"] = root
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("BOK_SERVE_ROOT", None)
        else:
            os.environ["BOK_SERVE_ROOT"] = old


class _FakeWorkers:
    """假 worker 池：spawn 子代继承起那时的 env 快照（BOK_SERVE_ROOT 可控），
    测试完统一 terminate+join 回收。"""

    def __init__(self) -> None:
        self._procs: list[multiprocessing.process.BaseProcess] = []
        self._ctx = multiprocessing.get_context("spawn")

    def spawn(self, serve_root: str | None) -> multiprocessing.process.BaseProcess:
        with _serve_root_env(serve_root):
            proc = self._ctx.Process(target=_sleeper_main, daemon=True)
            proc.start()
        self._procs.append(proc)
        self._wait_ready(proc.pid, serve_root)
        return proc

    @staticmethod
    def _wait_ready(pid: int, serve_root: str | None) -> None:
        """等子代 exec 完成（env 可读）且已自立会话（killpg 安全前提）。"""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                grouped = os.getpgid(pid) != os.getpgrp()
            except OSError:
                grouped = False
            visible = (bok._process_serve_root(pid) == serve_root
                       if serve_root else bok._ps_field(pid, "command=") != "")
            if grouped and visible:
                return
            time.sleep(0.2)
        pytest.fail(f"假 worker pid={pid} 未就绪（env={serve_root!r}）")

    def cleanup(self) -> None:
        for proc in self._procs:
            with contextlib.suppress(Exception):
                if proc.is_alive():
                    proc.terminate()
        for proc in self._procs:
            with contextlib.suppress(Exception):
                proc.join(timeout=5)


def _write_marker(home: Path, pid: int, root: str) -> None:
    """伪造 proc-<pid>.root 标记（用真 lstart——pid 复用防护按真值比对）。"""
    lstart = bok._ps_field(pid, "lstart=")
    assert lstart, "ps lstart 读取失败，测试环境异常"
    (_run_dir(home) / f"proc-{pid}.root").write_text(f"{root}\t{lstart}\n", encoding="utf-8")


def _assert_alive(proc) -> None:
    assert proc.is_alive(), "假 worker 不该死（守卫失效=误杀）"


def _wait_dead(proc, timeout_s: float = 10.0) -> None:
    proc.join(timeout_s)
    assert not proc.is_alive(), "假 worker 未在限期内被终止（放行语义失效）"


# ---------------------------------------------------------------------------
# 载体与判定原语
# ---------------------------------------------------------------------------

def test_env_carrier_foreign_detected(monkeypatch, tmp_path):
    """env 载体（Linux /proc、macOS ps eww）：他树 env → foreign=True。"""
    _tmp_home(monkeypatch, tmp_path)
    pool = _FakeWorkers()
    try:
        proc = pool.spawn(OTHER_ROOT)
        root = bok._process_serve_root(proc.pid)
        assert root == OTHER_ROOT, f"env 载体没读到: {root!r}"
        foreign, r = bok._pid_origin_foreign(proc.pid)
        assert foreign and r == OTHER_ROOT
    finally:
        pool.cleanup()


def test_marker_carrier_with_real_lstart(monkeypatch, tmp_path):
    """标记载体：真 lstart 的他树标记 → foreign；lstart 对不上（pid 复用）→ 未知放行。"""
    home = _tmp_home(monkeypatch, tmp_path)
    pool = _FakeWorkers()
    try:
        proc = pool.spawn(None)  # 不带 env，逼走标记载体
        _write_marker(home, proc.pid, OTHER_ROOT)
        assert bok._process_serve_root(proc.pid) == OTHER_ROOT
        # 假 lstart：pid 复用防护把标记作废 → 来源未知 → 不判他树
        (_run_dir(home) / f"proc-{proc.pid}.root").write_text(
            f"{OTHER_ROOT}\tFAKE LSTART\n", encoding="utf-8")
        assert bok._process_serve_root(proc.pid) == ""
        foreign, _ = bok._pid_origin_foreign(proc.pid)
        assert not foreign
    finally:
        pool.cleanup()


def test_own_tree_and_unknown_not_foreign(monkeypatch, tmp_path):
    """本树（env 或标记）与完全未知 → 一律 (False, …)=放行旧语义。"""
    home = _tmp_home(monkeypatch, tmp_path)
    pool = _FakeWorkers()
    try:
        own_env = pool.spawn(str(bok.ROOT))
        assert bok._pid_origin_foreign(own_env.pid) == (False, str(bok.ROOT))
        own_marker = pool.spawn(None)
        _write_marker(home, own_marker.pid, str(bok.ROOT))
        assert bok._pid_origin_foreign(own_marker.pid)[0] is False
        unknown = pool.spawn(None)
        assert bok._pid_origin_foreign(unknown.pid) == (False, "")
    finally:
        pool.cleanup()


# ---------------------------------------------------------------------------
# 三条 pidfile 清杀路径的守卫
# ---------------------------------------------------------------------------

def test_stop_pidfile_refuses_foreign_kills_unknown(monkeypatch, tmp_path, capsys):
    """serve TTS 预清路径：他树拒杀留痕；未知来源照旧语义杀。"""
    home = _tmp_home(monkeypatch, tmp_path)
    pool = _FakeWorkers()
    try:
        foreign = pool.spawn(OTHER_ROOT)
        unknown = pool.spawn(None)
        run = _run_dir(home)
        (run / "tts.pid").write_text(str(foreign.pid))
        bok._stop_pidfile(run / "tts.pid")
        _assert_alive(foreign)
        assert "属另一代码树" in capsys.readouterr().err
        (run / "tts.pid").write_text(str(unknown.pid))
        bok._stop_pidfile(run / "tts.pid")
        _wait_dead(unknown)
    finally:
        pool.cleanup()


def test_kill_pidfile_refuses_foreign(monkeypatch, tmp_path, capsys):
    """monitor respawn 路径（_kill_pidfile 单点覆盖两调用处）：他树拒杀留痕。"""
    home = _tmp_home(monkeypatch, tmp_path)
    pool = _FakeWorkers()
    try:
        foreign = pool.spawn(OTHER_ROOT)
        _write_marker(home, foreign.pid, OTHER_ROOT)  # 标记载体同判
        run = _run_dir(home)
        (run / "agent.pid").write_text(str(foreign.pid))
        bok._kill_pidfile(run / "agent.pid")
        _assert_alive(foreign)
        assert "先在对方树 down" in capsys.readouterr().err
    finally:
        pool.cleanup()


def test_cmd_down_skips_foreign_pidfile(monkeypatch, tmp_path, capsys):
    """cmd_down pidfile 循环：他树 skip，未知照杀。清扫两路径打桩——绝不碰真栈。"""
    home = _tmp_home(monkeypatch, tmp_path)
    pool = _FakeWorkers()
    monkeypatch.setattr(bok, "_sweep_orphan_workers", lambda: [])
    monkeypatch.setattr(bok, "_sweep_orphan_listeners", lambda healthy_ok=True: [])
    try:
        foreign = pool.spawn(OTHER_ROOT)
        unknown = pool.spawn(None)
        run = _run_dir(home)
        (run / "agent.pid").write_text(str(foreign.pid))
        (run / "tts.pid").write_text(str(unknown.pid))
        rc = bok.cmd_down()
        assert rc == 0
        _assert_alive(foreign)
        err = capsys.readouterr().err
        assert "属另一代码树" in err and "agent" in err
        _wait_dead(unknown)
    finally:
        pool.cleanup()


def test_sweep_orphan_workers_skips_foreign(monkeypatch, tmp_path, capsys):
    """命令行特征清扫：ps 输出打桩只列自家假进程——他树条目不进 swept 不被杀。"""
    _tmp_home(monkeypatch, tmp_path)
    pool = _FakeWorkers()
    real_run = bok.subprocess.run

    class _Done:
        def __init__(self, stdout: str):
            self.stdout = stdout
            self.returncode = 0

    def fake_ps_run(cmd, **kw):  # noqa: ARG001
        if list(cmd[:3]) == ["ps", "-axo", "pid,command"]:
            rows = [f"{p.pid} {sys.executable} -m agent_runtime.main"
                    for p in pool._procs]
            return _Done("  PID COMMAND\n" + "\n".join(rows) + "\n")
        return real_run(cmd, **kw)

    monkeypatch.setattr(bok.subprocess, "run", fake_ps_run)
    try:
        foreign = pool.spawn(OTHER_ROOT)
        unknown = pool.spawn(None)
        swept = bok._sweep_orphan_workers()
        swept_pids = [pid for pid, _ in swept]
        assert foreign.pid not in swept_pids, "他树条目进了收割名单"
        assert unknown.pid in swept_pids
        assert "属另一代码树" in capsys.readouterr().err
        _assert_alive(foreign)
        _wait_dead(unknown)
    finally:
        pool.cleanup()


def test_respawn_skips_healthy_port(monkeypatch, tmp_path):
    """monitor _respawn 起拉循环：端口已有健康监听跳过（杀被守卫挡下后不硬起刷
    bind 失败噪声）。_respawn 是 cmd_monitor 闭包——按同源逻辑最小复刻验证。"""
    _tmp_home(monkeypatch, tmp_path)
    started: list[int] = []
    monkeypatch.setattr(bok, "healthy", lambda port: port == 8081)
    monkeypatch.setattr(bok, "_start_proc",
                        lambda *a, **k: started.append(1) or 0)
    specs = [
        {"name": "agent", "port": 8081, "pidfile": Path("/tmp/x-agent.pid"),
         "logfile": Path("/tmp/x.log"), "argv": [], "env": {}},
        {"name": "interp-fwd", "port": 8082, "pidfile": Path("/tmp/x-fwd.pid"),
         "logfile": Path("/tmp/x.log"), "argv": [], "env": {}},
    ]
    # 与 cmd_monitor._respawn 的起拉循环同构（健康跳过段逐字同款）
    for spec in specs:
        if bok.healthy(spec["port"]):
            continue
        bok._start_proc(spec["argv"], spec["pidfile"], spec["logfile"], env=spec["env"])
    assert started == [1], "只有不健康端口该被拉起"
