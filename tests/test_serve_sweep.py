"""孤儿清扫「健康即非孤儿」+ serve 等待环宽松终检单测（2026-09-19 互杀事故收编）。

事故根因：宿主 CPU 风暴下 serve 的 1s 健康探测假死 → 180s 等待超时退出、
留下正在加载模型的健康子代 → 下一轮 serve 的 _sweep_orphan_listeners 把
上一轮健康子代当孤儿杀掉（身份复核通过即杀、不做健康检查）→ 互杀循环。
全部断言不得依赖真实栈在跑——lsof/ps 子进程与健康探测全部打桩。
"""

from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402

_FAKE_PID = 424242  # 不得与测试进程自身 pid 撞（sweep 跳过自身）


class _Completed:
    """subprocess.run 桩返回值（sweep 只读 .stdout；存活探针读 .returncode）。"""

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


class _FakeResp(io.BytesIO):
    """BytesIO 带 status 属性（HTTP 探针走 with urlopen(...)）。"""

    status = 200


def _patch_stack_io(monkeypatch, tmp_path, lsof_out: dict[int, str], ps_cmd: str,
                    healthy: bool, ps_lstart: str = "", ps_alive_rc: int = 0):
    """桩掉 lsof/ps 子进程与 _relaxed_healthy，并把 app_data_dir 指到 tmp
    （来源鉴定读写来源标记、清扫标记文件都落这里，不碰真 app-data）。
    返回 (killed, probed, app_root)。

    lsof_out: port -> stdout（缺席端口视为无人监听）；ps_cmd: ps -p <pid> 的
    command= 输出；ps_lstart: ps -o lstart= 的输出（来源鉴定比对用）；
    ps_alive_rc: ps -p <pid>（无 -o 存活探针）的 returncode；healthy: 健康探测
    桩判定结果（probed_ports 记录探测口）。"""
    killed: list[tuple[str, int]] = []
    probed: list[int] = []
    app_root = tmp_path / "appdata"
    (app_root / "run").mkdir(parents=True, exist_ok=True)

    def fake_run(args, capture_output=True, text=True, timeout=None):
        if args[0] == "lsof":
            port = int(args[2].rsplit(":", 1)[1])
            return _Completed(lsof_out.get(port, ""))
        if args[0] == "ps":
            if "lstart=" in args:
                return _Completed(ps_lstart)
            if len(args) == 3 and args[1] == "-p":  # 无 -o 的存活探针
                return _Completed("", returncode=ps_alive_rc)
            return _Completed(ps_cmd)
        raise AssertionError(f"unexpected subprocess call: {args}")

    def fake_relaxed(port, timeout_s=5.0):
        probed.append(port)
        return healthy

    monkeypatch.setattr(bok.subprocess, "run", fake_run)
    monkeypatch.setattr(bok, "_relaxed_healthy", fake_relaxed)
    monkeypatch.setattr(bok, "app_data_dir", lambda: app_root)
    monkeypatch.setattr(bok.os, "getpgid", lambda pid: 7000 + pid)
    monkeypatch.setattr(bok.os, "killpg",
                        lambda pgid, sig, *_a, **_k: killed.append(("killpg", pgid)))
    monkeypatch.setattr(bok.os, "kill",
                        lambda pid, sig, *_a, **_k: killed.append(("kill", pid)))
    return killed, probed, app_root


_MARKER_LSTART = "Sat Sep 19 16:09:25 2026"
_FOREIGN_ROOT = "/tmp/other-bok-tree"


def _write_stamp(app_root, pid: int, root: str, lstart: str = _MARKER_LSTART) -> None:
    """落一张来源标记（_start_proc 同款格式：ROOT<TAB>lstart）。"""
    (app_root / "run" / f"proc-{pid}.root").write_text(f"{root}\t{lstart}\n")


