"""bokctl.commands.monitor —— `bok monitor`（G2 W③ 自 core 搬入）。

装配/spawn 件（_worker_specs/_start_proc）穿 ``servers.X``、进程清扫件穿
``proc.X``、健康探针（healthy/_probe_worker/_agent_worker_port）穿
``core.X``——全部 call-time 属性取用（patch 缝=模块属性）。"""
from __future__ import annotations

import os
import time

from bokctl import core, paths, proc, servers


def cmd_monitor() -> int:
    """C6-1 常驻监控环:LiveKit 重启→A+B 全 worker respawn(重注册);单 worker
    掉线→补拉。9/12 11:52-12:05 实证:livekit 重启后 worker 注册全丢,
    「no worker is available」连 4 通 0 轮、无人补拉;serve 一次性返回管唔到。
    2026-09-17 重排:探不上→respawn 改连续失败计数(单轮 1s TCP 探测在 GPU
    满载下係常态误报)。
    2026-09-25 G3 重排(LANE-AB-2026-09-25.md 附3):①探活从 1s TCP 换成真
    GET :port/worker 端点(_probe_worker 与 prod status 同源单点)——TCP UP 对
    「进程在、没 register/假活」不可见,1s 窗在 swap 颠簸下还假死(offscript
    窗误杀 ×7 根因);②active_calls>0 时**任何探活失败都不杀**(硬 veto,取代
    12 轮/60s 抬门槛——swap 颠簸可连吃 60s,门槛抬得再高也有窗,veto 先生才
    关死);CP 不可达退回无通话口径。
    """
    py = paths.repo_python()
    run_dir = paths.app_data_dir() / "run"
    log_dir = paths.app_data_dir() / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    # 盲斑 1 修复（2026-09-22）：外部手跑 monitor 也落 pidfile+来源戳——旧版只
    # 有 _start_proc 拉起的 monitor 才有痕迹，外部启动令 _ensure_monitor 探不到
    # 单例（双监控环）、跨树杀守卫对无戳进程不保护。经 _start_proc 拉起时同
    # 内容写两次，幂等无害。
    proc._write_proc_stamps(run_dir / "monitor.pid", os.getpid())
    print(f"[monitor] started — watching :7880 + workers {core._agent_worker_port()}/8082/8083")
    lk_up = core.healthy(7880)
    last_action = 0.0
    down_streak: dict[str, int] = {}

    def _respawn(specs: list[dict], why: str) -> None:
        nonlocal last_action
        # 限频:转换风暴(重启抖动)下 30s 内只动作一次。
        if time.monotonic() - last_action < 30.0:
            return
        last_action = time.monotonic()
        print(f"[monitor] {why} — respawning workers")
        for spec in specs:
            proc._kill_pidfile(spec["pidfile"])
        # 等端口释放(优雅关停最长 ~10s;拿不到就交给端口单例守卫兜底)。
        _deadline = time.monotonic() + 10.0
        while time.monotonic() < _deadline:
            if not any(core.healthy(s["port"]) for s in specs):
                break
            time.sleep(0.5)
        for spec in specs:
            # 他树/复用守卫（2026-09-22）：上一轮 kill 被他树戳挡下时，端口仍被
            # 对方的健康 worker 持有——硬起只会 bind 失败退出刷噪声。已有健康
            # 监听的端口跳过重拉（自己刚被杀掉的 worker 端口是空的，不受影响）。
            if core.healthy(spec["port"]):
                print(f"[monitor] :{spec['port']} 已有健康监听，跳过重拉"
                      "（他树持有则去对方树 down）")
                continue
            servers._start_proc(spec["argv"], spec["pidfile"], spec["logfile"], env=spec["env"])
            print(f"[monitor] respawned {spec['name']} :{spec['port']}")

    while True:
        try:
            specs = servers._worker_specs(py)
            now_up = core.healthy(7880)
            skip_lk_mark = False
            if not lk_up and now_up:
                # LiveKit 回来了(重启)——注册在新进程,worker 必须重注册。但若有在途
                # 通话（或 CP 不可达=状态未知），respawn 的集体 kill 会陪葬活通话
                # （2026-09-27 修：旧版无条件 kill）：保守不杀，且**不更新 lk_up**
                # → 5s 后本轮重试，通话归零即刻补拉（重启注册丢的修复只是延后不丢）。
                _active = proc._cp_active_calls()
                if _active is None or _active > 0:
                    skip_lk_mark = True
                    print(
                        f"[monitor] livekit back up but active_calls="
                        f"{'unknown(CP unreachable)' if _active is None else _active} "
                        "— veto respawn (retry after calls drain)"
                    )
                else:
                    _respawn(specs, "livekit back up (restart detected)")
                    down_streak = {}
            elif now_up:
                active = proc._cp_active_calls()
                need = proc._DOWN_STREAK_NEED_ACTIVE if active else proc._DOWN_STREAK_NEED_IDLE
                down: list[dict] = []
                for spec in specs:
                    # 真端点探针:与 prod status/_probe_worker 同源单点(见 docstring G3①)。
                    ok, _detail = core._probe_worker(spec["port"])
                    if ok:
                        down_streak[spec["name"]] = 0
                        continue
                    streak = down_streak.get(spec["name"], 0) + 1
                    down_streak[spec["name"]] = streak
                    kill, veto_log = proc._monitor_kill_round(streak, active)
                    if veto_log:
                        _desc = "unknown(CP unreachable)" if active is None else active
                        print(
                            f"[monitor] worker {spec['name']} probe failed x{streak} "
                            f"but active_calls={_desc}, veto kill"
                        )
                    if kill:
                        down.append(spec)
                if down and time.monotonic() - last_action >= 30.0:
                    # 单 worker 真 down 补拉;全 down 逐个 kill+start(端口已死,
                    # 无需 _respawn 的集体 kill-then-wait)。有通话在途时硬 veto
                    # (G3②):kill 恒 False、streak 照涨——通话一结束(active 归零)
                    # 真死 worker 立刻补拉,活 worker 的瞬态卡顿永不触发。
                    last_action = time.monotonic()
                    for spec in down:
                        down_streak[spec["name"]] = 0
                        proc._kill_pidfile(spec["pidfile"])
                        servers._start_proc(spec["argv"], spec["pidfile"], spec["logfile"], env=spec["env"])
                        print(
                            f"[monitor] worker {spec['name']} down x{need} "
                            f"(active_calls={active}) — respawned :{spec['port']}"
                        )
            if not skip_lk_mark:
                lk_up = now_up
        except Exception as exc:  # noqa: BLE001 - 监控环任何异常都唔准退出
            print(f"[monitor] loop error: {exc!r} — keep watching")
        time.sleep(5.0)


def run(args) -> int:
    return cmd_monitor()
