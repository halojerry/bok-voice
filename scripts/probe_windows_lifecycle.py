#!/usr/bin/env python
"""Windows 无头部署生命周期探针（site-delivery Task 2）:down 停止语义 + schtasks 契约。

背景:
  site-delivery 计划把 Bok Voice 无头部署到 Windows:进程管理从 mac/launchd 形态
  迁到「常驻 worker + Task Scheduler(schtasks)」。今天 tools/bok.py 的 cmd_down 在
  Windows 上静默失效——os.killpg 在 Windows 不存在,AttributeError 不在
  except (ProcessLookupError, PermissionError, OSError) 元组里,被外层
  `except Exception: continue` 整个吞掉,pidfile 一条都杀不掉。本探针把目标
  停止/生命周期语义钉成可执行契约,是 M2(Windows down 修复)验收的量尺:

  A 段「down 停止语义」全平台执行——POSIX 腿必须在本机绿(killpg 契约的
    回归门禁);Windows 腿 M2 起同样实跑(spawn 走 CREATE_NEW_PROCESS_GROUP、
    探活 tasklist、停止 taskkill /T /F),由 windows-latest CI(M4)实跑。
    步骤:用 bok._start_proc 起「子进程→孙进程」真进程树(POSIX
    start_new_session=True 与 bok 完全同款,杀的是进程组)→ pidfile 落临时
    目录 → monkeypatch 把 cmd_down 的三个副作用出口(run 目录/legacy data
    目录/孤儿清扫)指到临时目录与空操作(零改动 bok.py)→ 跑真 cmd_down →
    断言树成员全灭。死亡判据:POSIX = waitpid 收割直接子进程(zombie 上
    kill(pid,0) 仍成功)+ os.kill(pid,0) 轮询;Windows 目标判据 = tasklist
    按 PID 查询,目标停止命令 = `taskkill /PID <pid> /T /F`(M2 已落地的
    正主)。防御性 [skip] 门(M2 前的遗留)只剩死代码保险价值:只认
    nt + ValueError 且消息点名 start_new_session 这一种签名(M2 起 nt spawn
    走 CREATE_NEW_PROCESS_GROUP,该形态理论上不再出现)才打 [skip] 并附
    [warn] 提示,依赖步随跳,不计入退出码;其余任何 spawn 异常一律
    fatal(exit 2),绝不静默漂绿。

  B 段「schtasks 生命周期」仅 Windows 实跑:纯函数生成 Task Scheduler XML
    (onstart 触发 + RestartOnFailure + SYSTEM principal,一 unit 一 task)→
    `schtasks /create /tn <name> /xml <file> /f` → `/query` → `/run` →
    Exec 子进程(ping.exe)拉起确认 → `/end` → **/end 必须连 Exec 子进程一起停**
    (B5b,M2-fix:Task Scheduler 只终止 Exec 动作进程 cmd.exe,链式子进程会存活
    ——旧断言只查 /query 零残留,对幸存子进程结构性失明) → best-effort
    taskkill /IM ping.exe /T /F 清理 → `/delete /tn <name> /f` → `/query`
    必须失败(零残留)。ping 前后 PID 集合差分定位任务拉起的子进程,防把机器上
    无关 ping.exe 记到任务头上。非 Windows 平台把命令序列与完整 XML 原样打
    [skip] 供评审与日后排障,不影响退出码。Windows 实跑但 /create 被
    access-denied 类错误拒绝(CI runner 提权状态不定,SYSTEM principal 任务
    注册需要管理员)→ [warn] 大声提示 + B 段整段 [skip]——skip 不计 pass
    (退出码只看真实执行步),已执行步 FAIL 仍 exit 1,绝不静默漂绿。

用法:
  python scripts/probe_windows_lifecycle.py [--task-name NAME]
      [--tree-timeout 10] [--skip-schtasks]

env:
  无必需 env。临时目录走系统 tempfile;不依赖 CP/本地模型/网络。

前置:
  纯 stdlib(零三方依赖);以 importlib 方式加载仓库 tools/bok.py 作被测对象,
  不改其一行。退出码:1 = 有已执行步 FAIL;2 = 环境连 A 段都跑不了(导入
  bok / 起进程失败);skip 一律不影响退出码。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

# Windows 控制台默认代码页(cp1252/GBK)不是 UTF-8:探针输出含中文步骤标签,
# 首 print 即 UnicodeEncodeError(2026-09-16 CI windows-latest 实证,crash 在
# main() 第一行 [info])。stdout/stderr 重配 UTF-8——替身流(io.StringIO 捕获)
# 没有 reconfigure,逐流 try 不阻断探针本体。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO_ROOT = Path(__file__).resolve().parents[1]
BOK_PY = REPO_ROOT / "tools" / "bok.py"

_RESULTS: list[tuple[bool, str]] = []

# 被测子进程树:child 由 bok._start_proc 拉起(会话组长),grandchild 由 child
# 再拉一层(默认继承 child 进程组)——killpg / taskkill /T 两套语义都要能连它
# 一起清掉。grandchild pid 由 child 落盘供探针断言。
_CHILD_CODE = (
    "import pathlib, subprocess, sys, time\n"
    "root = pathlib.Path(sys.argv[1])\n"
    "p = subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(600)\"])\n"
    "(root / 'grandchild.pid').write_text(str(p.pid))\n"
    "time.sleep(600)\n"
)


def _record(ok: bool, label: str, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {label}"
    if detail:
        line += f" —— {detail}"
    print(line, flush=True)
    _RESULTS.append((ok, label))


def _skip(label: str, detail: str = "") -> None:
    line = f"[skip] {label}"
    if detail:
        line += f" —— {detail}"
    print(line, flush=True)


def _load_bok() -> Any:
    """以文件位置加载 tools/bok.py(无包结构,零改动);失败由调用方记环境致命。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("bok_under_probe", str(BOK_PY))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {BOK_PY} 构造 import spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- A 段 ----


