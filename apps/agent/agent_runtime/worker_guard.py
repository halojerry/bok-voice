"""Worker 端口单例守卫(C6-2,2026-09-13)。

9/12 18:27 实证:重复 spawn 撞显式端口(8081/8082/8083)时后绑者 OSError
Errno 48 即崩——崩溃轨迹(worker failed + traceback + 重启 churn 噪音)掩盖
了真正的事实:已有一个健康 worker 在服务。守卫把这类竞态从「崩溃+噪音」
变成「检测到已有实例,良性退出 0」——无论谁双拉(serve 竞态/monitor/手起),
都不再产生假故障信号。kill 旧实例请 bok.py down。

探测用 connect 而非 bind:bind 探测在 SO_REUSEADDR 下会假阴性,connect 成功
= 确有监听者。
"""

from __future__ import annotations

import socket


def port_occupied(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def worker_port_singleton_guard(port: int, name: str) -> None:
    """端口已被监听 → 打点 WORKER_ALREADY_RUNNING 并 SystemExit(0)。

    在 cli.run_app 之前调用(livekit bind 前);正常路径零开销一次 connect。
    BOK_WORKER_PORT_GUARD=0 关(诊断用:强制双实例复现 Errno 48 现场时)。
    """
    import os
    import sys

    if os.environ.get("BOK_WORKER_PORT_GUARD", "1") != "1":
        return
    try:
        occupied = port_occupied(port)
    except OSError:
        return
    if occupied:
        print(
            f"[worker] {name} port {port} already in use — WORKER_ALREADY_RUNNING, "
            "exiting 0 (已有健康实例在服务;要换实例请先 bok.py down)",
            flush=True,
        )
        raise SystemExit(0)
