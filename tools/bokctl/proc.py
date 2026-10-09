#!/usr/bin/env python
"""proc 域(进程生命周期:pidfile 读写/来源戳/lstart 复用闸/kill_tree/孤儿清扫
双件/monitor veto 判定;G2 W② 从 core 搬出,搬运纪律=穿模块对象调用)。

- 本模块只 `from bokctl import core`(及 servers 域)拿模块对象:凡仍住在 core 的
  名字(_pid_alive/_relaxed_healthy 等——共享件判留 core,
  与 doctor 波同判:healthy/_probe_worker/_http_call/_pid_alive 被
  status/doctor/prod/serve 多面吃)一律 `core.X` 调用时取——patch 与后续域
  搬运在 core 侧保持可见(patch 缝=模块属性);路径/平台锚(ROOT/app_data_dir)
  paths 波(2026-10-04)后穿 `paths.X` 取。
- 本域自有函数(_ps_field/_process_serve_root/_pid_origin_foreign/
  _pidfile_alive_stamped/_pid_reused_stale/_write_proc_stamps/_kill_proc_tree/
  _kill_pidfile/_ensure_monitor/_cp_active_calls/_monitor_kill_round/
  _sweep_orphan_workers/_sweep_orphan_listeners 等)域内裸名互调(同模块全局=
  call-time 可 patch)。
- 留守 core 的近邻(边界记录,2026-10-04;servers 波更新):`_start_proc`/
  `_spawn_kwargs`/`_rotate_log`/`_stop_pidfile` spawn 原语原判留 core(serve
  面+本域 _ensure_monitor 跨面共用),**servers 波(W②)已随服务面搬入
  bokctl.servers**——本域 `_ensure_monitor` 改穿 `servers._start_proc`;
  `cmd_down`(prod.cmd_prod_uninstall 与 node_agent 都吃)/`cmd_monitor`(拉
  servers._worker_specs=serve 装配面)/`cmd_serve`+`cmd_up`(服务面整族)——
  前两命令留 core(monitor/down;已改穿 servers.X 取件),serve/up 两命令随
  服务面入 servers;`_pid_alive` 因 prod 消费留 core(doctor 波先例)。
- 测试面:patch 一律走 tests/_bokpatch.py(patch_bok;PATCH_TARGETS 已把
  _sweep_orphan_workers/_sweep_orphan_listeners/_kill_proc_tree/_ensure_monitor/
  _ps_field 改道 bokctl.proc);facade 读用 bok.proc.X。
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

from bokctl import core, paths, servers


def _write_proc_stamps(pidfile: Path, pid: int) -> None:
    """pidfile + proc-<pid>.root 来源戳（_start_proc 落笔两件套的单点提炼）。

    cmd_monitor 外部手跑时也自写（2026-09-22 monitor 盲斑修复）：外部启动
    原本不落任何痕迹——_ensure_monitor 探不到单例会再拉一个（双监控环），
    跨树杀守卫对无戳进程 fail-open 不保护。经 _start_proc 拉起时会写两次
    （同 pid 同内容，幂等无害）。"""
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(pid))
    try:
        (pidfile.parent / f"proc-{pid}.root").write_text(f"{paths.ROOT}\t{_ps_field(pid, 'lstart=')}\n")
    except Exception:
        pass  # 标记写不出=来源未知，清扫走原语义；绝不影响起进程


def _pidfile_alive_stamped(pidfile: Path) -> bool:
    """_ensure_monitor 单例判定的 lstart 加强版（2026-09-22 monitor 盲斑 2）。

    _pid_alive 是纯 pid 探活——pidfile 残留死 pid 被无关进程复用时误判存活，
    _ensure_monitor 跳过重拉 = 栈从此无 monitor。本函数在 pid 活之上追加
    来源戳比对：proc-<pid>.root 记的 lstart 与 ps 现值不一致 = pid 已被
    复用，判死（stale pidfile，该重拉）。戳缺失/lstart 读不出时保守当存活
    （fail-open 旧语义）——外部旧式启动的 monitor 没有戳，不能误杀单例。"""
    if not core._pid_alive(pidfile):
        return False
    try:
        pid = int(pidfile.read_text().strip())
        marker = pidfile.parent / f"proc-{pid}.root"
        parts = marker.read_text().strip().split("\t")
    except Exception:
        return True  # pid 活但戳读不出：fail-open 当存活（与 _process_serve_root 同纪律）
    if not (len(parts) == 2 and parts[1]):
        return True  # 无戳/坏戳：外部旧式启动，保守当活
    cur = _ps_field(pid, "lstart=")
    if not cur:
        return True  # ps 读不出（如 Windows 无 ps）：保守当活
    return cur == parts[1]


def _pid_reused_stale(pidfile: Path, pid: int) -> bool:
    """pidfile 指向的 pid 是否已被无关进程复用（down 清杀前置闸，2026-10-02）。

    判据=来源戳 ``proc-<pid>.root`` 记的 lstart 与 live 进程 ``ps -o lstart=``
    现值不一致（与 ``_pidfile_alive_stamped`` 同款比对——该判据此前只接
    monitor 单例判定，清杀路径缺失 = stale pidfile 的 pid 被复用时来源「未知」
    fail-open 照杀，误杀无辜进程）。戳搜索面=pidfile 同目录 + app-data ``run/``
    （legacy ``data/sidecar-*.pid`` 的戳落在 run 面）。无戳（旧式启动）/坏戳/
    ps 读不出 → False（fail-open 旧语义：无戳遗留照杀，未知绝不挡杀——与
    ``_pid_origin_foreign`` 同纪律）。"""
    recorded = ""
    for base in (pidfile.parent, paths.app_data_dir() / "run"):
        try:
            parts = (base / f"proc-{pid}.root").read_text().strip().split("\t")
        except Exception:
            continue
        if len(parts) == 2 and parts[1]:
            recorded = parts[1]
            break
    if not recorded:
        return False
    cur = _ps_field(pid, "lstart=")
    if not cur:
        return False
    return cur != recorded


class _KillTreeError(RuntimeError):
    """Windows taskkill 停树失败（带 rc/输出尾）——必须浮出，不得静默吞
    （旧版 os.killpg 在 nt 抛 AttributeError 被外层 except 吞掉 = down 静默失效）。"""


def _kill_proc_tree(pid: int) -> None:
    """按 _start_proc 的会话/进程组语义终止整棵进程树。

    POSIX：与旧代码逐字节同款——killpg(SIGTERM)，(ProcessLookupError,
    PermissionError, OSError) 时回退单杀；异常照旧上抛给调用方。
    Windows：taskkill /T /F 沿父子树收割（_start_proc 用 CREATE_NEW_PROCESS_GROUP
    建组，见 _spawn_kwargs）。rc=128（进程已不在）等价 ProcessLookupError，静默
    放行；其余失败抛 _KillTreeError——真失败必须浮出，绝不重演静默吞。
    """
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - taskkill 缺失/超时都要浮出
            raise _KillTreeError(f"taskkill /PID {pid} /T /F error: {exc!r}") from exc
        if r.returncode == 128:  # process not found = 已死，对齐 POSIX 静默放行
            return
        if r.returncode != 0:
            tail = ((r.stderr or "").strip() or (r.stdout or "").strip()).splitlines()
            detail = tail[-1][:200] if tail else ""
            raise _KillTreeError(
                f"taskkill /PID {pid} /T /F rc={r.returncode} {detail}")
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)


def _kill_pidfile(pidfile: Path) -> None:
    """按 pidfile 杀进程组(_start_proc 是会话组长,子进程一并清)。

    他树戳守卫（2026-09-22）：monitor respawn 路径读到被他树覆写的共享
    pidfile 时拒绝杀——不挡「本树/无戳」，与 down 纪律同款 fail-open。"""
    try:
        pid = int(pidfile.read_text().strip())
        foreign, root = _pid_origin_foreign(pid)
        if foreign:
            print(f"[kill] skip {pidfile.name}: pid {pid} 属另一代码树（{root}）"
                  "——他树进程永不收割（先在对方树 down）", file=sys.stderr)
            return
        _kill_proc_tree(pid)
    except _KillTreeError as exc:
        # monitor respawn 路径的 best-effort 停止：Windows 真失败留痕不炸环。
        print(f"[kill] {pidfile.name}: {exc}", file=sys.stderr)
    except Exception:
        pass


def _ensure_monitor(py) -> None:
    """C6-1:常驻 worker monitor 单例拉起(pidfile 存活即跳过)。

    2026-09-22 盲斑修复：①存活判定升级为 lstart 比对（_pidfile_alive_stamped，
    pidfile 残留 pid 被复用不再误判活 = 栈无 monitor）；②跨树可观测——monitor
    属他树时打日志跳过（共享栈模型既定行为，从静默变有声，不改变动作）。"""
    run_dir = paths.app_data_dir() / "run"
    log_dir = paths.app_data_dir() / "logs"
    pidfile = run_dir / "monitor.pid"
    if _pidfile_alive_stamped(pidfile):
        try:
            pid = int(pidfile.read_text().strip())
            foreign, root = _pid_origin_foreign(pid)
            if foreign:
                print(f"[bok] monitor 已在运行且属另一代码树（{root}）——跳过拉起"
                      "（共享栈模型；要换装先去对方树 down）")
        except Exception:
            pass
        return
    servers._start_proc(
        [str(py), str(paths.ROOT / "tools" / "bok.py"), "monitor"],  # G2 W①:re-exec 走门面 launcher
        pidfile,
        log_dir / "monitor.log",
        env={"BOK_MONITOR": "1"},
    )
    print("[bok] worker monitor started (livekit restart → respawn all workers)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """monitor 探测禁跟随重定向:URL 是钉死的环回常量(无用户输入,SSRF 前提
    不存在),唯一要防的係 CP 被攻破后借 302 把带 BOK_CP_TOKEN 的探测引去外部
    主机——重定向一律升 HTTPError,token 永不出环回。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def _cp_active_calls() -> int | None:
    """CP 在途通话数(None=CP 不可达,monitor 退回纯连续失败口径)。

    respawn 前置闸的数据源(2026-09-17 call-54cab865 通话第 63s 被 monitor
    强杀实证):healthy() 係 1s TCP 探测,GPU 满载整机卡顿一瞬三个 worker 可以
    同时探不上——旧逻辑即刻 kill+respawn=在途通话陪葬。有通话在途时把门槛
    从 2 轮(≥10s)抬到 12 轮(≥60s):活 worker 卡顿永不连吃 60s,真死 worker
    一分钟内照样补拉。"""
    try:
        req = urllib.request.Request(_MONITOR_CP_ACTIVE_URL)
        token = (os.environ.get("BOK_CP_TOKEN") or "").strip()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        _opener = urllib.request.build_opener(_NoRedirect)
        with _opener.open(req, timeout=2.0) as r:
            data = json.loads(r.read().decode("utf-8"))
        rows = data if isinstance(data, list) else (data.get("calls") or [])
        return len(rows)
    except Exception:  # noqa: BLE001 - CP 不可达=口径降级,唔阻监控环
        return None


