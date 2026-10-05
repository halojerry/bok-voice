"""bokctl.commands.serve —— `bok serve`（G2 W③ 自 bokctl.servers 搬入）。

服务面拉起家族（_start_proc/_local_tts_needed/_realtime_demo_enabled/
_repo_web_modules/_cmd_up_services/_start_call_plane）仍住 bokctl.servers、
进程清扫穿 ``proc.X``、健康等待穿 ``health.X``、共享锚（healthy/
_cp_bind_host/_desktop_stack_targets）穿 ``core.X``——全部 call-time 属性
取用（patch 缝=模块属性）；通话面拉起经 ``commands.up.cmd_up()``（owner
随 W③ 搬入 commands.up）。"""
from __future__ import annotations

import os
import sys

from bokctl import commands, core, env, health, paths, proc, servers


def cmd_serve() -> int:
    """Bring up the full no-Docker desktop stack and wait until ready.

    Packaged mode (BOK_PACKAGED=1) serves the UI from the Tauri static bundle,
    so the Next server on :3000 is NOT started. All local services bind
    127.0.0.1 (CP honors BOK_BIND_HOST, default 127.0.0.1). Business data goes
    to SQLite and the knowledge vault lives in app-data (never the read-only
    bundle).
    """
    run_dir = paths.app_data_dir() / "run"
    log_dir = paths.app_data_dir() / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 起栈前端口预清（2026-09-17 殭尸专项 → 2026-09-19 健康闸+来源鉴定）：身份
    # 复核不过的一律不动；他树 stamp 的 bok 栈永不收割（跨树互杀/多会话纪律）；
    # 本树与无戳的再过健康闸——「健康」放行（防 CPU 风暴把加载中子代当孤儿误
    # 杀的互杀循环）。残留面：同树「健康但旧代码」的进程无法从外部判定所载代
    # 码版本，会被 spawn 门复用（A/B 污染面）——清扫日志逐口提示 left alone，
    # 改完代码要吃新代码先 down 再起。
    stale = proc._sweep_orphan_listeners()
    for port, cmd, pid in stale:
        print(f"[serve] swept stale listener :{port} (pid {pid}, {cmd})")

    py = paths.repo_python()
    # Dev 模式用系统 node 起 Next dev（打包模式 BOK_PACKAGED=1 跳过 web:3000）。
    node = paths.bundled_node() or "node"
    # control-plane
    # Dev 与打包统一：业务数据 SQLite 落盘、知识 vault 在 app-data（bundle 只读）。
    db = (paths.app_data_dir() / "bok_voice.db").as_posix()
    cp_env: dict[str, str] = env._control_plane_env(db)
    if not core.healthy(8000):
        servers._start_proc(
            [str(py), "-m", "uvicorn", "control_plane.main:app", "--host", core._cp_bind_host(), "--port", "8000"],
            run_dir / "control-plane.pid",
            log_dir / "control-plane.log",
            env=cp_env,
        )
    # Dev mode: Next dev server on :3000 (packaged serves static UI from Tauri).
    # next.config.mjs 是 output:"export"，`next start` 无法服务 export 产物，
    # 必须用 `next dev`（export 只在 build 阶段生效）。
    if not paths.is_packaged() and not core.healthy(3000):
        servers._start_proc(
            [
                str(node),
                str(servers._repo_web_modules() / "next" / "dist" / "bin" / "next"),
                "dev",
                "-H",
                "127.0.0.1",
                "-p",
                "3000",
            ],
            run_dir / "web.pid",
            log_dir / "web.log",
            env={"NEXT_PUBLIC_CONTROL_PLANE_URL": os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8000")},
            cwd=str(paths.ROOT / "apps" / "web"),
        )

    # 通话面（LiveKit + agent worker + 常驻监控）由 cmd_up→_start_call_plane 单点
    # 拉起（2026-09-20 提取，serve 与 node_agent 全栈同源）；本函数只做就绪等待。
    rc = commands.up.cmd_up()
    if rc:
        return rc

    print("[bok] waiting for desktop stack…")
    desktop_tts_needed = servers._local_tts_needed()[0]
    # 基础口表吃 _desktop_stack_targets()（agent worker 口=BOK_WORKER_PORT 动态；
    # 2026-10-02 审计——旧版硬编码 8081，错开档就绪等待永远打缺省口）。
    targets = core._desktop_stack_targets()
    if desktop_tts_needed:
        targets.insert(2, 8788)
    if servers._realtime_demo_enabled():
        # 演示档 worker 随栈拉起时纳入就绪等待（opt-in，:8084）。
        targets.append(8084)
    if core.healthy(1236):
        # MT 翻译小模型(:1236)可选:cmd_up 拉起了才纳入等待,缺模型不算失败。
        targets.append(1236)
    if not paths.is_packaged():
        targets.append(3000)
    # 就绪判据（2026-10-02 readiness 真话）：1235（/v1/models）/8787/8788
    # （/health）必须 HTTP 200——mlx 先绑端口后装权重、sidecar 模型装载中
    # 503，TCP 通≠能干活；这些口的宽松终检同款（见 _serve_ready_probe*）。
    if health._wait_desktop_ready(targets):
        _desktop_tts = "tts=8788" if desktop_tts_needed else "tts=skipped(cloud-only)"
        ready = f"[bok] desktop ready: control-plane=8000 asr=8787 {_desktop_tts} llm=1235"
        if 1236 in targets:
            ready += " mt=1236"
        print(ready)
        # 非打包模式自动打开浏览器页面(可用 BOK_NO_OPEN_BROWSER=1 关闭)。
        if not paths.is_packaged() and os.environ.get("BOK_NO_OPEN_BROWSER", "0") != "1":
            try:
                import webbrowser
                webbrowser.open("http://127.0.0.1:3000")
            except Exception:  # pragma: no cover - 打开浏览器失败不影响启动
                pass
        return 0
    # 宽松终检（2026-09-19 互杀事故收编）：CPU 风暴下 1s 探测可整轮假死，
    # 120s 走完≠栈真死——逐口 5s 复检再宣判；serve 在这里退出会把健康子代
    # 留给下一轮 serve 的孤儿清扫误杀（互杀循环根因），能不退就不退。
    # 严格口（1235/8787/8788）的复检维持 HTTP-200 真话（still_down 点名如实）。
    still_down = health._ports_down_after_grace(targets, probe=health._serve_ready_probe_relaxed)
    if not still_down:
        _desktop_tts2 = "tts=8788" if desktop_tts_needed else "tts=skipped(cloud-only)"
        print(f"[bok] desktop ready (relaxed recheck): control-plane=8000 asr=8787 {_desktop_tts2} llm=1235")
        return 0
    print(f"[bok] timeout waiting for desktop stack — still down: {still_down} (see app-data/logs)", file=sys.stderr)
    return 1


def run(args) -> int:
    return cmd_serve()
