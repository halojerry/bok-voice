"""Windows 无头生命周期（site-delivery Task 8 / M2）单测——全平台可跑、零 Windows 执行。

钉住的契约：
  ① _spawn_kwargs：nt 走 CREATE_NEW_PROCESS_GROUP（CPython 对 start_new_session
    是静默忽略，probe_windows_lifecycle.py docstring 记录的事实）；
  ② _kill_proc_tree：nt = taskkill /PID <pid> /T /F（已死 rc=128 静默、真失败
    浮出 _KillTreeError）；POSIX = killpg→单杀回退（与旧代码逐字节同款）；
  ③ cmd_down：Windows 真失败打 stderr+rc1，死 pid 静默（POSIX stdout 契约不变）；
  ④ schtasks_units XML：BootTrigger / RestartOnFailure(PT1M×3) / SYSTEM
    principal / env 前缀链 action / utf-16 落盘 / schema 元素顺序；
  ⑤ doctor _doctor_gpu_gate：NVIDIA 门禁独立于虚拟声卡（回归 va_ok=True 时
    门禁仍被评估；POSIX 不跑）；
  ⑥ prod install --node-agent / uninstall / --open-firewall / 参数透传保序。
"""
from __future__ import annotations

import inspect
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# 平台模拟约定（2026-09-20）：本文件用例模拟 Windows，需同时打 is_mac=False 与
# is_linux=False 两桩——只关 is_mac 时，真 Linux 主机（CI=ubuntu-latest）会让
# cmd_prod_install 走进 systemd 分支把用例截胡（容器实测 6 红）。

import bok  # noqa: E402
import schtasks_units  # noqa: E402

_TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def _parse_xml(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _find_all(root: ET.Element, local: str) -> list[ET.Element]:
    return [e for e in root.iter() if e.tag == f"{_TASK_NS}{local}"]


def _find(root: ET.Element, local: str) -> ET.Element:
    els = _find_all(root, local)
    assert len(els) == 1, f"expect exactly one <{local}>, got {len(els)}"
    return els[0]


def _local_children(el: ET.Element) -> list[str]:
    return [c.tag[len(_TASK_NS):] for c in el]


# ---------------- ① spawn 旗标 ----------------


def test_spawn_kwargs_windows_uses_process_group(monkeypatch) -> None:
    monkeypatch.setattr(bok.os, "name", "nt")
    # subprocess.CREATE_NEW_PROCESS_GROUP 只在 Windows 存在；POSIX 上用字面量 0x200 断言。
    assert bok._spawn_kwargs() == {"creationflags": 512}


def test_spawn_kwargs_posix_start_new_session() -> None:
    if bok.os.name == "nt":
        pytest.skip("POSIX-only contract")
    assert bok._spawn_kwargs() == {"start_new_session": True}


def test_start_proc_writes_pidfile_posix(tmp_path: Path) -> None:
    """POSIX 真起一炮：pidfile 落盘、返回 pid（probe_windows_lifecycle A 段的同款路径）。"""
    (tmp_path / "logs").mkdir()
    pidfile = tmp_path / "run" / "x.pid"
    pid = bok._start_proc([sys.executable, "-c", "pass"], pidfile, tmp_path / "logs" / "x.log")
    assert pid > 0
    assert pidfile.read_text().strip() == str(pid)


# ---------------- ② kill 语义 ----------------


def test_kill_tree_windows_builds_taskkill_command(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(bok.os, "name", "nt")
    monkeypatch.setattr(bok.subprocess, "run", fake_run)
    bok._kill_proc_tree(12345)  # 不应抛
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert isinstance(kwargs.get("timeout"), (int, float))


def test_kill_tree_windows_not_found_is_silent(monkeypatch) -> None:
    """rc=128（进程已不在）≈ POSIX ProcessLookupError：静默放行。"""
    monkeypatch.setattr(bok.os, "name", "nt")
    monkeypatch.setattr(
        bok.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 128, stdout="", stderr="ERROR: process not found"))
    bok._kill_proc_tree(99)  # 不应抛


def test_kill_tree_windows_failure_surfaced(monkeypatch) -> None:
    monkeypatch.setattr(bok.os, "name", "nt")
    monkeypatch.setattr(
        bok.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="Access is denied."))
    with pytest.raises(bok._KillTreeError) as ei:
        bok._kill_proc_tree(99)
    assert "taskkill" in str(ei.value) and "rc=1" in str(ei.value)


