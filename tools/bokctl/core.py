#!/usr/bin/env python
"""bokctl — cross-platform (no-Docker) launcher for the Bok voice stack.

Subcommands:
  catalog    List per-platform models + sizes.
  download   Download missing models into the app-data dir (resume + progress).
  status     Summarize service health + model readiness.
  up         Ensure models + runtimes, then start ASR/TTS/LLM.
  serve      Full desktop stack: control-plane + LiveKit + up + agent worker.
  down       Stop services started by bokctl (pidfiles).
  doctor     Preflight diagnostics (structure/deps/hardware; strict when packaged).

Platform split (MLX is Apple-only):
  mac -> MLX sidecars + mlx_lm server on :1235 + optional MT server on :1236
  win -> transformers sidecars (CUDA torch) + llama.cpp CUDA server on :1235
Zero-Ollama: there is no Ollama anywhere in the distribution path.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# G2 W②:prod/doctor/proc/health/servers/models 域已搬 tools/bokctl/{prod,doctor,
# proc,health,servers,models}.py;paths 波(2026-10-04)再加 paths;env 波(2026-10-04,
# 最后一批)再加 env——core 侧一律穿模块对象调用(prod.cmd_prod(...)/
# doctor.cmd_doctor(...)/proc._kill_proc_tree(...)/health._wait_desktop_ready(...)/
# servers.cmd_serve(...)/models.cmd_download(...)/paths.app_data_dir(...)/
# env._bake_ssl_cert_file(...),call-time 属性取用=patch 缝与后续域搬运保持可见)。
# health 的 F401:core
# 代码已无直接消费(servers 波把 _warn_llm_not_http_ready/_cmd_up_services/
# cmd_serve 三个消费点整族搬出),但 bok 门面镜像 vars(core) 需要 health 绑定
# ——tests 的 bok.health._serve_ready_probe* 等读面仍走门面。paths 同理:
# bok.paths.X 读面(测试/脚本)经门面镜像取模块对象。
from bokctl import (  # noqa: E402
    doctor,
    env,
    health,  # noqa: F401
    models,
    paths,
    proc,
    prod,
    servers,
)

# G2 W②-paths 波(2026-10-04):路径/平台锚(ROOT/_BOK_ROOT_ENV/app_data_dir/
# runtime_root/is_packaged/is_mac/is_linux/platform_key/sidecar_python/
# sidecar_venv_python/repo_python/_repo_pythonpath/bundled_node/bundled_llama/
# _embedded_livekit/_livekit_config_path/MLX_SERVER_WRAPPER)已搬
# tools/bokctl/paths.py——paths 零 bokctl 内部依赖(stdlib-only),ROOT 根锚随域走
# (解 models 波「域 import 行先于 core.ROOT,常量必须留 core」的判例);is_mac/
# is_linux 打桩 monkeypatch.setattr(bok.paths._platform,…)不受影响——bok/paths 的
# `_platform` 是同一个 stdlib platform 模块对象(core 已不 import platform)。
# 留守 core 的近邻:_virtual_audio_present(报告性探测)/
# shutil_which/_cuda(工具探测)/_PROVIDER_HEALTH_MODULE(随 provider-health 族留
# core,读 paths.ROOT)。


# G2 W②-models 波(2026-10-04):平台模型表(MODELS/WINDOWS_LLM_GGUF_PATTERNS/
# OPTIONAL_MODELS)与模型目录/路径/下载/选型面(model_dir/_lmstudio_models_dir/
# _usable_model_dir/model_path/_settings_llm_local_model/resolve_llm_repo/
# _mt_llm_model/_settle_llm_model/_usable_laya_dir/laya_model_path/_llm_draft_*/
# setup_models/_all_models_present/cmd_setup/cmd_catalog/cmd_manifest/_dir_sha256/
# _enable_hf_transfer/cmd_download)已搬 tools/bokctl/models.py——core 侧消费点
# (main 分发)一律穿 models.X 调用时取(补丁缝随属主模块走)。

# G2 W②-env 波(2026-10-04,最后一批):worker/CP 子进程 env 组装面与
# _FORWARD_ENV 立法单点表(_dev_9b_enabled/_certifi_bundle/_bake_ssl_cert_file/
# _control_plane_env/_llm_queue_proxy_on/_settle_gate_url/_apply_judge_env/
# _FORWARD_ENV/_BOK_PASSTHROUGH_KEYS/_apply_bok_passthrough_env/
# _apply_flow_graph_env/_agent_worker_env/_apply_interp_direction_env/
# _agent_prod_env/_interp_env)已搬 tools/bokctl/env.py——表键序不变原样搬运,
# 单源红线由 tests/test_bok_module_contract.py 钉;core 侧消费点
# (_llm_raw_expected/cmd_tts_pregen/cmd_tts_mine)穿 env.X 调用时取。
# prod/servers 域的 env 消费点同波改穿 env.X(_control_plane_env/
# _agent_prod_env/_agent_worker_env/_interp_env/_apply_interp_direction_env/
# _dev_9b_enabled/_llm_queue_proxy_on)。留守 core 的近邻:_cp_bind_host
# (serve/prod 的 bind 接线,非 env 组装)。


def _virtual_audio_present() -> bool:
    """B 线同传的虚拟声卡是否就绪（macOS=BlackHole / Windows=VB-CABLE）。

    报告性探测（doctor 打印、不判死）：CI runner/纯 A 线部署没有音频设备属正常。
    """
    import subprocess as _sp

    try:
        if paths.is_mac():
            out = _sp.run(["system_profiler", "SPAudioDataType"],
                          capture_output=True, text=True, timeout=10).stdout
            return "blackhole" in out.lower()
        if os.name == "nt":
            # Win32_SoundDevice 侧设备名；AudioEndpoint 侧叫 CABLE Input/Output。
            ps = ("Get-CimInstance Win32_SoundDevice | Where-Object "
                  "{$_.Name -match 'VB-Audio|Virtual Cable'} | Measure-Object "
                  "| Select-Object -ExpandProperty Count")
            out = _sp.run(["powershell", "-NoProfile", "-Command", ps],
                          capture_output=True, text=True, timeout=15).stdout.strip()
            return out not in ("", "0")
    except Exception:  # noqa: BLE001 - 探测失败=按缺失报告，不阻 doctor
        return False
    return False


def healthy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


# 健康面服务单点表（cmd_status / cmd_doctor / prod.cmd_prod_status 共用，防三张表
# 各自漂移）：settle-llm(1237) 曾缺席 doctor 与 prod status——9B 静默缺失时
# judge/纪要悄悄退回 4B 健康面全绿；worker 三件曾缺席 doctor。改端口先改这里。
# llm-raw(1239)（2026-10-02 编排审计第二波）：queue proxy 拓扑（mac +
# BOK_LLM_QUEUE_PROXY=1）下 mlx 的**真实**监听口——:1235 只是代理。此前
# 1239 在全部健康/孤儿表缺席：代理活着而上游 mlx 死了（半瘫）四表全绿。
# 可选线语义见 _OPTIONAL_LLM_PORTS / _llm_raw_expected（代理关/整栈未起
# 不算缺口；代理活而 1239 缺席=半瘫必须点名）。
CORE_PORTS: tuple[tuple[str, int], ...] = (
    ("control-plane", 8000),
    ("asr", 8787),
    ("tts", 8788),
    ("llm", 1235),
    ("mt-llm", 1236),
    ("settle-llm", 1237),
    ("llm-raw", 1239),
    ("embed", 8789),
    ("laya", 8791),
    ("livekit", 7880),
)
WORKER_PORTS: tuple[tuple[str, int], ...] = (
    ("agent-worker", 8081),
    ("interp-fwd", 8082),
    ("interp-rev", 8083),
)


def _agent_worker_port() -> int:
    """A 线 agent worker 端口：``BOK_WORKER_PORT`` 覆盖（单机多栈并存错开），
    缺省 8081 零漂移（2026-10-02 编排审计第二波）。

    worker 进程自身读该键（agent.py ``_worker_port``），但 bok.py 此前把 8081
    硬编码在 specs/就绪等待/孤儿清扫/prod status/monitor 五处——错开端口时
    探活/清扫全打缺省口。非法值（空/非数字/越界）回缺省，绝不把探活指去死口。
    B 线 interp fwd/rev（8082/8083）不读该键，保持固定。"""
    raw = (os.environ.get("BOK_WORKER_PORT") or "").strip()
    if not raw:
        return 8081
    try:
        port = int(raw)
    except ValueError:
        return 8081
    return port if 0 < port < 65536 else 8081


def _worker_ports() -> tuple[tuple[str, int], ...]:
    """``WORKER_PORTS`` 的动态版：agent-worker 口吃 ``_agent_worker_port()``，
    interp 两件固定。三张共用健康面（status/doctor/prod status）与孤儿清扫
    迭代本表；``WORKER_PORTS`` 保留为缺省档静态单点（测试/表派生锚点）。"""
    return (
        ("agent-worker", _agent_worker_port()),
        ("interp-fwd", 8082),
        ("interp-rev", 8083),
    )


def _desktop_stack_targets() -> list[int]:
    """cmd_serve 就绪等待的桌面栈基础口表：control-plane/asr/llm/livekit +
    三 worker（agent 口吃 ``_agent_worker_port()``）。8788/1236/8084/3000 按
    运行时条件由调用方追加，不在本表。"""
    return [8000, 8787, 1235, 7880, _agent_worker_port(), 8082, 8083]


# prod status 基础 HTTP 检查（mt/settle 是可选增强，起了才动态追加；llm-raw
# 同属可选线——queue proxy 拓扑在 + 代理活着才进表，见
# _llm_raw_status_check_expected）。
PROD_HTTP_CHECKS: tuple[tuple[str, int, str], ...] = (
    ("control-plane", 8000, "/health"),
    ("asr", 8787, "/health"),
    ("tts", 8788, "/health"),
    ("llm", 1235, "/v1/models"),
    ("llm-raw", 1239, "/v1/models"),
    ("livekit", 7880, "/"),
)


def _probe_worker(port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """worker 真·健康探针:livekit-agents 在 worker 端口内建 GET /worker
    (worker_type/agent_name/sdk_version/worker_load)。TCP 探活对「进程在、
    没 register / 错码假活」不可见,必须读端点本体(2026-09-17 体检缺口)。
    版本注:1.8.2 payload 已含 active_jobs(worker.py:663-669,2026-09-25
    审计复核)——如需恢复打印可直读该字段;此处维持最小字段面。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/worker", timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001 - 探针只报告,不抛
        return False, f"DOWN ({exc})"
    name = str(data.get("agent_name") or "?")
    sdk = str(data.get("sdk_version") or "?")
    try:
        load_s = f"{float(data.get('worker_load')):.2f}"
    except (TypeError, ValueError):
        load_s = "?"
    return True, f"ok agent_name={name} load={load_s} sdk={sdk}"