# monitor→CP 探测端点:编译期钉死的环回常量(安全复查 SSRF 判定的锚——无任何
# 动态输入参与主机构造,重定向经 _NoRedirect 禁跟随)。
_MONITOR_CP_ACTIVE_URL = "http://127.0.0.1:8000/api/calls?status=active"

_DOWN_STREAK_NEED_IDLE = 2  # 连续 ≥2 轮(≥10s)探不上才判 down(无通话在途)
# G3 硬 veto(2026-09-25)取代旧「有通话在途门槛抬到 12 轮(≥60s)」：在途通话
# >0 时**任何探活失败都不杀**——12 轮抬门槛仍有误杀窗（LANE-AB-2026-09-25.md
# 附3：swap 颠簸下 offscript 窗 worker 被误杀 ×7，respawn 244 行）。该常量仅
# 剩 veto 打点节奏用途（长 veto 窗每 12 轮≈60s 提醒一次，防静默）。
_DOWN_STREAK_NEED_ACTIVE = 12


def _monitor_kill_round(streak: int, active_calls: int | None) -> tuple[bool, bool]:
    """单 worker 连续探活失败的处置判定（纯函数，monitor 环与单测共用）。

    返回 ``(kill, veto_log)``：
    - ``kill``：本轮该补拉。``active_calls>0`` 时恒 False——硬 veto 关死误杀窗
      （活 worker 卡顿多惨都唔杀，真死 worker 等场景间隙 active 归零立刻补拉，
      连续计数在 veto 窗内照涨所以不丢窗口）；**``active_calls is None``（CP
      不可达，在途通话状态未知）同样恒 False=保守不杀**（2026-09-27 修：旧
      ``if not active_calls`` 把 None 当 0 → idle 门槛 2 轮即杀，veto 静默失效）；
      active 为 0 时按 idle 门槛。
    - ``veto_log``：该打 veto 打点——streak 首过 idle 门槛时一次，此后每
      ``_DOWN_STREAK_NEED_ACTIVE`` 轮提醒一次（长 veto 窗不静默也不刷屏）。
    """
    if active_calls is None:
        # 状态未知 → 保守不杀，按 veto 节奏打点（streak 越高越罕见，防刷屏）。
        veto_log = streak == _DOWN_STREAK_NEED_IDLE or (
            streak > _DOWN_STREAK_NEED_IDLE and streak % _DOWN_STREAK_NEED_ACTIVE == 0
        )
        return False, veto_log
    if not active_calls:
        return streak >= _DOWN_STREAK_NEED_IDLE, False
    veto_log = streak == _DOWN_STREAK_NEED_IDLE or (
        streak > _DOWN_STREAK_NEED_IDLE and streak % _DOWN_STREAK_NEED_ACTIVE == 0
    )
    return False, veto_log