def test_kill_tree_windows_timeout_surfaced(monkeypatch) -> None:
    monkeypatch.setattr(bok.os, "name", "nt")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(bok.subprocess, "run", fake_run)
    with pytest.raises(bok._KillTreeError):
        bok._kill_proc_tree(99)


def test_kill_tree_posix_killpg_then_single_kill_fallback(monkeypatch) -> None:
    """POSIX 契约与旧代码逐字节同款：killpg(getpgid(pid), SIGTERM)，
    (ProcessLookupError, PermissionError, OSError) 时回退 os.kill(pid, SIGTERM)。"""
    if bok.os.name == "nt":
        pytest.skip("POSIX-only contract")
    import signal
    calls: list[tuple] = []
    monkeypatch.setattr(bok.os, "getpgid", lambda pid: 4242)
    monkeypatch.setattr(bok.os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
    bok._kill_proc_tree(123)
    assert calls == [("killpg", 4242, signal.SIGTERM)]

    def _raise(pgid, sig):
        raise ProcessLookupError

    calls.clear()
    monkeypatch.setattr(bok.os, "killpg", _raise)
    monkeypatch.setattr(bok.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    bok._kill_proc_tree(123)
    assert calls == [("kill", 123, signal.SIGTERM)]


# ---------------- ③ cmd_down ----------------


def _make_run_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (run_dir / name).write_text(content)
    return tmp_path


def test_cmd_down_posix_stdout_contract(monkeypatch, tmp_path: Path, capsys) -> None:
    """POSIX stdout 契约：杀得掉的打 stopped；死 pid/坏 pidfile 静默；rc=0。"""
    if bok.os.name == "nt":
        pytest.skip("POSIX-only contract")
    tmp = _make_run_dir(tmp_path, {"good.pid": "111\n", "dead.pid": "222\n", "bad.pid": "not-a-pid"})
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp)
    monkeypatch.setattr(bok, "_sweep_orphan_workers", lambda: [])
    # 端口级清扫必须一并打桩(2026-09-19 二次灭栈实案):healthy_ok=False 的
    # down 档会真杀本机活栈——本测试只测 pidfile stdout 契约,不吃真 lsof。
    monkeypatch.setattr(bok, "_sweep_orphan_listeners", lambda **_kw: [])

    killed: list[int] = []

    def fake_kill(pid: int) -> None:
        if pid == 222:
            raise ProcessLookupError
        killed.append(pid)

    monkeypatch.setattr(bok, "_kill_proc_tree", fake_kill)
    rc = bok.cmd_down()
    out = capsys.readouterr().out
    assert rc == 0
    assert killed == [111]
    assert "[down] stopped good (pid 111)" in out
    assert "dead" not in out and "bad" not in out


def test_cmd_down_windows_failure_surfaced_and_continues(monkeypatch, tmp_path: Path, capsys) -> None:
    """Windows：taskkill 真失败 → stderr 留痕 + rc=1，且继续清其余 pidfile（不重演静默吞）。"""
    tmp = _make_run_dir(tmp_path, {"a.pid": "10\n", "b.pid": "20\n"})
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp)
    monkeypatch.setattr(bok.os, "name", "nt")
    monkeypatch.setattr(
        bok.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="Access is denied."))
    rc = bok.cmd_down()
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAILED to stop a" in captured.err and "FAILED to stop b" in captured.err


def test_cmd_down_windows_success_and_notfound(monkeypatch, tmp_path: Path, capsys) -> None:
    tmp = _make_run_dir(tmp_path, {"live.pid": "10\n", "gone.pid": "20\n"})

    def fake_run(argv, **kwargs):
        rc = 0 if argv[-1] == "10" else 128
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp)
    monkeypatch.setattr(bok.os, "name", "nt")
    monkeypatch.setattr(bok.subprocess, "run", fake_run)
    rc = bok.cmd_down()
    captured = capsys.readouterr()
    assert rc == 0
    assert "[down] stopped live (pid 10)" in captured.out
    assert "FAILED" not in captured.err