def _posix_dead(pid: int) -> bool:
    """POSIX 死亡判据。

    child 是本探针的直接子进程(_start_proc 丢弃了 Popen,无人 wait),killpg
    SIGTERM 后先变 zombie——zombie 上 os.kill(pid,0) 仍成功,必须 waitpid 收割
    才算死;grandchild 非本进程子进程,孤儿化后由 launchd/init 代收,走
    os.kill(pid,0) 轮询。"""
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return True
    except (ChildProcessError, OSError):
        pass  # 非本进程子进程 / 已被收割 → 落到存活探测
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _win_pid_alive(pid: int) -> bool:
    """Windows 存活判据:tasklist 按 PID 过滤(目标契约的观测面,CI 实跑)。"""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return True  # 查询失败保守当存活,避免假绿
    return f'"{pid}"' in (r.stdout or "")


def _dead_fn() -> Any:
    return (lambda p: not _win_pid_alive(p)) if os.name == "nt" else _posix_dead


def _wait_all_dead(pids: list[int], timeout: float) -> bool:
    is_dead = _dead_fn()
    deadline = time.monotonic() + timeout
    while True:
        alive = [p for p in pids if not is_dead(p)]
        if not alive:
            return True
        if time.monotonic() >= deadline:
            print(f"[info] 轮询 {timeout}s 后仍存活: {alive}", flush=True)
            return False
        time.sleep(0.2)


def _best_effort_kill(pid: int) -> None:
    """探针自身卫生措施(非被测语义):异常路径上不留 600s 挂尸树。"""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def _wait_grandchild(tmp: Path, timeout: float = 10.0) -> int | None:
    marker = tmp / "grandchild.pid"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                return int(marker.read_text().strip())
            except ValueError:
                return None
        time.sleep(0.1)
    return None