def _sweep_orphan_workers() -> list[tuple[int, str]]:
    """清扫 pidfile 体系漏掉的 agent worker/解释器/mock 客户进程（返回 [(pid, 标签)]）。

    判据：进程命令行含 agent_runtime.main / agent_runtime.interpret /
    agent_runtime.interp_lite（B 线薄线试点入口）/ scripts/runtime/mock_callee.py
    （CP detached 派生的 mock 被叫 start_new_session,
    同样绕过 pidfile 体系——房间断了会自退,但栈 down 时若仍卡响铃窗须一并清）。
    只清本项目特征进程,唔会误伤无关服务。他树戳守卫（2026-09-22）：读得出
    「拉起树」且 ≠ 本树 → 跳过不进 swept（与 _sweep_orphan_listeners 同款
    纪律——命令行特征只证明「bok 家」，来源戳才证明「谁家的」）。
    Windows（M2 定案）：**明跳**（返回空表,不清扫）。tasklist 不回命令行
    （image 只有 python.exe,无法安全区分本项目 worker——宁可少清不可误杀）；
    wmic 已弃用；PowerShell CIM 查询未在本仓 Windows 实机验证过。无头形态下
    Windows 栈整体活在单一 node_agent 任务树里（taskkill /T /F / schtasks /end
    一把清,见 _kill_proc_tree）,孤儿面远小于 mac 多单元拓扑;实装 CIM 清扫前
    诚实跳过,不假装扫过。"""
    swept: list[tuple[int, str]] = []
    seen: set[int] = set()
    if os.name == "nt":
        return swept
    try:
        ps = subprocess.run(["ps", "-axo", "pid,command"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return swept
    for line in ps.splitlines()[1:]:
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        for marker in (
            "agent_runtime.main",
            "agent_runtime.interpret",
            "agent_runtime.interp_lite",  # B 线薄线试点（2026-10-09，同槽位换入口）
            "scripts/runtime/mock_callee.py",
        ):
            if marker in parts[1]:
                if pid not in seen:
                    seen.add(pid)
                    foreign, root = _pid_origin_foreign(pid)
                    if foreign:
                        print(f"[sweep] orphan pid {pid} 属另一代码树（{root}）——不动"
                              "（他树进程永不收割；要切换先在对方 down）", file=sys.stderr)
                    else:
                        swept.append((pid, marker))
                break
    for pid, label in swept:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                continue
    return swept


def _ps_field(pid: int, field: str) -> str:
    """ps 单字段取值（lstart 身份比对用），失败返回空串（fail-closed）。"""
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", field],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _process_serve_root(pid: int) -> str:
    """来源鉴定（评审 A-P2 完整版）：该 pid 由哪棵代码树拉起。载体优先级：
    ① app-data run/proc-<pid>.root（_start_proc 落笔「ROOT<TAB>子代lstart」，
    lstart 与 ps 现值精确比对——pid 复用必然对不上，标记作废）；
    ② Linux /proc/<pid>/environ 的 BOK_SERVE_ROOT（补标记缺席路径）；
    ③ macOS `ps eww -p <pid> -o command=`（2026-09-22 补：实测能读出 env 里的
    BOK_SERVE_ROOT——隔离 HOME 接管的 worker 标记文件落在对方 app-data、
    本树 app-data 里没有，/proc 又不存在，旧版两载体全盲=来源「未知」，
    他树 worker 会被 down/清扫当无主残留收割，6c82 接管实弹踩到）。
    读不到/对不上返回空串=来源未知，调用方按未知走原语义；任何异常同空串。"""
    if os.name == "nt":
        return ""
    try:
        marker = paths.app_data_dir() / "run" / f"proc-{pid}.root"
        if marker.exists():
            parts = marker.read_text().strip().split("\t")
            if len(parts) == 2 and parts[1]:
                cur = _ps_field(pid, "lstart=")
                return parts[0] if cur and cur == parts[1] else ""
            return ""
    except Exception:
        pass
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
        for item in raw.split(b"\0"):
            if item.startswith(b"BOK_SERVE_ROOT="):
                return item.decode("utf-8", "replace").split("=", 1)[1]
    except Exception:
        pass
    try:
        # 载体③：macOS 环境经 ps eww 挂在 command 列尾部。只认变量名边界，
        # 防「某 env 值里恰好含这段字面量」误报；取非空白段（ROOT 是路径无空格）。
        # 实测边界：python 进程（=真实标的 worker/monitor）恒可读；/bin/sleep
        # 这类短 argv 二进制读不出 env——载体只服务 bok 家进程，够用。
        out = subprocess.run(
            ["ps", "eww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r"(?<![A-Za-z0-9_])BOK_SERVE_ROOT=(\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _pid_origin_foreign(pid: int) -> tuple[bool, str]:
    """pid 是否属**另一棵代码树**（2026-09-22 pidfile 清杀路径补戳）。

    返回 (foreign, root)：来源读得出且 ≠ 本树 ROOT → (True, root)；来源未知
    （旧版进程/探测失败/pid 复用对不上）或本树 → (False, …)。fail-open 与
    「down=停本树+无戳遗留」纪律一致——未知绝不挡杀，只有铁证是他树才让位。
    只对 bok 家进程有意义（任意进程 env 里有 BOK_SERVE_ROOT 即视为 bok 子代）。"""
    root = _process_serve_root(pid)
    if not root:
        return False, ""
    if os.path.realpath(root) == os.path.realpath(str(paths.ROOT)):
        return False, root
    return True, root


def _sweep_stale_root_markers() -> None:
    """清 proc-<pid>.root 残留：ps 探不到的 pid 视为死，标记删除（防标记文件
    无限积累）；ps 探测失败=未知一律保留。"""
    try:
        markers = list((paths.app_data_dir() / "run").glob("proc-*.root"))
    except Exception:
        return
    for marker in markers:
        try:
            pid = int(marker.stem.removeprefix("proc-"))
        except ValueError:
            try:
                marker.unlink()
            except Exception:
                pass
            continue
        try:
            alive = subprocess.run(
                ["ps", "-p", str(pid)], capture_output=True, text=True, timeout=5,
            ).returncode == 0
        except Exception:
            continue
        if not alive:
            try:
                marker.unlink()
            except Exception:
                pass


# 端口 → 命令行身份标记（_sweep_orphan_listeners 双条件收割用）。端口是 bok
# 固定拓扑（见 CORE_PORTS/WORKER_PORTS 语义）；身份复核防误杀同端口无关服务。
# 8081-8083 额外认 multiprocessing.spawn：livekit-agents worker 的 spawn 子代
# 命令行只有 `-c from multiprocessing.spawn import spawn_main`，父进程暴毙后
# 命令行特征丢失但仍占端口——旧代码殭尸继续注册抢 job（A/B 污染实锤类）。
_ORPHAN_PORT_OWNERS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (8000, ("control_plane.main",)),
    (8787, ("qwen3-asr",)),
    (8788, ("qwen3-tts",)),
    (8789, ("bge-embed",)),
    # Laya 决策 sidecar(:8791):uvicorn 命令行带 --app-dir services/laya-sidecar。
    (8791, ("laya-sidecar",)),
    (1235, ("mlx_lm",)),
    (1236, ("mlx_lm",)),
    (1237, ("mlx_lm",)),
    # llm-raw(1239)（2026-10-02 编排审计第二波）：queue proxy 拓扑下 mlx 的真实
    # 监听口——父进程暴毙后代理仍转发失败、孤儿 mlx 却占着 GPU 解码不放，
    # 身份标记同族（--model 路径 + mlx_lm）。
    (1239, ("mlx_lm",)),
    # settle-proxy(1238)（2026-10-03 I1 前门闸）：queue_proxy 同码同族（命令行
    # 带 services/llm-mlx/queue_proxy.py）；父进程暴毙后占着 9B 前门口令新栈
    # serve 等就绪假死——同 1239 的孤儿语义。
    (1238, ("queue_proxy",)),
    (7880, ("livekit-server",)),
    (3000, ("next", "node")),
    (8081, ("agent_runtime", "multiprocessing")),
    (8082, ("agent_runtime", "multiprocessing")),
    (8083, ("agent_runtime", "multiprocessing")),
    # 演示档 worker（opt-in）：同族身份标记——只清「占着 8084 且是 bok 家进程」
    # 的殭尸，身份不符照旧不动手。
    (8084, ("agent_runtime", "multiprocessing")),
)


def _orphan_port_owners() -> tuple[tuple[int, tuple[str, ...]], ...]:
    """``_ORPHAN_PORT_OWNERS`` 的动态版：A 线 worker 口吃
    ``_agent_worker_port()``（BOK_WORKER_PORT 错开时孤儿清扫/身份复核仍对得上
    真口；2026-10-02 审计），其余条目原样（interp 8082/8083 与静态表相同）。"""
    agent_port = core._agent_worker_port()
    if agent_port == 8081:
        return _ORPHAN_PORT_OWNERS
    return tuple(
        (agent_port, markers) if port == 8081 else (port, markers)
        for port, markers in _ORPHAN_PORT_OWNERS
    )


def _sweep_orphan_listeners(kill: bool = True,
                            healthy_ok: bool = True) -> list[tuple[int, str, int]]:
    """端口级孤儿兜底（2026-09-17 殭尸专项）：按 bok 端口表逐口查 LISTEN 进程，
    命令行身份复核通过才收割；身份不符（他人物理占用）只报警不动手。

    健康即非孤儿（2026-09-19 互杀事故收编）：宿主 CPU 风暴（Parallels 141%）
    下 serve 的 1s 健康探测假死 → 180s 等待超时退出、留下正在加载模型的健康
    子代 → 下一轮 serve 本函数把它们当孤儿杀掉 → 互杀循环、栈永远起不来。
    身份复核只证明「这是 bok 家的进程」，不证明它已死——现动手前先做一次
    放宽超时（5s）的健康探测（_relaxed_healthy：有 HTTP 健康面走 HTTP、无的
    退 TCP），探测健康 → 跳过收割（left alone），不健康才照旧收割；返回值
    swept 只含真正收割的条目（left-alone 的不进）。
    来源鉴定（2026-09-19 provenance 完整版，评审 A-P2）：marker//proc stamp
    读得出「拉起树」时，他树的 bok 栈永不收割——它可能正在加载模型（跨树互杀
    同款根因），多会话纪律也禁碰他树进程；down 同样不收他树。读不出 stamp
    （旧版进程/ps 失败）按来源未知走原健康闸/收割语义，旧行为兜底不变。
    kill=False 只探测+报告不动手（健康的同样报 left alone；干跑/测试用）。
    Windows 明跳（同 _sweep_orphan_workers：无安全身份来源，宁可少清不误杀）。"""
    swept: list[tuple[int, str, int]] = []
    if os.name == "nt":
        return swept
    _sweep_stale_root_markers()
    for port, markers in _orphan_port_owners():
        try:
            out = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            continue
        for line in out.split():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            try:
                cmd = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "command="],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
            except Exception:
                cmd = ""
            if not any(m in cmd for m in markers):
                print(f"[sweep] port {port}: pid {pid} 身份不符（{cmd[:80] or '未知'}），不动", file=sys.stderr)
                continue
            # 来源鉴定（2026-09-19 provenance 完整版，评审 A-P2）：stamp 读得出
            # 「拉起树」且 ≠ 本树 → 他会话/他树的 bok 栈，永不收割——它可能正在
            # 加载模型（跨树互杀同款根因），多会话纪律也禁碰他树进程；down 同样
            # 不收他树（down=停本树+无戳遗留）。stamp 读不出按来源未知走下面
            # 原健康闸/收割语义，旧行为兜底不变。
            proc_root = _process_serve_root(pid)
            if proc_root and os.path.realpath(proc_root) != os.path.realpath(str(paths.ROOT)):
                print(
                    f"[sweep] port {port}: pid {pid} 属另一代码树（{proc_root}）——不动"
                    "（他树进程永不收割；要切换先在对方 down）",
                    file=sys.stderr,
                )
                continue
            # 健康即非孤儿（2026-09-19 互杀事故）：动手前放宽超时（5s）复检一次，
            # 活的放行——CPU 风暴下上一轮 serve 探测假死退出留下的健康子代，
            # 不能在这里被当孤儿误杀。healthy_ok=False（cmd_down 专用）跳过该闸：
            # down 的拆除契约要求连「pidfile 够不着但健康」的残留一并收掉——
            # pidfile 被覆写成死 pid 时这是唯一回收路径（评审 P1-1）。
            if healthy_ok and core._relaxed_healthy(port):
                print(
                    f"[sweep] port {port}: pid {pid} healthy — left alone"
                    "（serve 将复用该进程；多 worktree/旧代码疑虑先 down 再起）",
                    file=sys.stderr,
                )
                continue
            swept.append((port, cmd[:60], pid))
            if not kill:
                continue
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    continue
    return swept