def test_sweep_orphan_workers_windows_is_documented_skip(monkeypatch) -> None:
    """Windows 明跳：不做任何 ps/tasklist 探测（误杀风险），返回空表。"""
    def explode(*a, **kw):
        raise AssertionError("windows sweep must not probe processes")

    monkeypatch.setattr(bok.os, "name", "nt")
    monkeypatch.setattr(bok.subprocess, "run", explode)
    assert bok._sweep_orphan_workers() == []


# ---------------- ④ schtasks_units：XML 契约 ----------------

_UNIT_ARGS = ["C:\\tools\\python.exe", "-m", "agent_runtime.main"]


def test_task_xml_is_wellformed_and_roundtrips() -> None:
    arguments = schtasks_units.build_cmd_arguments(
        {"PYTHONUNBUFFERED": "1"}, _UNIT_ARGS, "C:\\repo",
        "C:\\logs\\bok-agent.log", "C:\\logs\\bok-agent.err.log")
    xml = schtasks_units.build_unit_task_xml("bok-agent", "cmd.exe", arguments, "C:\\repo")
    root = _parse_xml(xml)  # well-formed（&& 已正确转义）
    args_text = _find(root, "Arguments").text
    assert args_text == arguments  # 元素文本经解析后还原原串（& 不丢）
    assert "&&" in args_text


def test_task_xml_boot_trigger_restart_and_principal() -> None:
    xml = schtasks_units.build_unit_task_xml("bok-agent", "cmd.exe", "/c x", "C:\\repo")
    root = _parse_xml(xml)
    boot = _find(root, "BootTrigger")
    assert _find(boot, "Enabled").text == "true"
    # RestartOnFailure ←→ launchd KeepAlive（崩溃拉回）：1 分钟间隔 × 3 次。
    rof = _find(root, "RestartOnFailure")
    assert _find(rof, "Interval").text == "PT1M"
    assert _find(rof, "Count").text == "3"
    # SYSTEM 无头 principal。
    principal = _find(root, "Principal")
    assert _find(principal, "UserId").text == "S-1-5-18"
    assert _find(principal, "RunLevel").text == "HighestAvailable"


def test_task_xml_schema_element_order() -> None:
    """schtasks /create 按 schema 校验元素顺序（乱序直接拒）。"""
    xml = schtasks_units.build_unit_task_xml("bok-agent", "cmd.exe", "/c x", "C:\\repo")
    root = _parse_xml(xml)
    assert _local_children(root) == [
        "RegistrationInfo", "Triggers", "Principals", "Settings", "Actions"]
    assert _local_children(_find(root, "Settings")) == [
        "MultipleInstancesPolicy", "DisallowStartIfOnBatteries", "StopIfGoingOnBatteries",
        "AllowHardTerminate", "StartWhenAvailable", "RunOnlyIfNetworkAvailable",
        "AllowStartOnDemand", "Enabled", "Hidden", "RunOnlyIfIdle",
        "ExecutionTimeLimit", "Priority", "RestartOnFailure"]


def test_task_xml_uri_and_description_default() -> None:
    xml = schtasks_units.build_unit_task_xml("bok-agent", "cmd.exe", "/c x", "C:\\repo")
    root = _parse_xml(xml)
    assert _find(root, "URI").text == "\\BokVoice\\bok-agent"
    assert "bok-agent" in _find(root, "Description").text


def test_task_name_convention() -> None:
    # mac plist 同名单元原样收口（防 bok-bok-agent 双前缀）；裸 unit 补前缀。
    assert schtasks_units.task_name("bok-agent") == "bok-agent"
    assert schtasks_units.task_name("bok-control-plane") == "bok-control-plane"
    assert schtasks_units.task_name("agent") == "bok-agent"
    assert schtasks_units.task_name("node-agent") == "bok-node-agent"