def test_sweep_skips_healthy_identity_match(monkeypatch, tmp_path, capsys):
    """三态①：身份匹配 + 健康 → 不杀、报 left alone、swept 不含该条目。"""
    killed, probed, _app = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8000: f"{_FAKE_PID}\n"},
        ps_cmd="python -m uvicorn control_plane.main:app --port 8000",
        healthy=True,
    )
    swept = bok._sweep_orphan_listeners(kill=True)
    assert swept == []
    assert killed == []  # 健康即非孤儿：kill=True 也绝不动手
    assert probed == [8000]  # 动手前先做健康探测
    err = capsys.readouterr().err
    assert f"[sweep] port 8000: pid {_FAKE_PID} healthy — left alone" in err


def test_sweep_harvests_unhealthy_identity_match(monkeypatch, tmp_path, capsys):
    """三态②：身份匹配 + 不健康 → 照旧收割（killpg 动手、swept 收录）。"""
    killed, probed, _app = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8787: f"{_FAKE_PID}\n"},
        ps_cmd="python -m qwen3-asr-sidecar.app --port 8787",
        healthy=False,
    )
    swept = bok._sweep_orphan_listeners(kill=True)
    assert swept and swept[0][0] == 8787 and swept[0][2] == _FAKE_PID
    assert killed == [("killpg", 7000 + _FAKE_PID)]
    assert "left alone" not in capsys.readouterr().err


def test_sweep_leaves_identity_mismatch_alone(monkeypatch, tmp_path, capsys):
    """三态③：身份不符（他人物理占用）→ 只报警不动手、不做收割。"""
    killed, probed, _app = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8000: f"{_FAKE_PID}\n"},
        ps_cmd="/usr/sbin/some-other-server --port 8000",
        healthy=False,  # 即使探测不健康，身份不符也绝不动手
    )
    swept = bok._sweep_orphan_listeners(kill=True)
    assert swept == []
    assert killed == []
    assert probed == []  # 身份闸在健康闸之前：复核不过根本不探测
    assert "身份不符" in capsys.readouterr().err


def test_sweep_dry_run_reports_left_alone(monkeypatch, tmp_path, capsys):
    """kill=False 干跑：健康的报 left alone、不动手、不进 swept。"""
    killed, _probed, _app = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={1235: f"{_FAKE_PID}\n"},
        ps_cmd="mlx_lm.server --model /models/Qwen3.5-4B --port 1235",
        healthy=True,
    )
    swept = bok._sweep_orphan_listeners(kill=False)
    assert swept == []
    assert killed == []
    assert "healthy — left alone" in capsys.readouterr().err


def test_sweep_dry_run_lists_unhealthy(monkeypatch, tmp_path, capsys):
    """kill=False 干跑：不健康的仍进 swept（报告「会收割谁」）但绝不动手。"""
    killed, _probed, _app = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8788: f"{_FAKE_PID}\n"},
        ps_cmd="python -m qwen3-tts-sidecar.app --port 8788",
        healthy=False,
    )
    swept = bok._sweep_orphan_listeners(kill=False)
    assert swept and swept[0][0] == 8788 and swept[0][2] == _FAKE_PID
    assert killed == []


def test_relaxed_healthy_http_surface(monkeypatch):
    """有 HTTP 健康面的端口优先 HTTP；任何应答都算活（426 本体作答同款）。"""
    seen: dict = {}

    def fake_urlopen(url, timeout=None):
        seen["url"], seen["timeout"] = url, timeout
        return _FakeResp(b"ok")

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    assert bok._relaxed_healthy(8000)
    assert seen["url"] == "http://127.0.0.1:8000/health"
    assert seen["timeout"] == 5.0  # 放宽窗口：CPU 风暴下 1s 会假死


def test_relaxed_healthy_http_error_still_alive(monkeypatch):
    """HTTPError=服务端有应答（b-line :8790 非 upgrade 恒 426）→ 活。"""

    def fake_urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 426, "Upgrade Required", None, None)

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    assert bok._relaxed_healthy(8790)

    def fake_down(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_down)
    assert not bok._relaxed_healthy(8790)


