"""bokctl.commands.status —— `bok status`（G2 W③ 自 core 搬入）。

共享面（healthy/CORE_PORTS/_worker_ports/_probe_worker/_provider_health_
summary/_llm_raw_expected）仍住 bokctl.core，一律穿 ``core.X`` call-time 取
（patch 缝=模块属性）；本地 TTS 门控穿 ``servers.X``。"""
from __future__ import annotations

from bokctl import core, paths, servers


def cmd_status() -> int:
    print(f"app-data: {paths.app_data_dir()}")
    _tts_needed, _tts_why = servers._local_tts_needed()
    services = [("web", 3000), *core.CORE_PORTS]
    for name, port in services:
        if name == "tts" and not core.healthy(port) and not _tts_needed:
            # 全云端门控跳过的 :8788 不是故障——如实标 skipped，不骗 DOWN。
            print(f"  {name:<13} :{port:<6} skipped (cloud-only: {_tts_why})")
            continue
        if name == "llm-raw" and not core.healthy(port) and not core._llm_raw_expected():
            # queue proxy 关（或非 mac）=mlx 直跑 :1235，:1239 结构性缺席——
            # 设计态不是故障（同 tts cloud-only 先例），不骗 DOWN。
            print(f"  {name:<13} :{port:<6} skipped (queue proxy off: mlx direct on :1235)")
            continue
        print(f"  {name:<13} :{port:<6} {'UP' if core.healthy(port) else 'DOWN'}")
    # worker 三件(2026-09-12 上表;2026-09-17 起读真 /worker 端点):serve 竞态令
    # worker 静默缺失、或进程在而没 register 时,TCP UP 仍全绿——「看着正常其实
    # 通话全灭」。端点本体才见 agent_name/worker_load。
    for name, port in core._worker_ports():
        ok, detail = core._probe_worker(port, timeout=2.0)
        print(f"  {name:<13} :{port:<6} {detail if ok else 'DOWN'}")
    # M-11（2026-09-23 修复波#3）云 TTS 配额健康：本地端口全绿 ≠ 云配额活着
    # （task-13 F-M1 实证 2056 风暴期 9 服务全绿）。扫 worker 日志近窗打点。
    _ph = core._provider_health_summary()
    if _ph is None:
        print(f"  {'cloud-tts':<13}         n/a (provider health scanner unavailable)")
    elif _ph["degraded"]:
        q, rl = _ph["quota_2056"], _ph["rate_limit"]
        bits = []
        if q["count"]:
            bits.append(f"2056(配额死)x{q['count']} last={q['last_hit']}")
        if rl["count"]:
            bits.append(f"限流x{rl['count']} {rl['statuses']} last={rl['last_hit']}")
        print(f"  {'cloud-tts':<13}         DEGRADED ({'; '.join(bits)} — 云端 TTS 会劣化到垫话/watchdog 兜底)")
    else:
        print(f"  {'cloud-tts':<13}         ok (无 2056/限流打点于近 {_ph['window_s']:.0f}s)")
    return 0


def run(args) -> int:
    return cmd_status()