def run_section_a(mod: Any, tree_timeout: float) -> str:
    """返回 'ok' / 'skipped'(Windows 今日腿) / 'fatal'(环境跑不了,exit 2)。"""
    tmp = Path(tempfile.mkdtemp(prefix="bok-lifecycle-probe-"))
    print(f"[info] A 段临时目录: {tmp}", flush=True)
    # cmd_down 的三个副作用出口全部隔离到临时目录(零改动 bok.py):
    #   app_data_dir -> tmp  ⇒ run_dir = tmp/run(pidfile 战场);
    #   ROOT -> tmp          ⇒ legacy data 扫描落在 tmp/data(空,不碰仓库);
    #   _sweep_orphan_workers -> no-op ⇒ 勿清扫真机上可能活着的 agent worker。
    orig_app_data_dir = mod.app_data_dir
    orig_root = mod.ROOT
    orig_sweep = mod._sweep_orphan_workers
    mod.app_data_dir = lambda: tmp
    mod.ROOT = tmp
    mod._sweep_orphan_workers = lambda: []
    cleanup_pids: list[int] = []
    keep_tmp = True  # 有 FAIL 时保留现场(日志/pid 证据);全过即清
    try:
        (tmp / "logs").mkdir(exist_ok=True)
        (tmp / "data").mkdir(exist_ok=True)
        pidfile = tmp / "run" / "probe-tree.pid"
        logfile = tmp / "logs" / "probe-tree.log"

        # A1 起真进程树(bok._start_proc 同款:POSIX start_new_session=True)
        try:
            child_pid = mod._start_proc(
                [sys.executable, "-c", _CHILD_CODE, str(tmp)], pidfile, logfile)
        except Exception as exc:
            # 窄匹配:只有 nt + ValueError 且消息点名 start_new_session 这一种
            # 形态才走防御性 [skip](M2 前的遗留门,现仅剩死代码保险价值——
            # M2 起 nt spawn 走 CREATE_NEW_PROCESS_GROUP,不应再出现)。其余任何
            # 异常(解释器问题/杀软拦杀/路径/权限…)一律 fatal(exit 2)——绝不
            # 能把无关的 spawn 失败伪装成 skip,让 Windows 腿在 CI 里静默漂绿、
            # 什么都没测。
            if (
                os.name == "nt"
                and isinstance(exc, ValueError)
                and "start_new_session" in str(exc)
            ):
                keep_tmp = False  # 还什么都没起,无需留现场
                print(
                    "[warn] Windows 停止语义腿(A 段)按防御性 [skip] 门跳过"
                    "(M2 已落地,该门仅剩死代码保险——出现即说明 Windows spawn "
                    "语义回退了);本次运行没有测到任何 Windows down 行为,"
                    "勿当 Windows 验收依据",
                    flush=True,
                )
                _skip(
                    "A1 起进程树(bok._start_proc,Windows 腿)",
                    f"start_new_session ValueError(M2 后不应出现) -> {exc!r};"
                    f"目标停止命令 = taskkill /PID <pid> /T /F",
                )
                return "skipped"
            print(f"[fatal] A 段环境无法起进程: {exc!r}", flush=True)
            return "fatal"
        gc_pid = _wait_grandchild(tmp)
        pidfile_ok = pidfile.exists() and pidfile.read_text().strip() == str(child_pid)
        _record(
            child_pid > 0 and pidfile_ok and gc_pid is not None,
            "A1 起真进程树(子→孙,bok._start_proc)",
            f"child={child_pid} grandchild={gc_pid} pidfile 写入={pidfile_ok}",
        )
        if gc_pid is None:
            cleanup_pids.append(child_pid)
            _record(False, "A2 下树前存活 sanity", "grandchild pid 未落盘(树没立起来)")
            return "ok"
        cleanup_pids.extend([child_pid, gc_pid])
        is_dead = _dead_fn()
        alive = [p for p in (child_pid, gc_pid) if not is_dead(p)]
        _record(
            len(alive) == 2,
            "A2 下树前存活 sanity(child+grandchild 都在)",
            f"存活={alive}",
        )
        if len(alive) != 2:
            return "ok"  # 树站不住,down 语义测了也是空转

        # A3 跑真 cmd_down(副作用出口已全部指到临时目录)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.cmd_down()
        out = buf.getvalue().strip()
        _record(
            rc == 0 and f"stopped {pidfile.stem}" in out and str(child_pid) in out,
            "A3 cmd_down() 按 pidfile 杀进程组",
            f"rc={rc} 输出={out!r}",
        )

        # A4 树成员全灭(核心断言:child+grandchild 一个不留)
        all_dead = _wait_all_dead([child_pid, gc_pid], timeout=tree_timeout)
        _record(
            all_dead,
            f"A4 down 后树成员全灭(≤{tree_timeout}s 轮询)",
            f"pids=[child {child_pid}, grandchild {gc_pid}]",
        )
        if all_dead:
            cleanup_pids.clear()
            keep_tmp = False
        return "ok"
    finally:
        mod.app_data_dir = orig_app_data_dir
        mod.ROOT = orig_root
        mod._sweep_orphan_workers = orig_sweep
        for pid in cleanup_pids:
            _best_effort_kill(pid)
        if not keep_tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- B 段 ----