def test_relaxed_healthy_tcp_fallback(monkeypatch):
    """无 HTTP 面的端口（3000 web UI 等）退 TCP——连接通即算活。"""
    assert 3000 not in bok._SWEEP_HTTP_PATHS
    seen: dict = {}

    def fake_connect(addr, timeout=None):
        seen.update(addr=addr, timeout=timeout)
        return io.BytesIO()

    monkeypatch.setattr(bok.socket, "create_connection", fake_connect)
    assert bok._relaxed_healthy(3000)
    assert seen["addr"] == ("127.0.0.1", 3000)
    assert seen["timeout"] == 5.0

    def refused(addr, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(bok.socket, "create_connection", refused)
    assert not bok._relaxed_healthy(3000)


def test_sweep_http_paths_derive_from_official_tables():
    """协议映射必须复用 PROD_HTTP_CHECKS/WORKER_PORTS 单点表，不另造并行表。"""
    for _name, port, path in bok.PROD_HTTP_CHECKS:
        assert bok._SWEEP_HTTP_PATHS[port] == path
    for _name, port in bok.WORKER_PORTS:
        assert bok._SWEEP_HTTP_PATHS[port] == "/worker"
    # mt/settle 是「起了才查」可选线，同为 mlx_lm server，健康面 /v1/models。
    assert bok._SWEEP_HTTP_PATHS[1236] == "/v1/models"
    assert bok._SWEEP_HTTP_PATHS[1237] == "/v1/models"
    # 表外端口缺席=TCP 语义，不得乱造 HTTP 面。
    assert 3000 not in bok._SWEEP_HTTP_PATHS


def test_ports_down_after_grace():
    """等待环宽松终检（纯函数）：只报仍探不活的口，全绿返回空列表。"""
    health = {8000: True, 8787: False, 8788: True, 1235: False}
    probe = lambda p: health[p]  # noqa: E731
    assert bok._ports_down_after_grace([8000, 8787, 8788, 1235], probe=probe) == [8787, 1235]
    assert bok._ports_down_after_grace([8000, 8788], probe=probe) == []
    assert bok._ports_down_after_grace([], probe=probe) == []


def test_ports_down_after_grace_default_probe():
    """缺省探针=_relaxed_healthy（打桩验证接线，防手滑换回 1s healthy()）。"""
    monkey_hits: list[int] = []

    def fake_relaxed(port, timeout_s=5.0):
        monkey_hits.append(port)
        return port != 8787

    orig = bok._relaxed_healthy
    bok._relaxed_healthy = fake_relaxed
    try:
        assert bok._ports_down_after_grace([8000, 8787]) == [8787]
    finally:
        bok._relaxed_healthy = orig
    assert monkey_hits == [8000, 8787]


def test_sweep_down_semantics_harvests_even_healthy(monkeypatch, tmp_path):
    """down 拆除语义（评审 P1-1,2026-09-19）:healthy_ok=False 时健康监听也照收
    ——pidfile 被覆写成死 pid 时,端口级清扫是 down 唯一的回收路径;健康闸只
    服务 serve 预清扫(保护加载中子代),不得削弱 down 契约。"""
    killed, probed, _app = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8000: f"{_FAKE_PID}\n"},
        ps_cmd="python -m uvicorn control_plane.main:app --port 8000",
        healthy=True,
    )
    swept = bok._sweep_orphan_listeners(kill=True, healthy_ok=False)
    assert swept == [(8000, "python -m uvicorn control_plane.main:app --port 8000"[:60], _FAKE_PID)]
    assert killed == [("killpg", 7000 + _FAKE_PID)]
    assert probed == []  # down 档根本不做健康探测


def test_sweep_spares_foreign_tree_stamped_process(monkeypatch, tmp_path, capsys):
    """来源鉴定三态④：marker 指向别的代码树 → 永不收割（连健康探测都不做——
    他栈可能正在加载模型，跨树互杀同款根因；多会话纪律禁碰他树）。"""
    killed, probed, app_root = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8000: f"{_FAKE_PID}\n"},
        ps_cmd="python -m uvicorn control_plane.main:app --port 8000",
        healthy=True, ps_lstart=_MARKER_LSTART,
    )
    _write_stamp(app_root, _FAKE_PID, _FOREIGN_ROOT)
    swept = bok._sweep_orphan_listeners(kill=True)
    assert swept == []
    assert killed == []
    assert probed == []  # 来源闸在健康闸之前：外来根本不探测
    assert "另一代码树" in capsys.readouterr().err