def test_write_task_xml_is_utf16_with_bom(tmp_path: Path) -> None:
    path = schtasks_units.write_task_xml(
        tmp_path / "units" / "bok-agent.xml",
        schtasks_units.build_unit_task_xml("bok-agent", "cmd.exe", "/c x", "C:\\repo"))
    raw = path.read_bytes()
    assert raw[:2] == b"\xff\xfe"  # UTF-16 LE BOM（schtasks /xml 只认带 BOM 的 UTF-16）
    text = path.read_text(encoding="utf-16")
    assert text.lstrip().startswith("<?xml")
    _parse_xml(text)


def test_build_cmd_arguments_env_chain_and_redirect() -> None:
    got = schtasks_units.build_cmd_arguments(
        {"B_VAR": "x y", "A_VAR": "1"}, _UNIT_ARGS, "C:\\repo",
        "C:\\logs\\out.log", "C:\\logs\\err.log")
    assert got == (
        '/c cd /d "C:\\repo"'
        ' && set "A_VAR=1"'
        ' && set "B_VAR=x y"'
        ' && "C:\\tools\\python.exe" -m agent_runtime.main'
        ' >> "C:\\logs\\out.log" 2>> "C:\\logs\\err.log"')
    # /c 后首字符非引号（躲 cmd /c 首引号剥离规则）。
    assert got.startswith("/c cd /d ")


def test_build_cmd_arguments_empty_env_still_leads_with_cd() -> None:
    got = schtasks_units.build_cmd_arguments(
        {}, ["C:\\Program Files\\livekit\\livekit-server.exe", "--config", "c.yaml"],
        "C:\\repo", "o.log", "e.log")
    assert got.startswith('/c cd /d "C:\\repo" && ')
    assert '"C:\\Program Files\\livekit\\livekit-server.exe" --config c.yaml' in got


def test_build_cmd_arguments_quotes_args_with_spaces() -> None:
    got = schtasks_units.build_cmd_arguments(
        {}, ["C:\\p\\python.exe", "C:\\tools\\node_agent.py", "--ui-dir", "C:\\a b\\out"],
        "C:\\repo", "o.log", "e.log")
    assert "C:\\tools\\node_agent.py" in got  # 无空格 token 不加引号
    assert '"C:\\a b\\out"' in got  # 含空格 token 加引号
    assert "--ui-dir" in got


def test_schtasks_argv_helpers() -> None:
    assert schtasks_units.schtasks_create_argv("bok-agent", "u/x.xml") == [
        "schtasks", "/create", "/tn", "bok-agent", "/xml", "u/x.xml", "/f"]
    assert schtasks_units.schtasks_query_argv("bok-agent") == [
        "schtasks", "/query", "/tn", "bok-agent"]
    assert schtasks_units.schtasks_end_argv("bok-agent") == [
        "schtasks", "/end", "/tn", "bok-agent"]
    assert schtasks_units.schtasks_delete_argv("bok-agent") == [
        "schtasks", "/delete", "/tn", "bok-agent", "/f"]


# ---------------- ④b 防火墙助手 ----------------


def test_firewall_rules_cover_8000_and_7880_tcp_udp() -> None:
    rules = schtasks_units.firewall_rules_argv()
    assert len(rules) == 4
    for argv in rules:
        assert argv[:5] == ["netsh", "advfirewall", "firewall", "add", "rule"]
        assert argv[5].startswith("name=Bok Voice ")
    by_key = {(argv[-1], argv[-2]): argv for argv in rules}
    assert set(by_key) == {
        ("localport=8000", "protocol=TCP"), ("localport=8000", "protocol=UDP"),
        ("localport=7880", "protocol=TCP"), ("localport=7880", "protocol=UDP")}
    assert by_key[("localport=8000", "protocol=TCP")][5] == "name=Bok Voice 8000 TCP"
    assert by_key[("localport=7880", "protocol=UDP")][5] == "name=Bok Voice 7880 UDP"


def test_firewall_default_print_only(capsys) -> None:
    def explode(*a, **kw):
        raise AssertionError("plan-only must not execute netsh")

    rc = schtasks_units.apply_firewall_rules(execute=False, run=explode)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("netsh advfirewall") == 4