def build_unit_task_xml(unit: str, command: str, arguments: str = "",
                        working_dir: str = "") -> str:
    """生成单个 stack unit 的 Task Scheduler 任务 XML(目标契约,纯函数)。

    要点:onstart 触发(开机自起)+ RestartOnFailure(崩了自动拉回)+
    SYSTEM principal(无头会话不依赖登录用户);一 unit 一 task,
    URI 收口在 \\BokVoice\\<unit>。元素顺序必须按 Task Scheduler schema
    (triggers → principals → settings → actions),否则 /create 直接拒。"""
    uri = f"\\BokVoice\\{unit}"
    desc = (f"Bok Voice stack unit: {unit} (onstart + RestartOnFailure, SYSTEM). "
            f"Generated by scripts/probe_windows_lifecycle.py")
    args_xml = (f"      <Arguments>{_xml_escape(arguments)}</Arguments>\n"
                if arguments else "")
    wd_xml = (f"      <WorkingDirectory>{_xml_escape(working_dir)}</WorkingDirectory>\n"
              if working_dir else "")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{_xml_escape(desc)}</Description>
    <URI>{uri}</URI>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(command)}</Command>
{args_xml}{wd_xml}    </Exec>
  </Actions>
</Task>
"""


def _schtasks_plan(task_name: str, xml_file: str = "<utf-16 编码的 XML 文件>") -> list[str]:
    return [
        f"schtasks /create /tn {task_name} /xml \"{xml_file}\" /f",
        f"schtasks /query /tn {task_name}          # rc=0 注册可查",
        f"schtasks /run /tn {task_name}            # rc=0 按需启动",
        "tasklist /FI \"IMAGENAME eq ping.exe\"   # B4b: Exec 子进程已被拉起(前后 PID 差分)",
        f"schtasks /end /tn {task_name}            # rc=0 停实例",
        "tasklist /FI \"IMAGENAME eq ping.exe\"   # B5b: 子进程必须已死(存活=FAIL)",
        "taskkill /IM ping.exe /T /F              # best-effort 清理(不留残留)",
        f"schtasks /delete /tn {task_name} /f      # rc=0 卸载",
        f"schtasks /query /tn {task_name}          # 必须失败(零残留)",
    ]


def _skip_section_b(task_name: str) -> None:
    """非 Windows:把目标契约按原样打出来供评审(不影响退出码)。"""
    unit = "control-plane"
    xml = build_unit_task_xml(
        unit,
        r"C:\Windows\System32\cmd.exe",
        "/c ping -n 30 127.0.0.1 > nul",
        r"C:\ProgramData\BokVoice",
    )
    _skip("B 段 schtasks 生命周期(仅 Windows 实跑)", f"任务名={task_name} unit={unit}")
    print("[skip] 目标契约命令序列(Windows 实跑时逐条断言 rc):", flush=True)
    for line in _schtasks_plan(task_name):
        print(f"[skip]   {line}", flush=True)
    print("[skip] 任务 XML 全文(utf-16 落盘,schtasks /xml 只认带 BOM 的 UTF-16):", flush=True)
    for line in xml.splitlines():
        print(f"[skip]   {line}", flush=True)


def _ping_pids() -> set[int]:
    """机器上 ping.exe 的存活 PID 集（tasklist CSV；查询异常=空集，由调用方
    把「前置缺失」与「已死」区分开——B4b/B5b 的断言都建立在 PID 差分上，
    防把机器上无关 ping.exe 记到任务头上）。"""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ping.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return set()
    pids: set[int] = set()
    for line in (r.stdout or "").splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "ping.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


# access-denied 类错误标记（stdout/stderr 小写后子串匹配）：SYSTEM principal
# 任务注册需要管理员，CI runner 提权状态不定——/create 被这类错误拒绝属环境
# 限制（非被测语义失败），B 段整段 [skip]（skip 不计 pass；已执行步 FAIL 仍
# exit 1）。英文报错与中文 Windows 的「拒绝访问」都收。
_ACCESS_DENIED_MARKERS = ("access is denied", "access denied", "拒绝访问",
                          "0x80070005")


def _access_denied(r: "subprocess.CompletedProcess[str]") -> bool:
    text = f"{r.stdout or ''}\n{r.stderr or ''}".lower()
    return any(marker in text for marker in _ACCESS_DENIED_MARKERS)


class _AdminDenied(Exception):
    """schtasks /create 被 access-denied 类错误拒绝——环境无管理员权限，
    后续 B 段步骤失去载体（任务没注册成），由调用方整段 [skip]。"""


def _run_section_b_windows(task_name: str, unit: str) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bok-schtasks-probe-"))
    # 探针任务动作用 30s 长 ping:给 /run 与 /end 一个真实运行中的实例可操作。
    xml = build_unit_task_xml(
        unit,
        os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe"),
        "/c ping -n 30 127.0.0.1 > nul",
        str(tmp),
    )
    xml_file = tmp / f"{task_name}.xml"

    def _sch(step: str, sch_args: list[str], expect_ok: bool,
             *, admin_graceful: bool = False) -> None:
        cmd = " ".join(["schtasks", *sch_args])
        print(f"[info] $ {cmd}", flush=True)
        try:
            r = subprocess.run(["schtasks", *sch_args],
                               capture_output=True, text=True, timeout=60)
        except Exception as exc:
            _record(False, step, f"$ {cmd} -> 异常 {exc!r}")
            return
        tail = ((r.stdout or "").strip() or (r.stderr or "").strip()).splitlines()
        detail = f"rc={r.returncode}" + (f" {tail[-1][:160]}" if tail else "")
        # admin_graceful 只给 B2 用：rc!=0 且输出属 access-denied 类 → 抛
        # _AdminDenied 让整段走 [skip]（环境限制）；其余任何失败照常记 FAIL。
        if admin_graceful and r.returncode != 0 and _access_denied(r):
            raise _AdminDenied(detail)
        _record((r.returncode == 0) == expect_ok, step, detail)

    try:
        # schtasks /xml 只认带 BOM 的 UTF-16(utf-8 无 BOM 会报 0x8007000D)。
        xml_file.write_text(xml, encoding="utf-16")
        roundtrip = xml_file.read_text(encoding="utf-16").lstrip().startswith("<?xml")
        _record(xml_file.exists() and roundtrip,
                "B1 生成 Task Scheduler XML(onstart+RestartOnFailure+SYSTEM,一 unit 一 task)",
                str(xml_file))
        ping_before = _ping_pids()  # 基线:只把 /run 之后新出现的 ping 记到任务头上
        try:
            _sch("B2 /create /xml /f(注册任务)", ["/create", "/tn", task_name,
                                                 "/xml", str(xml_file), "/f"], True,
                 admin_graceful=True)
            _sch("B3 /query(注册可查)", ["/query", "/tn", task_name], True)
            _sch("B4 /run(按需启动)", ["/run", "/tn", task_name], True)
            time.sleep(2.0)
            # B4b 前置确认:/run 真的拉起了 Exec 子进程(ping -n 30 约 29s,2s 处必活)。
            # 没有这一步,B5b 的「/end 杀干净」可以是空转的假绿。
            child_pids = _ping_pids() - ping_before
            _record(bool(child_pids),
                    "B4b Exec 子进程(ping.exe)已被任务拉起",
                    f"任务新增 pids={sorted(child_pids) or '无(/run 没拉起子进程,后续断言不可信)'}")
            _sch("B5 /end(停运行实例)", ["/end", "/tn", task_name], True)
            time.sleep(1.0)
            # B5b 核心断言(M2-fix):/end 必须连 Exec 子进程一起停。Task Scheduler
            # 只终止动作进程 cmd.exe,链式子进程会存活——旧断言只查 /query 零残留,
            # 对幸存子进程结构性失明。prod 侧 cmd_prod_uninstall 按此假设写
            # (pidfile 补杀,勿按镜像名杀共享镜像)。
            survivors = _ping_pids() & child_pids
            _record(bool(child_pids) and not survivors,
                    "B5b /end 必须连 Exec 子进程一起停(杀不到=schtasks /end 不够用,停栈/卸载须补 taskkill)",
                    f"存活={sorted(survivors) or '无'}")
            # best-effort 卫生清理(非被测语义):探针绝不留 ping 残留。
            if _ping_pids():
                print("[info] best-effort cleanup: taskkill /IM ping.exe /T /F", flush=True)
                try:
                    subprocess.run(["taskkill", "/IM", "ping.exe", "/T", "/F"],
                                   capture_output=True, timeout=15)
                except Exception:
                    pass
            _sch("B6 /delete /f(卸载任务)", ["/delete", "/tn", task_name, "/f"], True)
            _sch("B7 /query 零残留(查不到才算过)", ["/query", "/tn", task_name], False)
        except _AdminDenied as exc:
            # CI runner 提权状态不定:/create 被拒=环境无管理员权限,不是契约失败。
            # [warn] 大声 + B 段整段 [skip]——skip 不进 _RESULTS 不计 pass,
            # 已执行步 FAIL 仍会把退出码压到 1,绝不静默漂绿。
            print(flush=True)
            print(f"[warn] B 段 /create 被 access-denied 类错误拒绝(无管理员权限): {exc}", flush=True)
            print("[warn] schtasks 生命周期腿整段 [skip]——skip 不等于 PASS;"
                  "有管理员权限的环境(实机/提权 CI)必须全绿", flush=True)
            _skip("B2 /create /xml /f(注册任务)", f"access-denied({exc}),任务未注册")
            for label in (
                "B3 /query(注册可查)",
                "B4 /run(按需启动)",
                "B4b Exec 子进程(ping.exe)已被任务拉起",
                "B5 /end(停运行实例)",
                "B5b /end 必须连 Exec 子进程一起停(杀不到=schtasks /end 不够用,停栈/卸载须补 taskkill)",
                "B6 /delete /f(卸载任务)",
                "B7 /query 零残留(查不到才算过)",
            ):
                _skip(label, "B2 /create 无管理员权限,任务未注册")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def run_section_b(task_name: str, force_skip: bool) -> None:
    if os.name != "nt" or force_skip:
        _skip_section_b(task_name)
        return
    print(f"[info] B 段实跑(需要管理员权限注册 SYSTEM 任务): task={task_name}", flush=True)
    _run_section_b_windows(task_name, "control-plane")


# ---------------------------------------------------------------- main ----


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bok Windows 生命周期探针(down 停止语义 + schtasks 契约)")
    parser.add_argument("--task-name",
                        default=f"BokVoiceProbe-{int(time.time())}",
                        help="B 段 schtasks 任务名(默认带时间戳唯一名,防与真实 BokVoice 任务撞名)")
    parser.add_argument("--tree-timeout", type=float, default=10.0,
                        help="A 段 down 后树全灭轮询上限秒数")
    parser.add_argument("--skip-schtasks", action="store_true",
                        help="强制跳过 B 段(仍打印契约,不执行)")
    args = parser.parse_args(argv)

    print(f"[info] platform={sys.platform} os.name={os.name} "
          f"python={sys.version.split()[0]}", flush=True)
    print(f"[info] 被测对象: {BOK_PY}", flush=True)

    if not BOK_PY.exists():
        print(f"[fatal] 找不到被测对象 {BOK_PY}", flush=True)
        return 2
    try:
        mod = _load_bok()
    except Exception as exc:
        print(f"[fatal] 导入 tools/bok.py 失败,环境连 A 段都跑不了: {exc!r}", flush=True)
        return 2

    print("[info] ===== A 段:down 停止语义(全平台执行) =====", flush=True)
    status = run_section_a(mod, args.tree_timeout)
    if status == "fatal":
        print("PROBE ENV FAIL：A 段环境无法执行(见上)", flush=True)
        return 2

    print("[info] ===== B 段:schtasks 生命周期(仅 Windows 实跑) =====", flush=True)
    run_section_b(args.task_name, args.skip_schtasks)

    failed = [label for ok, label in _RESULTS if not ok]
    print(flush=True)
    if failed:
        print(f"PROBE FAIL：{len(failed)}/{len(_RESULTS)} 步失败 -> {'; '.join(failed)}",
              flush=True)
        return 1
    note = ("(Windows 专属腿在本平台按契约 [skip],不计入退出码)"
            if os.name != "nt" else "")
    print(f"PROBE PASS：{len(_RESULTS)}/{len(_RESULTS)} 步全过 {note}".rstrip(),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