def test_down_spares_foreign_tree_stamped_process(monkeypatch, tmp_path, capsys):
    """down 档同样不收他树 stamp 进程（down=停本树+无戳遗留；他树要切换
    由对方 down）。"""
    killed, probed, app_root = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8000: f"{_FAKE_PID}\n"},
        ps_cmd="python -m uvicorn control_plane.main:app --port 8000",
        healthy=True, ps_lstart=_MARKER_LSTART,
    )
    _write_stamp(app_root, _FAKE_PID, _FOREIGN_ROOT)
    swept = bok._sweep_orphan_listeners(kill=True, healthy_ok=False)
    assert swept == []
    assert killed == []
    assert probed == []
    assert "另一代码树" in capsys.readouterr().err


def test_sweep_harvests_own_tree_stamped_unhealthy(monkeypatch, tmp_path):
    """本树 stamp + 不健康 → 照常收割（来源闸只豁免他树，不豁免自家死子代）。"""
    killed, probed, app_root = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8787: f"{_FAKE_PID}\n"},
        ps_cmd="python -m qwen3-asr-sidecar.app --port 8787",
        healthy=False, ps_lstart=_MARKER_LSTART,
    )
    _write_stamp(app_root, _FAKE_PID, str(bok.ROOT))
    swept = bok._sweep_orphan_listeners(kill=True)
    assert swept and swept[0][0] == 8787 and swept[0][2] == _FAKE_PID
    assert killed == [("killpg", 7000 + _FAKE_PID)]
    assert probed == [8787]


def test_down_harvests_own_tree_stamped_even_healthy(monkeypatch, tmp_path):
    """P1-1 契约与来源鉴定共存：本树 stamp + down 档（healthy_ok=False）→
    健康监听照收（pidfile 覆写死 pid 时这是唯一回收路径）。"""
    killed, probed, app_root = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8000: f"{_FAKE_PID}\n"},
        ps_cmd="python -m uvicorn control_plane.main:app --port 8000",
        healthy=True, ps_lstart=_MARKER_LSTART,
    )
    _write_stamp(app_root, _FAKE_PID, str(bok.ROOT))
    swept = bok._sweep_orphan_listeners(kill=True, healthy_ok=False)
    assert swept and swept[0][0] == 8000
    assert killed == [("killpg", 7000 + _FAKE_PID)]
    assert probed == []


def test_recycled_pid_stamp_rejected_falls_to_health_gate(monkeypatch, tmp_path, capsys):
    """pid 复用防串号：marker 的 lstart 与 ps 现值对不上 → 标记作废=来源未知
    → 走原健康闸（不误判成他树、也不盲杀）。"""
    killed, probed, app_root = _patch_stack_io(
        monkeypatch, tmp_path,
        lsof_out={8000: f"{_FAKE_PID}\n"},
        ps_cmd="python -m uvicorn control_plane.main:app --port 8000",
        healthy=True, ps_lstart="Mon Sep  1 00:00:00 2025",  # ≠ marker 记录
    )
    _write_stamp(app_root, _FAKE_PID, _FOREIGN_ROOT)  # 若串号误信会误判他树
    swept = bok._sweep_orphan_listeners(kill=True)
    assert swept == []
    assert killed == []
    assert probed == [8000]  # 来源未知 → 健康闸接手
    err = capsys.readouterr().err  # readouterr 只能读一次,先存再双断言
    assert "另一代码树" not in err
    assert "healthy — left alone" in err


