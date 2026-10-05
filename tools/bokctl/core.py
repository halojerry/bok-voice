#!/usr/bin/env python
"""bokctl core —— 域根：健康面/HTTP 共享护栏/pid 探针等共享件（G2 W③ 后）。

CLI 装配与分派住 bokctl.cli（parse_args/main；core 以别名续读门面），命令
实现一命令一模块住 bokctl.commands.*（W③ 起 cmd_status/cmd_monitor/
cmd_down/cmd_tts_pregen/cmd_tts_mine/cmd_clean_testdata/cmd_serve/cmd_up/
cmd_setup/cmd_download 均随分派走，不再住本模块——`bok doctor`/
`bok prod`/`bok catalog`/`bok manifest` 走 cli 适配器进各自域）。

Platform split (MLX is Apple-only):
  mac -> MLX sidecars + mlx_lm server on :1235 + optional MT server on :1236
  win -> transformers sidecars (CUDA torch) + llama.cpp CUDA server on :1235
Zero-Ollama: there is no Ollama anywhere in the distribution path.
"""
from __future__ import annotations

# `time` 的 F401：core 本体 W③ 后无 time 消费点，但 bok 门面镜像 vars(core)
# 需要 bok.time 绑定——tests monkeypatch.setattr(bok.time,…) 读面走门面。
import http.client
import json
import os
import socket
import subprocess
import time  # noqa: F401
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# G2 W②:prod/doctor/proc/health/servers/models 域已搬 tools/bokctl/{prod,doctor,
# proc,health,servers,models}.py;paths 波(2026-10-04)再加 paths;env 波(2026-10-04,
# 最后一批)再加 env——core 侧一律穿模块对象调用(prod.cmd_prod(...)/
# doctor.cmd_doctor(...)/proc._kill_proc_tree(...)/health._wait_desktop_ready(...)/
# servers._start_proc(...)/models.cmd_catalog(...)/paths.app_data_dir(...)/
# env._bake_ssl_cert_file(...),call-time 属性取用=patch 缝与后续域搬运保持可见)。
# G2 W③(2026-10-05):CLI 层再拆——argparse 装配+分派搬 bokctl.cli,命令实现
# 一命令一模块搬 bokctl.commands.*(cmd_status/cmd_monitor/cmd_down/cmd_serve/
# cmd_up/cmd_setup/cmd_download/cmd_tts_pregen/cmd_tts_mine/cmd_clean_testdata;
# doctor/prod/catalog/manifest 走 cli 适配器进各自域)。
# health/doctor/models/proc/prod/servers 的 F401:core 代码 W③ 后无直接消费
# (servers 波把 _warn_llm_not_http_ready/_cmd_up_services/cmd_serve 三个消费点
# 搬出;W③ 再把命令入口搬 bokctl.commands.*),但 bok 门面镜像 vars(core) 需要
# 这些域绑定——tests 的 bok.health._serve_ready_probe*/bok.doctor.cmd_doctor/
# bok.proc._monitor_kill_round/bok.prod.cmd_prod_status/bok.models.X/
# bok.servers._start_call_plane 等读面仍走门面。paths 同理:
# bok.paths.X 读面(测试/脚本)经门面镜像取模块对象。
# cli/commands 绑定:core 是组合根——cli 经此加载(连带 commands.* 全量装载),
# commands 转发面(node_agent bok.commands.down.cmd_down 等)经门面镜像可达。
from bokctl import (  # noqa: E402
    cli,
    commands,  # noqa: F401
    doctor,  # noqa: F401
    env,
    health,  # noqa: F401
    models,  # noqa: F401
    paths,
    proc,  # noqa: F401
    prod,  # noqa: F401
    servers,  # noqa: F401
)

# 门面续读面(tests/脚本 bok.parse_args×10/bok.main×4 经 vars(core) 镜像;main/parse_args
# 是纯叶子——分派缝在 cli 内的 call-time 模块引用,值绑定无假绿面):
parse_args = cli.parse_args
main = cli.main

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
# 单源红线由 tests/test_bok_module_contract.py 钉;消费点(_llm_raw_expected 与
# commands/misc 的 cmd_tts_pregen/cmd_tts_mine)穿 env.X 调用时取。
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


# 健康面服务单点表（commands.status.cmd_status / cmd_doctor / prod.cmd_prod_status
# 共用，防三张表
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
    """cmd_serve（今住 bokctl.commands.serve）就绪等待的桌面栈基础口表：control-plane/asr/llm/livekit +
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


def _cp_bind_host() -> str:
    """CP 监听地址（site-delivery fixwave，补 M2.3 计划项「bind 0.0.0.0 env 开关」）：
    BOK_BIND_HOST 显式 opt-in（如 0.0.0.0）才对外监听，缺省恒 127.0.0.1——本机
    单用户形态行为零变化。serve 与 prod install（launchd/schtasks 单元定义）共用
    同一份解析：--open-firewall 的 :8000 放行规则只有 bind 0.0.0.0 时才有意义。"""
    return (os.environ.get("BOK_BIND_HOST") or "").strip() or "127.0.0.1"


if __name__ == "__main__":
    raise SystemExit(main())
