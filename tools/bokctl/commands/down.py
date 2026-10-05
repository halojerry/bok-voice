"""bokctl.commands.down —— `bok down`（G2 W③ 自 core 搬入）。

pidfile 收割/孤儿清扫件全部住 bokctl.proc、路径锚住 bokctl.paths——搬运时
已穿模块对象调用，本模块原样保留（`proc.X`/`paths.X` call-time 取）。"""
from __future__ import annotations

import os
import signal
import sys

from bokctl import paths, proc


def cmd_down() -> int:
    run_dir = paths.app_data_dir() / "run"
    stop_failures = 0
    for pidfile in run_dir.glob("*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
        except Exception:
            continue
        # 他树戳守卫（2026-09-22）：run/*.pid 是 HOME 作用域单槽共享文件，
        # 多会话/worker 换装会互相覆写——down 只停「本树 + 无戳遗留」（_sweep_
        # orphan_listeners 已立法的同款纪律在 pidfile 路径落地；旧版此处对
        # pidfile 内容无脑收割，2026-09-22 实弹把我们树的 worker 杀掉的正是
        # 这个缺口）。
        foreign, root = proc._pid_origin_foreign(pid)
        if foreign:
            print(f"[down] skip {pidfile.stem}: pid {pid} 属另一代码树（{root}）"
                  "——他树进程永不收割（先在对方树 down）", file=sys.stderr)
            continue
        # pid 复用闸（2026-10-02 审计）：来源戳 lstart 与 live 进程对不上 =
        # stale pidfile 的 pid 已被无关进程复用——来源判「未知」会 fail-open
        # 照杀（误杀无辜）；此处按铁证跳过。无戳遗留照旧语义杀。
        if proc._pid_reused_stale(pidfile, pid):
            print(f"[bok] stale pidfile {pidfile.stem} pid={pid} reused — skip kill")
            continue
        # _start_proc 以 start_new_session=True 启动（会话组长）；按进程组
        # 终止可连 livekit-agents worker 的 multiprocessing 子进程一起清掉，
        # 避免子进程残留占用 8081 导致下次 agent 启动失败。
        # Windows(M2)：taskkill /T /F 沿父子树收割；真失败必须浮出——旧代码
        # os.killpg 在 nt 不存在，AttributeError 被外层 except 整个吞掉，down
        # 全程静默失效（probe_windows_lifecycle.py 记录的 M1 断层）。
        try:
            proc._kill_proc_tree(pid)
        except proc._KillTreeError as exc:
            print(f"[down] FAILED to stop {pidfile.stem}: {exc}", file=sys.stderr)
            stop_failures += 1
            continue
        except Exception:
            # 死 pid / 权限缺失：与旧 POSIX 行为一致，静默跳过。
            continue
        print(f"[down] stopped {pidfile.stem} (pid {pid})")
    # Legacy dev sidecars managed by old start_sidecars.sh (host pids in data/).
    data_dir = paths.ROOT / "data"
    for pidfile in data_dir.glob("sidecar-*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
            # pid 复用闸同款（2026-10-02 审计）：legacy 扫描面同样只杀真本树 pid。
            if proc._pid_reused_stale(pidfile, pid):
                print(f"[bok] stale pidfile {pidfile.stem} pid={pid} reused — skip kill")
                continue
            os.kill(pid, signal.SIGTERM)
            print(f"[down] stopped {pidfile.stem} (pid {pid})")
        except Exception:
            continue
    # 孤儿 worker 兜底清扫（2026-09-07 QA 实证）:serve 异常退出后 start_new_session
    # 的 worker 存活,而 pidfile 可能已被覆写成死 pid——down 按 pidfile 清不到,
    # 旧代码 worker 会继续注册 livekit 抢 job。按进程特征+监听端口兜底清扫一遍。
    orphans = proc._sweep_orphan_workers()
    for pid, label in orphans:
        print(f"[down] swept orphan worker (pid {pid}, {label})")
    # 端口级兜底（2026-09-17 殭尸专项）:spawn 子代/sidecar/livekit/uvicorn 殘留
    # 是命令行特征清扫的盲区,按 bok 端口表+身份复核双条件收割。
    # healthy_ok=False:down 是拆除语义,pidfile SIGTERM 已先送达,「健康残留」
    # 也必须收走——否则 pidfile 覆写成死 pid 时 down 返回 0 但栈仍在跑
    # （旧代码被静默采纳=A/B 污染复活,评审 P1-1）。
    for port, cmd, pid in proc._sweep_orphan_listeners(healthy_ok=False):
        print(f"[down] swept orphan listener :{port} (pid {pid}, {cmd})")
    return 1 if stop_failures else 0


def run(args) -> int:
    return cmd_down()