def test_firewall_execute_surfaces_failure_rc(monkeypatch, capsys) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="requires elevation")

    rc = schtasks_units.apply_firewall_rules(execute=True, run=fake_run)
    assert rc == 1
    assert "requires elevation" in capsys.readouterr().err


def test_firewall_execute_all_ok(monkeypatch, capsys) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="Ok.", stderr="")

    rc = schtasks_units.apply_firewall_rules(execute=True, run=fake_run)
    assert rc == 0
    assert capsys.readouterr().out.count("$") == 4


# ---------------- ⑤ doctor NVIDIA 门禁回归 ----------------


def test_doctor_gpu_gate_skipped_on_mac(monkeypatch, capsys) -> None:
    calls: list[int] = []
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "_nvidia_gate", lambda: calls.append(1) or (True, "x"))
    fails: list[str] = []
    bok._doctor_gpu_gate(packaged=True, fails=fails)
    assert calls == []  # mac 无 nvidia-smi：门禁不适用
    assert fails == []
    assert "nvidia gate" not in capsys.readouterr().out


def test_doctor_gpu_gate_runs_on_linux(monkeypatch, capsys) -> None:
    """Linux CUDA 节点同门同判（runbook §5⑥，2026-09-22）：门禁必须被评估。"""
    calls: list[int] = []
    monkeypatch.setattr(bok, "is_linux", lambda: True)
    monkeypatch.setattr(bok.os, "name", "posix")
    monkeypatch.setattr(bok, "_nvidia_gate", lambda: calls.append(1) or (True, "NVIDIA OK"))
    fails: list[str] = []
    bok._doctor_gpu_gate(packaged=False, fails=fails)
    assert calls == [1]
    assert fails == []  # dev 只提示
    assert "nvidia gate: NVIDIA OK" in capsys.readouterr().out
    bok._doctor_gpu_gate(packaged=True, fails=fails)
    assert calls == [1, 1] and fails == []  # gate 过线=packaged 也不 fail


def test_doctor_gpu_gate_runs_regardless_of_virtual_audio(monkeypatch, capsys) -> None:
    """回归核心：va_ok=True（装了虚拟声卡）也必须评估门禁（曾误缩进在 if not va_ok 下）。"""
    calls: list[int] = []
    monkeypatch.setattr(bok.os, "name", "nt")
    monkeypatch.setattr(bok, "_nvidia_gate", lambda: calls.append(1) or (True, "NVIDIA OK"))
    fails: list[str] = []
    bok._doctor_gpu_gate(packaged=False, fails=fails)
    assert calls == [1]  # 门禁被评估（虚拟声卡状态无关——本函数根本不读它）
    assert fails == []
    assert "nvidia gate: NVIDIA OK" in capsys.readouterr().out


def test_doctor_gpu_gate_packaged_failure_fails_doctor(monkeypatch) -> None:
    monkeypatch.setattr(bok.os, "name", "nt")
    monkeypatch.setattr(bok, "_nvidia_gate", lambda: (False, "NVIDIA GPU 未检测到"))
    fails: list[str] = []
    bok._doctor_gpu_gate(packaged=False, fails=fails)
    assert fails == []  # dev 模式只提示
    bok._doctor_gpu_gate(packaged=True, fails=fails)
    assert fails == ["NVIDIA GPU 未检测到"]  # packaged 硬失败


# ---------------- ⑥ prod install / uninstall / 参数透传 ----------------


def _fake_schtasks_factory(rc: int = 0, record: list | None = None):
    def fake_run(argv, timeout=60.0):
        if record is not None:
            record.append(list(argv))
        return subprocess.CompletedProcess(argv, rc, stdout="SUCCESS", stderr="")
    return fake_run