def test_process_serve_root_marker_branches(monkeypatch, tmp_path):
    """来源鉴定纯函数分支：本树命中 / lstart 不符作废 / 无标记未知 / 格式坏未知。"""
    app_root = tmp_path / "appdata"
    (app_root / "run").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bok, "app_data_dir", lambda: app_root)
    monkeypatch.setattr(bok, "_ps_field", lambda pid, field: _MARKER_LSTART)

    _write_stamp(app_root, 101, str(bok.ROOT))
    assert bok._process_serve_root(101) == str(bok.ROOT)

    _write_stamp(app_root, 102, _FOREIGN_ROOT, "Mon Sep  1 00:00:00 2025")
    assert bok._process_serve_root(102) == ""  # lstart 对不上=pid 已复用

    _write_stamp(app_root, 103, _FOREIGN_ROOT, "")  # 落笔时 ps 失败的残缺标记
    assert bok._process_serve_root(103) == ""

    (app_root / "run" / "proc-104.root").write_text("no-tab-garbage\n")
    assert bok._process_serve_root(104) == ""
    assert bok._process_serve_root(105) == ""  # 无标记=未知


def test_start_proc_writes_env_stamp_and_marker(monkeypatch, tmp_path):
    """_start_proc 落来源 stamp：子代 env 带 BOK_SERVE_ROOT=本树 ROOT，
    同名标记文件落 pidfile 旁（ROOT<TAB>lstart，ps 拿不到时第二字段留空）。"""

    class _FakeProc:
        pid = _FAKE_PID

    calls: list = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeProc()

    monkeypatch.setattr(bok.subprocess, "Popen", fake_popen)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    logfile = tmp_path / "logs" / "x.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)

    pid = bok._start_proc(["true"], run_dir / "x.pid", logfile, env={"A": "b"})
    assert pid == _FAKE_PID
    assert (run_dir / "x.pid").read_text() == str(_FAKE_PID)
    # Popen 会被调两次：真 spawn 一次 + _ps_field 的 subprocess.run 内部一次
    # （桩也拦 run 的 Popen，且 run 不带 env——断言只认第一次真 spawn）。
    spawn_args, spawn_kwargs = calls[0]
    assert spawn_args[0] == ["true"]
    assert spawn_kwargs["env"]["BOK_SERVE_ROOT"] == str(bok.ROOT)
    assert spawn_kwargs["env"]["A"] == "b"
    assert spawn_kwargs["start_new_session"] is True  # posix 会话组长（killpg 语义）
    raw = (run_dir / f"proc-{_FAKE_PID}.root").read_text()
    assert raw.startswith(str(bok.ROOT) + "\t")  # 格式=ROOT<TAB>lstart
    assert raw.endswith("\n")
    # 桩环境 lstart 拿不到（空第二字段）→ 来源判定必须落「未知」而非误信。
    assert bok._process_serve_root(_FAKE_PID) == ""


def test_stale_root_markers_cleaned(monkeypatch, tmp_path):
    """标记清理：ps 探不到的 pid（returncode≠0）标记删除，防无限积累。"""
    killed, _probed, app_root = _patch_stack_io(
        monkeypatch, tmp_path, lsof_out={}, ps_cmd="", healthy=False, ps_alive_rc=1,
    )
    _write_stamp(app_root, 999001, str(bok.ROOT))
    assert bok._sweep_orphan_listeners(kill=True) == []
    assert not (app_root / "run" / "proc-999001.root").exists()
    assert killed == []


def test_live_root_markers_kept(monkeypatch, tmp_path):
    """标记清理：ps 探测失败/存活（returncode=0）标记保留，不误删在册进程。"""
    _killed, _probed, app_root = _patch_stack_io(
        monkeypatch, tmp_path, lsof_out={}, ps_cmd="", healthy=False, ps_alive_rc=0,
    )
    _write_stamp(app_root, 999002, str(bok.ROOT))
    assert bok._sweep_orphan_listeners(kill=True) == []
    assert (app_root / "run" / "proc-999002.root").exists()


def test_only_optional_ports_gate():
    """宽松终检可选线豁免（纯函数）：缺口全落 1236/1237 → 放行；掺任何核心
    端口/空列表 → 唔放行（空=全绿走 ready 分支,轮不到本闸）。"""
    assert bok._only_optional_ports([1236])
    assert bok._only_optional_ports([1236, 1237])
    assert not bok._only_optional_ports([8787])
    assert not bok._only_optional_ports([8787, 1236])
    assert not bok._only_optional_ports([])