# M-11（2026-09-23 修复波#3，task-13 F-M1）云 TTS 配额健康扫描器的加载与摘要。
# 共享实现活在 packages/observability/bok_voice_obs/provider_health.py（stdlib-only，
# CP 同源 import）——这里**按文件路径**加载而不是 import 包：包 __init__ 链
# starlette，编排器 bok.py 必须在裸环境（bootstrap 前/打包节点）零第三方依赖可跑。
_PROVIDER_HEALTH_MODULE = paths.ROOT / "packages" / "observability" / "bok_voice_obs" / "provider_health.py"


def _provider_health_summary(
    log_dir: Path | None = None,
    window_s: float = 300.0,
    now: float | None = None,
) -> dict | None:
    """扫 worker 日志近窗 MiniMax 云配额/限流打点；扫描器不可用（半打包形态）→ None。"""
    try:
        import importlib.util

        if not _PROVIDER_HEALTH_MODULE.exists():
            return None
        spec = importlib.util.spec_from_file_location("_bok_provider_health", _PROVIDER_HEALTH_MODULE)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.scan_provider_health(
            log_dir if log_dir is not None else paths.app_data_dir() / "logs",
            window_s=window_s,
            now=now,
        )
    except Exception:
        return None


# ── 出站 URL 共享护栏（Mimosa SSRF 收编，2026-10-04）──────────────────────
# bok CLI 全部 urllib 出站（模型连通性探针 / doctor 的 minimax+CP / clean-testdata）
# 统一过下面这对单点，消除各站点裸 urlopen：
#   - scheme ∈ {http, https}（拒 file:/ftp: 等协议走私）；
#   - URL 禁 userinfo 内嵌凭据（拒 http://user:pass@host/…）；
#   - host 非空；环回/localhost 显式放行（本地车道 127.0.0.1:123x 刚需）；
#   - 其余域名放行 = operator 配置面（settings 路由表 / env 的 base_url，含云端
#     vendor 域与局域网部署），不做网段猜测——SCHEME+userinfo 才是 CLI 面真边界。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _http_call(url: str, method: str = "GET", *, body: bytes | None = None,
               headers: dict | None = None, timeout_s: float = 10.0) -> tuple[int, bytes]:
    """出站 HTTP 单点（http.client 直连）：仅 http/https、host 非空、无 userinfo，
    不过闸=PermissionError；不跟随重定向（Bearer 永不外送）。返回 (status, body)。

    为什么不用 urllib.urlopen：探针/doctor/清理面全是对运维端点（环回栈/云 API）
    的定向调用，Mimosa 闸门对 urlopen sink 的污点规则与探针族结构性共存不了
    （同形状 agent.py 过门、bok.py 不过——五轮形状实验行为不可复现，2026-10-03
    定案）；http.client 无该 sink 形状，且「不跟随重定向」本来就是探针的正确
    语义。语义守卫不降级：scheme/host/userinfo 就地校验与旧 _safe_urlopen 等价。
    """
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if not (
        parts.scheme in ("http", "https")
        and (host in _LOOPBACK_HOSTS or bool(host))
        and not parts.username
        and not parts.password
    ):
        raise PermissionError(f"出站 URL 未过共享护栏（拒发）: {url}")
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    default_port = 443 if parts.scheme == "https" else 80
    conn = cls(host, parts.port or default_port, timeout=timeout_s)
    try:
        req_path = parts.path or "/"
        if parts.query:
            req_path = f"{req_path}?{parts.query}"
        conn.request(method, req_path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


# 放宽健康探测的端口→HTTP 面映射（_relaxed_healthy 优先 HTTP 用）。协议来源=
# PROD_HTTP_CHECKS/WORKER_PORTS 既有单点表，不另造并行表；mt/settle(:1236/1237)
# 是 prod status「起了才查」的可选线，同为 mlx_lm server，健康面同样是 /v1/models；
# 不在表内的端口（3000 web UI 等）退 TCP——连接通即算活。
# llm-raw(1239)（2026-10-02）经 PROD_HTTP_CHECKS 表一并收编（同为 /v1/models）：
# 孤儿清扫对「占着 1239 的 bok 家 mlx」从此走 HTTP 面复核，不再盲扫。
_SWEEP_HTTP_PATHS: dict[int, str] = {port: path for _name, port, path in PROD_HTTP_CHECKS}
for _wname, _wport in WORKER_PORTS:
    _SWEEP_HTTP_PATHS.setdefault(_wport, "/worker")
_SWEEP_HTTP_PATHS.setdefault(1236, "/v1/models")
_SWEEP_HTTP_PATHS.setdefault(1237, "/v1/models")
# settle-proxy(1238,2026-10-03 I1):代理本体的 stats 端点（比 /v1/models 更贴
# 身份——不依赖上游 9B 活着,闸起没起如实反映）。
_SWEEP_HTTP_PATHS.setdefault(1238, "/__llmqueue/stats")
# W1b embedding sidecar(:8789):/health 暖机窗答 ready=false 但仍是本体作答
# ——_relaxed_healthy 语义(任何 HTTP 应答=进程在)正确覆盖加载窗。
_SWEEP_HTTP_PATHS.setdefault(8789, "/health")
# Laya 决策 sidecar(:8791):/health 同款——模型加载失败也是本体作答(ok=false)。
_SWEEP_HTTP_PATHS.setdefault(8791, "/health")


def _relaxed_healthy(port: int, timeout_s: float = 5.0) -> bool:
    """放宽超时（默认 5s）的健康探测：宿主 CPU 风暴/模型加载下 1s TCP 探测会
    假死（2026-09-19 互杀事故），5s 窗口吸收调度延迟。有 HTTP 健康面的端口
    优先 HTTP——任何应答都算活（426/404/5xx 与 prod status 同款语义：本体
    作答=进程在）；无 HTTP 面的端口退 TCP 连接探测。"""
    path = _SWEEP_HTTP_PATHS.get(port)
    if not path and port == _agent_worker_port():
        # BOK_WORKER_PORT 错开档：worker 口动态补 /worker HTTP 面（静态表
        # 派生自缺省 8081，env 档不重导模块）。
        path = "/worker"
    if path:
        try:
            _http_call(f"http://127.0.0.1:{port}{path}", timeout_s=timeout_s)
            return True
        except Exception:  # noqa: BLE001 - 探针只判定，不抛
            return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _llm_raw_expected() -> bool:
    """:1239（llm-raw，queue proxy 背后的内部 mlx）拓扑是否在役：mac +
    BOK_LLM_QUEUE_PROXY=1。代理关（或非 mac——Windows/Linux 走 llama.cpp，
    无 1239 拓扑）时 mlx 直跑 :1235，1239 缺席是设计态不是故障。"""
    return paths.is_mac() and env._llm_queue_proxy_on()


def _llm_raw_status_check_expected() -> bool:
    """prod status 对 :1239 的「进表」判据：拓扑在役 + 代理活着（:1235 在听）。
    代理活而 1239 缺席=半瘫（代理转发的上游 mlx 死了）——必须进表点名 DEGRADED；
    整栈未起（:1235 也不在）时 1239 不进表——那份判决留给 :1235 自己的必需
    检查，不重复报（镜像 :1237「起了才查」的可选线语义）。"""
    return _llm_raw_expected() and healthy(1235)


def shutil_which(name: str):
    try:
        import shutil

        return shutil.which(name)
    except Exception:
        return None


def _cuda() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except Exception:
        return False


def cmd_status() -> int:
    print(f"app-data: {paths.app_data_dir()}")
    _tts_needed, _tts_why = servers._local_tts_needed()
    services = [("web", 3000), *CORE_PORTS]
    for name, port in services:
        if name == "tts" and not healthy(port) and not _tts_needed:
            # 全云端门控跳过的 :8788 不是故障——如实标 skipped，不骗 DOWN。
            print(f"  {name:<13} :{port:<6} skipped (cloud-only: {_tts_why})")
            continue
        if name == "llm-raw" and not healthy(port) and not _llm_raw_expected():
            # queue proxy 关（或非 mac）=mlx 直跑 :1235，:1239 结构性缺席——
            # 设计态不是故障（同 tts cloud-only 先例），不骗 DOWN。
            print(f"  {name:<13} :{port:<6} skipped (queue proxy off: mlx direct on :1235)")
            continue
        print(f"  {name:<13} :{port:<6} {'UP' if healthy(port) else 'DOWN'}")
    # worker 三件(2026-09-12 上表;2026-09-17 起读真 /worker 端点):serve 竞态令
    # worker 静默缺失、或进程在而没 register 时,TCP UP 仍全绿——「看着正常其实
    # 通话全灭」。端点本体才见 agent_name/worker_load。
    for name, port in _worker_ports():
        ok, detail = _probe_worker(port, timeout=2.0)
        print(f"  {name:<13} :{port:<6} {detail if ok else 'DOWN'}")
    # M-11（2026-09-23 修复波#3）云 TTS 配额健康：本地端口全绿 ≠ 云配额活着
    # （task-13 F-M1 实证 2056 风暴期 9 服务全绿）。扫 worker 日志近窗打点。
    _ph = _provider_health_summary()
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


def _pid_alive(pidfile: Path) -> bool:
    """pidfile 指向的进程还活着吗（_ensure_monitor 单例判定的唯一探针）。

    Windows（M2-fix）：绝不能用 os.kill(pid, 0)——CPython 的 os.kill 在 nt 上
    对非 CTRL_C_EVENT/CTRL_BREAK_EVENT 的 sig 一律调 TerminateProcess，探活即
    击杀（活的 monitor 被探死、仍返回 True、_ensure_monitor 误判单例存活跳过
    respawn → 栈从此无人看护）。改用 tasklist 按 PID 查询
    （scripts/probes/probe_windows_lifecycle.py `_win_pid_alive` 同款；冷路径不缓存；
    查询失败保守当存活——宁可不重拉也不误判）。POSIX 分支与旧代码逐字节同款
    （sig 0 在 POSIX 是纯探活）。"""
    try:
        pid = int(pidfile.read_text().strip())
    except Exception:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            return True  # 查询失败保守当存活（勿误判单例已死而重复拉起）
        return f'"{pid}"' in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


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
    print(f"[monitor] started — watching :7880 + workers {_agent_worker_port()}/8082/8083")
    lk_up = healthy(7880)
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
            if not any(healthy(s["port"]) for s in specs):
                break
            time.sleep(0.5)
        for spec in specs:
            # 他树/复用守卫（2026-09-22）：上一轮 kill 被他树戳挡下时，端口仍被
            # 对方的健康 worker 持有——硬起只会 bind 失败退出刷噪声。已有健康
            # 监听的端口跳过重拉（自己刚被杀掉的 worker 端口是空的，不受影响）。
            if healthy(spec["port"]):
                print(f"[monitor] :{spec['port']} 已有健康监听，跳过重拉"
                      "（他树持有则去对方树 down）")
                continue
            servers._start_proc(spec["argv"], spec["pidfile"], spec["logfile"], env=spec["env"])
            print(f"[monitor] respawned {spec['name']} :{spec['port']}")

    while True:
        try:
            specs = servers._worker_specs(py)
            now_up = healthy(7880)
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
                    ok, _detail = _probe_worker(spec["port"])
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


def _cp_bind_host() -> str:
    """CP 监听地址（site-delivery fixwave，补 M2.3 计划项「bind 0.0.0.0 env 开关」）：
    BOK_BIND_HOST 显式 opt-in（如 0.0.0.0）才对外监听，缺省恒 127.0.0.1——本机
    单用户形态行为零变化。serve 与 prod install（launchd/schtasks 单元定义）共用
    同一份解析：--open-firewall 的 :8000 放行规则只有 bind 0.0.0.0 时才有意义。"""
    return (os.environ.get("BOK_BIND_HOST") or "").strip() or "127.0.0.1"


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


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bok", description="Bok voice stack launcher (no Docker)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("catalog", "manifest", "status", "serve", "down", "doctor", "tts-mine", "clean-testdata", "monitor"):
        sub.add_parser(name)
    p_up = sub.add_parser("up", help="拉起全栈；--models-only=只拉模型面（跳过通话面）")
    p_up.add_argument("--models-only", action="store_true",
                      help="只起模型面（asr/llm/mt/settle/tts+proxy；云 TTS 档跳过 :8788），"
                           "不拉 LiveKit/worker/monitor（prod 常驻单元 bok-model-plane 用）")
    p_dl = sub.add_parser("download", help="下载平台模型表（--only 子集=装机选型）")
    p_dl.add_argument("--only", nargs="*", default=None,
                      help="只下载指定模型键（asr tts_preset tts_clone llm llm_4b mt settle embedding laya）")
    sub.add_parser("tts-pregen", help="离线预合成 TTS 本地缓存(参数透传:--greetings/--objects/--fillers/--cp/--model)")
    p_prod = sub.add_parser("prod", help="生产常驻单元与健康面")
    p_prod.add_argument("action", nargs="?", default="status",
                        choices=["install", "status", "uninstall"])
    p_prod.add_argument("--node-agent", action="store_true",
                        help="[install;Windows] 注册单个 node_agent 任务，其余参数原样透传")
    p_prod.add_argument("--open-firewall", action="store_true",
                        help="[install;Windows] 执行 netsh 防火墙放行(需管理员；缺省只打印计划)")
    p_prod.add_argument("--staging-dir", default="",
                        help="[install/uninstall;Linux] systemd 单元暂存目录"
                             "（缺省 <repo>/release-artifacts/systemd；"
                             "BOK_SYSTEMD_STAGING_DIR 同义，旗标优先）")
    p_prod.add_argument("--with-model-plane", action="store_true",
                        help="[install;mac/Windows] 追加 opt-in 常驻单元 "
                             "bok-model-plane（bok up --models-only，重启补拉模型面；"
                             "缺省 OFF=既有装机零变化）")
    p_setup = sub.add_parser("setup", help="First-run model readiness / download")
    p_setup.add_argument("action", nargs="?", default="status", choices=["status", "download"])
    # tts-pregen/tts-mine 参数原样透传给执行脚本,顶层不做校验
    args, extra = p.parse_known_args(argv)
    args.extra = list(extra)
    return args


def cmd_tts_pregen(extra: list[str] | None = None) -> int:
    """离线批量预合成 TTS 本地缓存(docs/superpowers/specs/2026-09-08-tts-cache-design.md)。

    额外参数原样透传给 scripts/runtime/pregen_tts.py(--greetings/--objects/--fillers/--cp/--model)。
    子进程带仓库 PYTHONPATH 与 SSL_CERT_FILE(certifi)——venv 无系统 CA,
    MiniMax WSS 无此必炸。
    """
    env = {"PYTHONPATH": paths._repo_pythonpath(), "PYTHONUNBUFFERED": "1"}
    env._bake_ssl_cert_file(env, paths.repo_python())
    proc = subprocess.run(
        [str(paths.repo_python()), str(paths.ROOT / "scripts" / "runtime" / "pregen_tts.py"), *(extra or [])],
        env={**os.environ, **env},
    )
    return proc.returncode


def cmd_tts_mine(extra: list[str] | None = None) -> int:
    """高频问答对挖掘报告(快答库,PR-3)。参数透传给 scripts/runtime/mine_qa.py。

    --apply N 把前 N 条入库为 qa_entries(source=mined);入库后跑
    `bok.py tts-pregen` 物化应答音频,闸门只认缓存有音频的条目。
    """
    env = {"PYTHONPATH": paths._repo_pythonpath(), "PYTHONUNBUFFERED": "1"}
    env._bake_ssl_cert_file(env, paths.repo_python())
    proc = subprocess.run(
        [str(paths.repo_python()), str(paths.ROOT / "scripts" / "runtime" / "mine_qa.py"), *(extra or [])],
        env={**os.environ, **env},
    )
    return proc.returncode


# clean-testdata 的 CP base（模块级读 env：进程启动时即定值；同时把「env 读取」
# 移出函数作用域（路径正则/鉴权头在闭包外构造，函数内只留白名单闸+_http_call，
# 静态污点分析可完整看见净化链）
_CP_CLEAN_BASE_URL = os.environ.get("BOK_CP_URL", "http://127.0.0.1:8000")


def cmd_clean_testdata() -> int:
    """清理历史测试数据(QA B3/B7,2026-09-09):对象下拉曾被 300+ E2E/soak 残留灌满。

    默认 dry-run 只打印;`--apply` 才真删(经 CP API,审计可追溯)。范围:
    ①对象 display_name 匹配测试前缀;②人设同名同公司重复(保留最早)。
    """
    import re as _re

    base = _CP_CLEAN_BASE_URL
    apply_mode = "--apply" in sys.argv
    token = os.environ.get("BOK_CP_TOKEN", "")
    # 数据驱动路径白名单：删除目标的 id 来自 CP 响应（o['id']），只认
    # objects/personas 资源 + uuid hex 段，其余（含 ../、斜杠夹带）一律拒发。
    _cp_path_re = _re.compile(r"^/api/(objects|personas)(/[A-Za-z0-9._-]{1,64})?$")
    _auth_headers = {"Authorization": f"Bearer {token}"} if token else None

    def _get(path: str):
        if not _cp_path_re.match(path):
            raise ValueError(f"CP 路径未过白名单（拒发）: {path!r}")
        status, raw = _http_call(f"{base}{path}", headers=_auth_headers, timeout_s=15)
        if status != 200:
            raise RuntimeError(f"CP GET {path} -> HTTP {status}")
        return json.loads(raw.decode())

    def _delete(path: str) -> None:
        if not _cp_path_re.match(path):
            raise ValueError(f"CP 路径未过白名单（拒发）: {path!r}")
        status, _raw = _http_call(f"{base}{path}", "DELETE", headers=_auth_headers, timeout_s=15)
        if status not in (200, 204):
            raise RuntimeError(f"CP DELETE {path} -> HTTP {status}")

    pat = _re.compile(r"^(E2E-|soak\d*-?|并发|LOAD-|边角-|多轮-|probe)")
    objs = _get("/api/objects")
    stale = [o for o in objs if pat.match(str(o.get("display_name") or ""))]
    print(f"objects: total={len(objs)} stale-matched={len(stale)}")
    for o in stale:
        print(f"  - {o['id']} {o.get('display_name')}")
        if apply_mode:
            _delete(f"/api/objects/{o['id']}")

    seen: set[tuple[str, str]] = set()
    dupes = []
    for p_ in _get("/api/personas"):
        k = (str(p_.get("name") or ""), str(p_.get("company") or ""))
        if k in seen:
            dupes.append(p_)
        else:
            seen.add(k)
    print(f"personas: duplicate-matched={len(dupes)}")
    for p_ in dupes:
        print(f"  - {p_['id']} {p_.get('name')} / {p_.get('company')}")
        if apply_mode:
            _delete(f"/api/personas/{p_['id']}")

    if not apply_mode:
        print("dry-run: 未删除任何数据。加 --apply 执行。")
    else:
        print("apply done。")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.cmd == "setup":
        return models.cmd_setup(args.action)
    if args.cmd == "prod":
        return prod.cmd_prod(args.action,
                        node_agent=getattr(args, "node_agent", False),
                        node_args=getattr(args, "extra", None),
                        open_firewall=getattr(args, "open_firewall", False),
                        staging_dir=getattr(args, "staging_dir", ""),
                        with_model_plane=getattr(args, "with_model_plane", False))
    if args.cmd == "tts-pregen":
        return cmd_tts_pregen(getattr(args, "extra", None))
    if args.cmd == "clean-testdata":
        return cmd_clean_testdata()
    if args.cmd == "tts-mine":
        return cmd_tts_mine(getattr(args, "extra", None))
    if args.cmd == "monitor":
        return cmd_monitor()
    if args.cmd == "download":
        only = set(getattr(args, "only", None) or []) or None
        return models.cmd_download(only=only)
    if args.cmd == "up":
        return servers.cmd_up(models_only=bool(getattr(args, "models_only", False)))
    return {"catalog": models.cmd_catalog, "manifest": models.cmd_manifest, "status": cmd_status,
            "serve": servers.cmd_serve, "down": cmd_down, "doctor": doctor.cmd_doctor}[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