def test_prod_install_windows_registers_five_units(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(schtasks_units, "run_schtasks", _fake_schtasks_factory(0, calls))
    rc = bok.cmd_prod_install()
    assert rc == 0
    creates = [c for c in calls if c[1] == "/create"]
    assert [c[3] for c in creates] == [
        "bok-control-plane", "bok-livekit", "bok-agent", "bok-interp-fwd", "bok-interp-rev"]
    for c in creates:
        assert c[0:2] == ["schtasks", "/create"] and c[-1] == "/f"
        xml_file = Path(c[5])
        assert xml_file.exists()
        root = _parse_xml(xml_file.read_text(encoding="utf-16"))
        assert _find(root, "UserId").text == "S-1-5-18"
        args = _find(root, "Arguments").text
        assert args.startswith("/c cd /d ")
        assert ">> " in args and "2>> " in args


def test_prod_install_windows_node_agent_single_task_passthrough(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(schtasks_units, "run_schtasks", _fake_schtasks_factory(0, calls))
    node_args = ["--cp-url", "http://127.0.0.1:8000", "--node-token", "tok",
                 "--ui-dir", "C:\\a b\\out"]
    rc = bok.cmd_prod_install(node_agent=True, node_args=node_args)
    assert rc == 0
    creates = [c for c in calls if c[1] == "/create"]
    assert len(creates) == 1 and creates[0][3] == "bok-node-agent"
    xml_file = Path(creates[0][5])
    root = _parse_xml(xml_file.read_text(encoding="utf-16"))
    args = _find(root, "Arguments").text
    assert "node_agent.py" in args
    for frag in node_args:
        assert frag in args
    assert 'set "PYTHONUNBUFFERED=1"' in args


def test_prod_install_windows_node_agent_requires_cp_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    rc = bok.cmd_prod_install(node_agent=True, node_args=["--license-key", "bokn_x"])
    assert rc == 2


def test_prod_install_windows_schtasks_failure_rc1(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(schtasks_units, "run_schtasks", _fake_schtasks_factory(1))
    assert bok.cmd_prod_install() == 1


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="mac 契约测试:--node-agent 的 mac 拒绝分支只在 mac 生效;"
                           "Linux 上 prod install 走不到该分支(跟进项:非 mac/nt 平台应有显式 unsupported 挡板)")
def test_prod_install_mac_node_agent_rejected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    assert bok.cmd_prod_install(node_agent=True, node_args=["--cp-url", "x"]) == 2


def test_prod_uninstall_windows_removes_tasks_and_xml(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(schtasks_units, "run_schtasks", _fake_schtasks_factory(0, calls))
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "bok-agent.xml").write_text("x", encoding="utf-16")
    rc = bok.cmd_prod_uninstall()
    assert rc == 0
    deletes = [c for c in calls if c[1] == "/delete"]
    assert [c[3] for c in deletes] == [
        "bok-control-plane", "bok-livekit", "bok-agent", "bok-interp-fwd",
        "bok-interp-rev", "bok-node-agent"]
    assert [c for c in calls if c[1] == "/end"], "/end 停运行实例先于 /delete"
    assert not (tmp_path / "units" / "bok-agent.xml").exists()


def test_prod_uninstall_windows_not_installed_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)

    def fake_run(argv, timeout=60.0):
        return subprocess.CompletedProcess(argv, 1, stdout="",
                                           stderr="ERROR: The specified task name does not exist in the system.")

    monkeypatch.setattr(schtasks_units, "run_schtasks", fake_run)
    assert bok.cmd_prod_uninstall() == 0  # 未安装 ≠ 失败


def test_prod_uninstall_windows_hard_failure_rc1(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(schtasks_units, "run_schtasks", _fake_schtasks_factory(1))
    assert bok.cmd_prod_uninstall() == 1


def test_parse_args_prod_passthrough_preserves_order() -> None:
    args = bok.parse_args([
        "prod", "install", "--node-agent", "--open-firewall",
        "--cp-url", "http://127.0.0.1:8000", "--license-key", "bokn_abc",
        "--interval", "30"])
    assert args.action == "install"
    assert args.node_agent is True
    assert args.open_firewall is True
    # 透传参数保序（node_agent argv 依赖顺序）。
    assert args.extra == ["--cp-url", "http://127.0.0.1:8000",
                          "--license-key", "bokn_abc", "--interval", "30"]


def test_parse_args_prod_uninstall_choice() -> None:
    args = bok.parse_args(["prod", "uninstall"])
    assert args.action == "uninstall"
    assert args.node_agent is False


# ---------------- M2-fix ①: _pid_alive 探活不得击杀 ----------------


def test_pid_alive_windows_uses_tasklist_never_os_kill(monkeypatch, tmp_path: Path) -> None:
    """nt：os.kill(pid, 0) 会 TerminateProcess（探活即击杀）——nt 分支必须只走
    tasklist，绝不碰 os.kill。"""
    def explode(pid, sig):
        raise AssertionError(f"os.kill called on nt (pid={pid}, sig={sig})")

    monkeypatch.setattr(bok.os, "name", "nt")
    monkeypatch.setattr(bok.os, "kill", explode)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        out = '"python.exe","4242","Console","1","1,000 K"\n' if any("4242" in a for a in argv) else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(bok.subprocess, "run", fake_run)
    pf = tmp_path / "monitor.pid"
    pf.write_text("4242\n")
    assert bok._pid_alive(pf) is True
    pf.write_text("9999\n")
    assert bok._pid_alive(pf) is False  # tasklist 查无此 PID → 死
    assert calls and all(c[:2] == ["tasklist", "/FI"] for c in calls)
    assert calls[0] == ["tasklist", "/FI", "PID eq 4242", "/FO", "CSV", "/NH"]


def test_pid_alive_windows_query_failure_conservative_alive(monkeypatch, tmp_path: Path) -> None:
    """tasklist 查询失败保守当存活（勿误判单例已死而重复拉起 monitor）。"""
    monkeypatch.setattr(bok.os, "name", "nt")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(bok.subprocess, "run", fake_run)
    pf = tmp_path / "monitor.pid"
    pf.write_text("4242\n")
    assert bok._pid_alive(pf) is True


def test_pid_alive_posix_still_uses_os_kill(monkeypatch, tmp_path: Path) -> None:
    """POSIX：与旧代码同款，os.kill(pid, 0) 纯探活。"""
    if bok.os.name == "nt":
        pytest.skip("POSIX-only contract")
    calls: list[tuple] = []
    monkeypatch.setattr(bok.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    pf = tmp_path / "monitor.pid"
    pf.write_text("4242\n")
    assert bok._pid_alive(pf) is True
    assert calls == [(4242, 0)]

    def _raise(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(bok.os, "kill", _raise)
    assert bok._pid_alive(pf) is False


# ---------------- M2-fix ②: schtasks /end 杀不到链式子进程 ----------------


def test_prod_uninstall_windows_survivor_cleanup(monkeypatch, tmp_path: Path, capsys) -> None:
    """/end 只杀 Exec 动作进程：pidfile 存活的链式子进程必须被点名 WARNING 并
    best-effort cmd_down()（taskkill /T /F 按 pidfile）清掉，且全部 /end 先于
    全部 /delete（先停动作进程→清子进程→再删注册）。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(schtasks_units, "run_schtasks", _fake_schtasks_factory(0, calls))
    monkeypatch.setattr(bok, "_pid_alive", lambda pf: True)
    down_calls: list[int] = []
    monkeypatch.setattr(bok, "cmd_down", lambda: down_calls.append(1))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "bok-agent.pid").write_text("111\n")
    (run_dir / "llm.pid").write_text("222\n")
    rc = bok.cmd_prod_uninstall()
    captured = capsys.readouterr()
    assert rc == 0
    assert down_calls == [1]
    assert "bok-agent" in captured.err and "llm" in captured.err
    assert "WARNING" in captured.err
    ends = [i for i, c in enumerate(calls) if c[1] == "/end"]
    deletes = [i for i, c in enumerate(calls) if c[1] == "/delete"]
    assert ends and deletes and max(ends) < min(deletes)


def test_prod_uninstall_windows_no_survivors_skips_down(monkeypatch, tmp_path: Path, capsys) -> None:
    """无 pidfile 存活：不打 WARNING、不跑 cmd_down（silence = 没有要 surface 的东西）。"""
    monkeypatch.setattr(bok, "is_mac", lambda: False)
    monkeypatch.setattr(bok, "is_linux", lambda: False)
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(schtasks_units, "run_schtasks", _fake_schtasks_factory(0, []))
    monkeypatch.setattr(bok, "_pid_alive", lambda pf: False)
    down_calls: list[int] = []
    monkeypatch.setattr(bok, "cmd_down", lambda: down_calls.append(1))
    assert bok.cmd_prod_uninstall() == 0
    captured = capsys.readouterr()
    assert down_calls == []
    assert "WARNING" not in captured.err


# ---------------- ⑧ doctor 门禁调用位置（结构断言） ----------------


def test_doctor_gpu_gate_called_at_function_top_level() -> None:
    """结构断言（镜像 cargo 探针的源码扫描风格）：`_doctor_gpu_gate(` 在
    cmd_doctor 里的调用必须位于函数体顶层（缩进 4，不在任何 `if not va_ok:`
    块内）——曾误缩进在块内（缩进 8），装了虚拟声卡的 Windows 机器结构性跳过
    GPU 门禁。行为回归见 ⑤ 的三只 gate 单测，这里钉「调用位置」本身。"""
    src = inspect.getsource(bok.cmd_doctor)
    calls = [
        (idx, ln)
        for idx, ln in enumerate(src.splitlines())
        if "_doctor_gpu_gate(" in ln and not ln.lstrip().startswith("#")
    ]
    assert calls, "cmd_doctor must call _doctor_gpu_gate("
    for idx, ln in calls:
        indent = len(ln) - len(ln.lstrip())
        assert indent == 4, (
            f"cmd_doctor source line {idx + 1}: _doctor_gpu_gate call indented "
            f"{indent} spaces — must sit at function-body top level "
            "(outside any `if not va_ok:` block)")


# ---------------- ⑨ CP bind host（BOK_BIND_HOST，M2.3 补课） ----------------


def _patch_prod_unit_deps(monkeypatch, tmp_path: Path) -> None:
    """_prod_units 的环境依赖全部钉到无害桩（不碰真实 app-data / 模型路径）。"""
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bok, "repo_python", lambda: "py")
    monkeypatch.setattr(bok, "_embedded_livekit", lambda: None)
    monkeypatch.setattr(bok, "_agent_prod_env", lambda: {})
    monkeypatch.setattr(bok, "_interp_env", lambda env: {})
    monkeypatch.setattr(bok, "_control_plane_env", lambda db: {})


def _cp_unit_args(monkeypatch, tmp_path: Path) -> list[str]:
    _patch_prod_unit_deps(monkeypatch, tmp_path)
    units = {name: args for name, args, _env, _comment in bok._prod_units()}
    return units["bok-control-plane"]


def test_prod_units_cp_bind_host_defaults_loopback(monkeypatch, tmp_path: Path) -> None:
    """缺省恒 127.0.0.1（本机单用户形态行为零变化）；serve 路径消费同一 helper。"""
    monkeypatch.delenv("BOK_BIND_HOST", raising=False)
    argv = _cp_unit_args(monkeypatch, tmp_path)
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    serve_src = inspect.getsource(bok.cmd_serve)
    assert "_cp_bind_host()" in serve_src, (
        "cmd_serve must consume the same _cp_bind_host() helper as _prod_units")


def test_prod_units_cp_bind_host_opt_in_wildcard(monkeypatch, tmp_path: Path) -> None:
    """BOK_BIND_HOST=0.0.0.0 显式 opt-in 后单元参数携带该 host（--open-firewall
    的 :8000 规则只在此形态下有意义）。"""
    monkeypatch.setenv("BOK_BIND_HOST", "0.0.0.0")
    argv = _cp_unit_args(monkeypatch, tmp_path)
    assert argv[argv.index("--host") + 1] == "0.0.0.0"


def test_prod_units_cp_bind_host_blank_env_falls_back(monkeypatch, tmp_path: Path) -> None:
    """空串/纯空白 env 等价未设（`or` 兜底），不得下发空 --host。"""
    monkeypatch.setenv("BOK_BIND_HOST", "   ")
    argv = _cp_unit_args(monkeypatch, tmp_path)
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
