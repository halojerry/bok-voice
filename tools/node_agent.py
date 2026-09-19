"""node-agent：薄节点守护（spec §4.2/§4.3，bok.py serve 的无头演化）。

P0 职责：①向云 CP 心跳上报（失联 ≥max_missed 置 refuse_jobs 旗标，日志可见；
拒派发的执行端是 livekit load_threshold，P3 接 commands 通道后由指令精确控制）
②可选拉起全栈（复用 bok.cmd_up/cmd_down）③把 cpUrl/livekitUrl 注入 web 产物
（runtime-config.js），使同一份静态导出可作节点本地坐席工作台；④**节点本地
托管坐席 UI**（--ui-dir 给定即 stdlib 静态服务 :3000，话务员浏览器零安装访问；
2026-09-17 起 runbook 的「http://<节点IP>:3000」由本进程兑现，不再依赖桌面壳）
⑤服从远程停机开关（site-delivery Task 6）：root 吊销的心跳 401 detail 携带
机器可执行 action:"shutdown" → 停栈退出（绝不 self-heal）；license 吊销=永久
→ 连续 3 次后退避停栈；auto_clone 克隆吊销保留重注册复活路径。
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


# ---- 日志（W1，2026-09-18）：console + 轮转文件双面 ----
# 文件侧是 upload_logs 远程通道（W2）的货源，也是无 SSH 节点排障的唯一抓手；
# 落盘失败优雅降级 console-only——守护的本职是心跳，日志绝不是启动前提。

LOG = logging.getLogger("bok.node_agent")
LOG.propagate = False

LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_ROTATE_BACKUPS = 5


def setup_logging(log_dir: str | Path | None = None, *,
                  max_bytes: int = LOG_ROTATE_BYTES,
                  backups: int = LOG_ROTATE_BACKUPS) -> Path | None:
    """console（stdout，供 schtasks/systemd 采集）+ RotatingFileHandler 双面日志。

    幂等（重复调用不叠加 handler）；目录不可写（只读盘/权限）返回 None、
    console-only 继续。返回启用时的日志文件路径（upload_logs 的默认货源）。
    """
    LOG.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.FileHandler)
               for h in LOG.handlers):
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("[node-agent] %(message)s"))
        LOG.addHandler(console)
    if log_dir is None:
        return None
    existing = next((h for h in LOG.handlers if isinstance(h, RotatingFileHandler)),
                    None)
    if existing is not None:
        return Path(existing.baseFilename)
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "node-agent.log",
                                 maxBytes=max_bytes, backupCount=backups,
                                 encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(fh)
        return log_dir / "node-agent.log"
    except OSError as exc:
        LOG.warning("file logging disabled (%s) — console only", exc)
        return None


@dataclass
class NodeConfig:
    cp_url: str
    node_token: str
    heartbeat_interval_s: int = 60
    max_missed: int = 3
    fingerprint: str = ""
    version: str = ""


def read_version(root: Path | None = None) -> str:
    """读包版本（build_node_pkg 注入的 VERSION 文件；dev 仓无此文件=空串）。"""
    root = Path(root) if root is not None else ROOT_DIR
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def collect_fingerprint() -> str:
    """机器指纹（sha256 hex，非原始序列号——隐私面只出哈希）。

    平台源：macOS IOPlatformSerialNumber / Linux /etc/machine-id /
    Windows MachineGuid；全部缺失时退 hostname（弱指纹，仅保启动不挂）。
    """
    import hashlib
    import platform as _platform

    raw = ""
    system = _platform.system()
    try:
        if system == "Darwin":
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "IOPlatformSerialNumber" in line:
                    raw = line.split("=")[-1].strip().strip('"')
                    break
        elif system == "Linux":
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    raw = Path(p).read_text(encoding="utf-8").strip()
                    if raw:
                        break
                except OSError:
                    continue
        elif system == "Windows":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                raw = str(winreg.QueryValueEx(key, "MachineGuid")[0])
    except Exception:  # noqa: BLE001 - 指纹是鉴权因子不是启动前提，缺失退弱档
        raw = ""
    if not raw:
        raw = f"weak:{_platform.node()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _post_json(url: str, payload: dict, headers: dict | None = None,
               timeout: int = 10) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            body = {}
        return exc.code, body


class RegisterRevoked(SystemExit):
    """注册被 CP 永久拒绝（sticky root 吊销 / license 吊销）——self-heal 无出路，
    进程必须退出（区别于普通注册失败的 SystemExit：那条在心跳循环里会被吞掉
    转失联计数，sticky 拒绝绝不允许进重试循环）。"""


def register_once(cp_url: str, license_key: str, fingerprint: str, *,
                  name: str = "", platform_label: str = "", version: str = "") -> tuple[str, str]:
    """带 license+指纹注册（加固模式必须）：返回 (node_id, node_token)。

    sticky 拒绝（detail 含 "revoked"：CP 注册闸对 root 吊销节点/license 吊销
    一律 401 不复活，nodes_store.register）→ RegisterRevoked 致命退出、明文
    一行说清原因；其余失败维持原 SystemExit 语义。"""
    code, body = _post_json(
        f"{cp_url.rstrip('/')}/api/nodes/register",
        {"name": name, "platform": platform_label, "version": version,
         "license_key": license_key, "fingerprint": fingerprint},
    )
    if code != 200 or not body.get("node_token"):
        detail_text = _wire_detail_text(body)
        if "revoked" in detail_text.lower():
            raise RegisterRevoked(
                f"[node-agent] FATAL: registration refused by control plane "
                f"({code}): {detail_text or body} — revocation is permanent "
                f"(sticky); re-registration cannot revive this node. Exiting.")
        raise SystemExit(
            f"[node-agent] register failed ({code}): {body.get('detail') or body}")
    return str(body["node_id"]), str(body["node_token"])


def ensure_token(cp_url: str, license_key: str, fingerprint: str,
                 state_file: Path, version: str = "") -> str:
    """license 流的 token 生命周期：状态文件缓存 → 心跳探测 401 分诊 → 幂等重注册。

    同 (license, fingerprint) 重注册在 CP 侧复用 node_id 换新 token——机器
    重装/重启/换 token 都走这一条恢复路径，不烧 license 配额。
    探测 401 分诊（Task 6）：root_revoked（detail dict action=shutdown）→
    KILLSWITCH 停机退出，绝不重注册；license_revoked → 重注册无出路（吊销
    永久），RegisterRevoked 致命退出；其余拒绝照旧重注册自愈。
    """
    token = ""
    if state_file.is_file():
        try:
            token = str(json.loads(state_file.read_text(encoding="utf-8")).get("node_token") or "")
        except Exception:  # noqa: BLE001
            token = ""
    if token:
        code, body = _post_json(
            f"{cp_url.rstrip('/')}/api/nodes/heartbeat",
            {"metrics": {}, "fingerprint": fingerprint},
            headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if code == 200:
            return token
        kind = classify_heartbeat_failure(body)
        if kind == "root_revoked":
            # 探测即处决：CP 已 root 吊销本节点——停机指令必须执行，不得借
            # 重注册绕过（注册闸也只会 401 sticky，试都不必试）。
            _kill_on_revoke("revoked by control plane — stack stopped")
        if kind == "license_revoked":
            raise RegisterRevoked(
                "[node-agent] FATAL: cached-token probe reports license revoked "
                "— revocation is permanent; re-registration cannot revive. Exiting.")
        LOG.warning("cached token rejected (%s) — re-registering", code)
    node_id, token = register_once(cp_url, license_key, fingerprint, version=version)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # 先 0600 建档再写（write_text+chmod 有 0644 窗口）：token=本机凭据。
    fd = os.open(str(state_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # O_TRUNC 对已存在文件保留旧 mode——旧版 0644 残档在此扳回
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"node_id": node_id, "node_token": token}, ensure_ascii=False))
    LOG.info("registered as %s (state -> %s)", node_id, state_file)
    return token


def heartbeat_once(cfg: NodeConfig, metrics: dict | None = None,
                   acks: list | None = None) -> tuple[bool, dict]:
    body = json.dumps({"metrics": metrics or {}, "fingerprint": cfg.fingerprint,
                       "version": cfg.version, "acks": acks or []}).encode()
    req = urllib.request.Request(
        f"{cfg.cp_url.rstrip('/')}/api/nodes/heartbeat",
        data=body,
        headers={"Authorization": f"Bearer {cfg.node_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # 401/403 带 body（detail=unknown node token / node revoked / license revoked /
        # not licensed…）——返回 body 供 heartbeat_tick 自愈判定（2026-09-16 深测 P2-7）。
        try:
            detail = json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            detail = {}
        LOG.warning("heartbeat failed: %r %s", exc, detail)
        return False, detail
    except Exception as exc:  # 失联不抛——计数交给调用方
        LOG.warning("heartbeat failed: %r", exc)
        return False, {}


def should_refuse_jobs(missed: int, max_missed: int) -> bool:
    return missed >= max_missed


def write_ui_config(out_dir: Path, cp_url: str, livekit_url: str) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "runtime-config.js"
    target.write_text(
        "window.__BOK_CONFIG__ = "
        + json.dumps({"cpUrl": cp_url, "livekitUrl": livekit_url})
        + ";\n"
    )
    return target


# ---- 节点本地 UI 托管（2026-09-17：runbook「话务员浏览器开 :3000」的实现载体）----

class _SpaStaticHandler(http.server.SimpleHTTPRequestHandler):
    """静态托管 + SPA 路由回退。

    Next 静态导出的客户端路由（/calls /settings 等无扩展名路径）磁盘上不存在
    对应文件 → 回 index.html 由前端路由接管；带扩展名的真实资产（/_next/*、
    *.js/*.png）缺失仍 404，不吞真 404。目录遍历防护由 SimpleHTTPRequestHandler
    自带。访问日志静默（守护进程日志只留心跳/异常主线），错误日志保留。
    """

    def send_head(self):  # noqa: D102 - 语义见类 docstring
        route = urlparse(self.path).path
        resolved = self.translate_path(self.path)
        if not os.path.exists(resolved) and "." not in PurePosixPath(route).name:
            self.path = "/index.html"
        return super().send_head()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib 签名
        pass  # 静默逐请求访问日志（错误走 log_error，仍可见）


def build_ui_server(ui_dir: Path, bind: str = "0.0.0.0", port: int = 3000
                    ) -> http.server.ThreadingHTTPServer:
    """构造 UI 静态服务（可测缝：测试拿 server 对象自查端口/发请求后 shutdown）。"""
    ui_dir = Path(ui_dir).resolve()
    if not (ui_dir / "index.html").is_file():
        raise FileNotFoundError(f"ui-dir 缺 index.html（先构建 apps/web out/）: {ui_dir}")
    handler = functools.partial(_SpaStaticHandler, directory=str(ui_dir))
    return http.server.ThreadingHTTPServer((bind, port), handler)


def serve_ui(ui_dir: Path, bind: str = "0.0.0.0", port: int = 3000) -> None:
    """阻塞式托管循环（放守护线程跑）：绑定失败/资产缺失只降级不杀心跳——
    守护的本职是心跳与栈托管，UI 端口被占（如同机 dev web :3000）时让位。"""
    try:
        server = build_ui_server(ui_dir, bind, port)
    except Exception as exc:  # noqa: BLE001 - UI 托管失败绝不拖垮守护
        LOG.warning("ui serve skipped (%r)", exc)
        return
    LOG.info("ui serving http://%s:%s <- %s", bind, port, ui_dir)
    try:
        server.serve_forever(poll_interval=0.5)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("ui serve stopped: %r", exc)
    finally:
        server.server_close()


# ---- 远程停机开关（site-delivery Task 6，wire 契约见 control_plane/main.py
# node_heartbeat 的 401 detail 塑形 + scripts/probe_killswitch.py ④⑤⑦⑨）----

# 全栈停止钩子：full-stack 模式由 main 注入 bok.cmd_down 的幂等包装（kill 路径
# 与 main finally 共享同一「只真停一次」旗标）；heartbeat-only/启动早期无栈
# 可停，保持 None。
_kill_stack_hook: Callable[[], None] | None = None

# license 吊销退避阈值：连续 N 次心跳命中 license_revoked → 走 kill 路径。
# 不立即 kill 是给「CP 侧数据修复/误操作回滚」留一个观察窗，3 次后不再等。
_LICENSE_REVOKE_KILL_AFTER = 3

# 允许 self-heal 重注册的失败类别；root_revoked/license_revoked/network 一律不在内。
_SELF_HEAL_KINDS = ("token_stale", "unlicensed", "auto_clone_revoked")


@dataclass
class HeartbeatState:
    """心跳循环跨轮计数：missed=失联计数（REFUSE_JOBS，语义不变）；
    license_revoked_streak=license 吊销连续命中计数（≥3 走 kill，成功清零）；
    pending_acks=待回执指令（下轮心跳带给 CP，update 失败回执用）。"""
    missed: int = 0
    license_revoked_streak: int = 0
    pending_acks: list | None = None

    def __post_init__(self) -> None:
        if self.pending_acks is None:
            self.pending_acks = []


def _wire_detail_text(body: dict | None) -> str:
    """401/403 体的 detail 归一为文本——detail 可能是纯字符串（auto_clone/
    license 类）也可能是结构化 dict（root 吊销的 shutdown 指令）。"""
    detail = (body or {}).get("detail")
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(detail)


def classify_heartbeat_failure(body: dict | None) -> str:
    """心跳失败分类（判定次序即优先级）：

    - root_revoked       dict detail 且 action=="shutdown"（root 吊销=sticky，
                         复活结构性不可能——CP 的机器可执行停机指令）；
    - license_revoked    文本含 "license"+"revok"（"license revoked" /
                         "license_revoked"——license 吊销永久，重注册必然 401）；
    - auto_clone_revoked 其余含 "revoked" 的纯文本（克隆检出 auto_clone=原机
                         重注册复活路径保留，不逼停）；
    - token_stale        "unknown node token"；
    - unlicensed         "license_required"/"not licensed"（加固档未绑 license）；
    - network            detail 缺失 / 连接异常（heartbeat_once 异常路返回 {}）。
    """
    detail = (body or {}).get("detail")
    if isinstance(detail, dict) and str(detail.get("action", "")).lower() == "shutdown":
        return "root_revoked"
    text = _wire_detail_text(body).lower()
    if "license" in text and "revok" in text:
        return "license_revoked"
    if "revoked" in text:
        return "auto_clone_revoked"
    if "unknown node token" in text:
        return "token_stale"
    if "license_required" in text or "not licensed" in text:
        return "unlicensed"
    return "network"


def _kill_enabled() -> bool:
    """BOK_NODE_KILL_ON_REVOKE 闸：默认开（执行 CP 停机指令），"0"=观察档
    （只逐轮大声记录、不停栈不退出——审计/灰度逃生口）。"""
    return os.environ.get("BOK_NODE_KILL_ON_REVOKE", "1") != "0"


def _kill_on_revoke(message: str) -> None:
    """熔断退出：大声日志 → （全栈模式）停栈 → 干净退出（SystemExit 0）。

    停栈经 _kill_stack_hook（幂等，见上）；停栈失败不阻断退出——节点身份已被
    CP 吊销，继续跑只会持续 401，退出本身必须完成。heartbeat-only 模式该
    SystemExit 直接传导为进程退出；full-stack 模式 worker 线程随之终止，main
    的存循轮询 ~1s 内收尾（finally 的幂等停栈此时是 no-op）。"""
    LOG.critical("KILLSWITCH: %s", message)
    if _kill_stack_hook is not None:
        try:
            _kill_stack_hook()
        except Exception as exc:  # noqa: BLE001 - 停栈失败不阻断退出
            LOG.error("stack stop error: %r", exc)
    raise SystemExit(0)


# ---- commands 通道执行侧（P3，2026-09-17）----

# 重启/更新完成后的退出码：非零 → schtasks RestartOnFailure / launchd KeepAlive
# 拉回进程 = 重启语义（cmd_up 随启动拉全栈）。0=干净退出（kill-switch 专用——
# RestartOnFailure 不拉回，节点保持死亡）。
_RESTART_EXIT_CODE = 75

# worker 线程请求的进程退出码（线程里 raise SystemExit 只死线程不传主进程——
# main 循环结束后读取；heartbeat-only 模式直接在主线程传播，不经此）。
_requested_exit_code: list[int] = []


def _request_exit(code: int) -> None:
    """请求进程退出码并让当前「线程」终止（full 模式 worker 线程由此退场，
    main 轮询收尾；heartbeat-only 模式在主线程直接 SystemExit 传播）。"""
    _requested_exit_code.append(code)
    raise SystemExit(code)


def _stop_stack_quiet(reason: str) -> None:
    LOG.info("%s — stopping stack", reason)
    if _kill_stack_hook is not None:
        try:
            _kill_stack_hook()
        except Exception as exc:  # noqa: BLE001 - 停栈失败不阻断退出路径
            LOG.error("stack stop error: %r", exc)


def dispatch_commands(cfg: NodeConfig, commands: list, *,
                      hb: HeartbeatState | None = None,
                      stop_stack: bool = True) -> None:
    """执行心跳响应里的指令（动作到处置的映射，绝不执行任意 shell）。

    - shutdown：停栈 + exit(0)——与 kill-switch 同路（非零才拉回，0=保持死亡）。
    - restart：停栈 + exit(75)——守护拉回进程，cmd_up 随启动带回全栈。
    - update：perform_update 成功 → exit(75)（CP 由 version 收敛关单，无需 ack）；
      失败 → ack 回执（ok=false），继续以旧版本服务，绝不带伤退出。
    未知动作：大声记录 + 忽略（新动作两端未同步时旧节点不炸）。
    """
    for cmd in commands or []:
        action = str((cmd or {}).get("action", ""))
        cid = str((cmd or {}).get("id", ""))
        args = (cmd or {}).get("args") or {}
        if action == "shutdown":
            _stop_stack_quiet("shutdown command from control plane")
            _request_exit(0)
        elif action == "restart":
            if stop_stack:
                _stop_stack_quiet("restart command from control plane")
            _request_exit(_RESTART_EXIT_CODE)
        elif action == "update":
            version = str(args.get("version", ""))
            err = perform_update(cfg, version, stop_stack=stop_stack)
            if err:
                LOG.error("update -> %s FAILED: %s", version, err)
                if hb is not None and hb.pending_acks is not None:
                    hb.pending_acks.append(
                        {"id": cid, "ok": False, "result": err[:200]})
            else:
                if stop_stack:
                    _stop_stack_quiet(f"updated -> {version}")
                LOG.info("update -> %s done; exiting for relaunch", version)
                _request_exit(_RESTART_EXIT_CODE)
        elif action == "upload_logs":
            stack: Path | None = None
            try:
                import bok
                stack = bok.app_data_dir() / "logs"
            except Exception:  # noqa: BLE001 - 栈日志是增强不是前提
                stack = None
            err = upload_recent_logs(cfg, stack_dir=stack)
            if err:
                LOG.warning("upload_logs FAILED: %s", err)
                if hb is not None and hb.pending_acks is not None:
                    hb.pending_acks.append({"id": cid, "ok": False, "result": err[:200]})
            else:
                LOG.info("upload_logs done (%s)", cid)
                if hb is not None and hb.pending_acks is not None:
                    hb.pending_acks.append({"id": cid, "ok": True, "result": "logs uploaded"})
        else:
            LOG.warning("ignoring unknown command action=%r (id=%s)", action, cid)


def _http_download(url: str, token: str, dest: Path, timeout: int = 300) -> None:
    """流式下载（Bearer node_token 自证，与心跳同凭据面）。非 200 抛 RuntimeError。"""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def perform_update(cfg: NodeConfig, version: str, *,
                   root: Path | None = None, stop_stack: bool = True) -> str:
    """update 指令执行体：CP 拉包 → sha256 校验 → 覆盖代码树 → 重装 → 停栈。

    返回 ""=成功（调用方 exit(75) 交守护拉起新版）；失败返回原因文本（调用方
    ack 回执、原地继续旧版本）。

    机制要点：生产栈的业务代码跑在 runtime python 的 site-packages（非
    editable 安装）——只换代码树不生效，必须跟 pip 重装。runtime/（python/
    livekit/node/llama，GB 级）与模型（app-data）不在包内、跨版本复用，
    覆盖时原样保留。诚实边界：copytree 覆盖不删除「新版已移除」的旧文件
    （残留物不在运行导入路径上，风险≈0；全量干净换目录方案受 Windows
    目录锁限制，留 P2）。"""
    version = version.strip()
    if not version:
        return "update: empty version"
    # 前导 v 归一（tag=v0.4.0、命令随手写 v0.4.0/0.4.0 两态）：工件卷存储
    # 与 VERSION 文件恒为无 v 形态，URL 一律按无 v 拼。
    version = version[1:] if version.startswith("v") else version
    if version == cfg.version:
        return f"update: already at {version}"
    # 与 CP 下载端点同款白名单：version 进 URL 路径前先本地校验（防御性——
    # 正常 CP 不会发怪版本号，被劫持的指令面也不该能在节点上拼路径）。
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        return f"update: invalid version {version!r}"
    root = Path(root) if root is not None else ROOT_DIR
    base = cfg.cp_url.rstrip("/")
    import hashlib
    import shutil
    import tarfile
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bok-update-") as td:
        tgz = Path(td) / f"bok-node-{version}.tar.gz"
        sha_path = Path(td) / "artifact.sha256"
        try:
            _http_download(f"{base}/api/nodes/downloads/pkg/{version}/"
                           f"bok-node-{version}.tar.gz", cfg.node_token, tgz)
            _http_download(f"{base}/api/nodes/downloads/pkg/{version}/"
                           f"bok-node-{version}.tar.gz.sha256", cfg.node_token, sha_path)
        except Exception as exc:  # noqa: BLE001 - 下载失败=可回执的普通失败
            return f"download failed: {exc}"
        expected = sha_path.read_text(encoding="utf-8").strip().split()[0].lower()
        digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
        if digest != expected:
            return f"sha256 mismatch (expected {expected[:12]}…, got {digest[:12]}…)"
        extract = Path(td) / "x"
        with tarfile.open(tgz, "r:gz") as tar:
            try:
                tar.extractall(extract, filter="data")  # py3.12+ 防路径穿越
            except TypeError:  # 旧解释器无 filter 参数
                tar.extractall(extract)
        # 包内顶层目录归一（git archive 前缀 / 直接打包根都接受）。
        src = extract
        entries = list(extract.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            src = entries[0]
        if not (src / "tools" / "node_agent.py").is_file():
            return "artifact layout unexpected (tools/node_agent.py missing)"
        LOG.info("update: verified %s, overlaying %s", version, root)
        shutil.copytree(src, root, dirs_exist_ok=True)

    # runtime python 在盘才重装（dev 仓/裸心跳面优雅跳过）；栈先停（Windows
    # 下运行中的 .pyd 锁会让 pip 覆盖失败）。
    rt_py = (root / "runtime" / "python" / "bin" / "python3")
    if os.name == "nt":
        rt_py = root / "runtime" / "python" / "python.exe"
    if stop_stack and _kill_stack_hook is not None:
        _stop_stack_quiet(f"update -> {version}")
    if rt_py.exists():
        if os.name == "nt":
            req_file = "requirements-runtime-win.txt"
        elif sys.platform == "darwin":
            req_file = "requirements-runtime-mac.txt"
        else:
            req_file = "requirements-runtime-linux.txt"
        LOG.info("update: reinstalling packages (%s)", rt_py.name)
        projects = ["packages/core", "packages/business-db", "packages/knowledge",
                    "packages/observability", "apps/control-plane",
                    "apps/agent[livekit]"]
        rc = subprocess.run([str(rt_py), "-m", "pip", "install", "--no-cache-dir",
                             "-r", str(root / req_file)],
                            cwd=str(root)).returncode
        if rc == 0:
            rc = subprocess.run([str(rt_py), "-m", "pip", "install", "--no-cache-dir",
                                 *projects],
                                cwd=str(root)).returncode
        if rc != 0:
            return f"pip reinstall failed rc={rc} (code tree updated; retry update)"
    else:
        LOG.warning("update: runtime python not found — code tree only")
    return ""


# ---- 远程日志通道（W2，2026-09-18）：upload_logs 指令 → 打包上报 ----
# 零入站模型不破：节点领到指令后主动 POST，CP 无法反向拉。无 SSH 的客户
# 节点排障全靠这条通道——包内日志丢了，远程就只剩心跳数字可看。

_LOG_UPLOAD_MAX_BYTES = 8 * 1024 * 1024


def _collect_log_files(log_dir: Path, stack_dir: Path | None) -> list[Path]:
    """货源收集：node-agent 日志（含轮转分卷）+ 栈日志目录（bok app-data/logs）。

    栈目录由调用方惰性解析（full 模式 import bok 才有；心跳-only/导入失败
    优雅跳过）。去重保序，缺文件自然跳过。
    """
    files = sorted(log_dir.glob("node-agent.log*")) if log_dir.is_dir() else []
    if stack_dir is not None and stack_dir.is_dir():
        files.extend(sorted(p for p in stack_dir.iterdir() if p.is_file()))
    seen: set[str] = set()
    out: list[Path] = []
    for p in files:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def upload_recent_logs(cfg: NodeConfig, *, log_dir: Path | None = None,
                       stack_dir: Path | None = None,
                       max_bytes: int = _LOG_UPLOAD_MAX_BYTES) -> str:
    """upload_logs 执行体：近期日志 tar.gz → POST /api/nodes/logs。

    返回 ""=成功；非空=失败原因（调用方 ack ok=false 回执）。
    预算封顶 max_bytes，超出预算的文件整只跳过不截断（半截日志误导排障）。
    """
    import tarfile
    import tempfile

    ldir = Path(log_dir) if log_dir is not None else (ROOT_DIR / "logs")
    sources = _collect_log_files(ldir, stack_dir)
    budget = max_bytes
    picked: list[Path] = []
    for p in sources:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > budget:
            continue
        budget -= size
        picked.append(p)
    if not picked:
        return "no log files to upload"
    try:
        with tempfile.TemporaryDirectory(prefix="bok-logup-") as td:
            bundle = Path(td) / "logs.tar.gz"
            with tarfile.open(bundle, "w:gz") as tar:
                for p in picked:
                    # node-agent 卷落包根，栈日志落 stack/ 前缀——两目录同名不打架。
                    arcname = p.name if p.parent == ldir else f"stack/{p.name}"
                    tar.add(p, arcname=arcname)
            payload = bundle.read_bytes()
        req = urllib.request.Request(
            f"{cfg.cp_url.rstrip('/')}/api/nodes/logs",
            data=payload, method="POST",
            headers={"Authorization": f"Bearer {cfg.node_token}",
                     "Content-Type": "application/gzip"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
        return "" if body.get("ok") else f"cp rejected: {body}"
    except urllib.error.HTTPError as exc:
        return f"upload failed: HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 可回执的普通失败，绝不炸心跳循环
        return f"upload failed: {exc!r}"


def heartbeat_tick(cfg: NodeConfig, missed: int, *, license_key: str = "",
                   state_file: Path | None = None,
                   hb: HeartbeatState | None = None) -> int:
    """单次心跳；失败按 classify_heartbeat_failure 分诊（Task 6）：

    - root_revoked：CP 停机指令——绝不 self-heal/重注册；kill 闸开（默认）→
      停栈+退出，闸关（=0）→ 观察档逐轮日志继续；
    - license_revoked：重注册无出路——不 self-heal，连续 ≥3 次走同一 kill
      路径（退避），成功即清零；
    - token_stale/unlicensed/auto_clone_revoked：license 流幂等重注册自愈
      （克隆检出恢复路径保留；每轮至多一次，无风暴）；
    - network/其他：失联计数（REFUSE_JOBS 语义不变）。
    返回新 missed 计数。"""
    ok, body = heartbeat_once(cfg, metrics={"missed": missed},
                              acks=(hb.pending_acks if hb is not None else None))
    if hb is not None and hb.pending_acks:
        hb.pending_acks.clear()
    if ok:
        if hb is not None:
            hb.license_revoked_streak = 0
        commands = body.get("commands") or []
        if commands:
            LOG.info("received %d command(s): %s", len(commands),
                     [c.get('action') for c in commands])
            dispatch_commands(cfg, commands, hb=hb)
        return 0
    kind = classify_heartbeat_failure(body)
    if kind == "root_revoked":
        if _kill_enabled():
            _kill_on_revoke("revoked by control plane — stack stopped")
        LOG.warning("KILLSWITCH (observe-only): control plane revoked this "
                    "node (action=shutdown) — BOK_NODE_KILL_ON_REVOKE=0, "
                    "stack NOT stopped")
        return missed + 1
    if kind == "license_revoked":
        streak = (hb.license_revoked_streak if hb is not None else 0) + 1
        if hb is not None:
            hb.license_revoked_streak = streak
        if streak >= _LICENSE_REVOKE_KILL_AFTER and _kill_enabled():
            _kill_on_revoke("license revoked by control plane — stack stopped")
        observe = (streak >= _LICENSE_REVOKE_KILL_AFTER and not _kill_enabled())
        LOG.warning("license revoked (streak %d/%d) — no self-heal: license "
                    "revocation is permanent, re-registration cannot revive%s",
                    streak, _LICENSE_REVOKE_KILL_AFTER,
                    " [observe-only: BOK_NODE_KILL_ON_REVOKE=0]" if observe else "")
        return missed + 1
    if license_key and state_file is not None and kind in _SELF_HEAL_KINDS:
        try:
            cfg.node_token = ensure_token(cfg.cp_url, license_key, cfg.fingerprint, state_file)
            ok, _ = heartbeat_once(cfg, metrics={"missed": 0})  # 新 token 立即复跳确认
            if ok and hb is not None:
                hb.license_revoked_streak = 0
            return 0 if ok else 1
        except RegisterRevoked:
            raise  # sticky 拒绝（node/license revoked）——致命，绝不吞成失联计数
        except SystemExit as exc:
            LOG.warning("re-register failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 - 网络抖动不令守护进程死亡
            LOG.warning("re-register failed: %r", exc)
    return missed + 1


def heartbeat_loop(cfg: NodeConfig, stop: threading.Event, *,
                   license_key: str = "", state_file: Path | None = None) -> None:
    hb = HeartbeatState()
    while not stop.wait(cfg.heartbeat_interval_s):
        hb.missed = heartbeat_tick(cfg, hb.missed, license_key=license_key,
                                   state_file=state_file, hb=hb)
        if should_refuse_jobs(hb.missed, cfg.max_missed):
            LOG.warning("missed=%d >= %d: REFUSE_JOBS (L1)", hb.missed, cfg.max_missed)


def main(argv=None) -> int:
    global _kill_stack_hook

    ap = argparse.ArgumentParser(description="Bok 薄节点守护")
    ap.add_argument("--cp-url", required=True)
    ap.add_argument("--node-token", default="",
                    help="直接给 token（预签发/旧流程）；与 --license-key 二选一")
    ap.add_argument("--license-key", default=os.environ.get("BOK_LICENSE_KEY", ""),
                    help="license 流：自动注册（同机幂等复用 node_id），token 落状态文件；"
                         "缺省回退 BOK_LICENSE_KEY env（键不走 argv——ps/history 可见）")
    ap.add_argument("--state-file", default="",
                    help="license 流 token 状态文件（默认 ~/.bok/node-state.json，chmod 600）")
    ap.add_argument("--name", default="", help="节点名（缺省 CP 侧默认）")
    ap.add_argument("--heartbeat-only", action="store_true", help="不拉起全栈，只跑心跳")
    ap.add_argument("--ui-dir", default="", help="web 静态产物目录（提供则写 runtime-config.js；"
                    "非 --heartbeat-only 时同时本地托管 :3000）")
    ap.add_argument("--ui-port", type=int, default=3000, help="UI 托管端口（默认 3000）")
    ap.add_argument("--ui-bind", default="0.0.0.0",
                    help="UI 托管绑定地址（默认 0.0.0.0=内网话务员可访问；仅本机用 127.0.0.1）")
    ap.add_argument("--no-ui", action="store_true",
                    help="不托管 UI（仍写 runtime-config.js）——端口冲突让位/特殊拓扑逃生口")
    ap.add_argument("--log-dir", default=os.environ.get("BOK_NODE_LOG_DIR", ""),
                    help="日志目录（默认 <包根>/logs；env BOK_NODE_LOG_DIR；"
                         "显式传 none=仅 console）")
    ap.add_argument("--livekit-url", default="ws://127.0.0.1:7880")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args(argv)
    if not args.node_token and not args.license_key:
        ap.error("--node-token 或 --license-key 至少给一个")

    log_dir = None if args.log_dir == "none" else (args.log_dir or ROOT_DIR / "logs")
    log_file = setup_logging(log_dir)
    if log_file:
        LOG.info("logging to %s", log_file)

    fingerprint = collect_fingerprint()
    version = read_version()
    token = args.node_token
    state_file = None
    if not token:
        state_file = (Path(args.state_file) if args.state_file
                      else Path.home() / ".bok" / "node-state.json")
        token = ensure_token(args.cp_url, args.license_key, fingerprint, state_file,
                             version=version)

    cfg = NodeConfig(cp_url=args.cp_url, node_token=token,
                     heartbeat_interval_s=args.interval, fingerprint=fingerprint,
                     version=version)
    if version:
        LOG.info("node package version: %s", version)
    if args.ui_dir:
        target = write_ui_config(Path(args.ui_dir), cfg.cp_url, args.livekit_url)
        LOG.info("ui config -> %s", target)
        if not args.no_ui and not args.heartbeat_only:
            threading.Thread(target=serve_ui, args=(Path(args.ui_dir), args.ui_bind, args.ui_port),
                             daemon=True, name="ui-serve").start()

    if args.heartbeat_only:
        stop = threading.Event()
        LOG.info("heartbeat loop start (interval=%ds)", cfg.heartbeat_interval_s)
        try:
            heartbeat_loop(cfg, stop, license_key=args.license_key, state_file=state_file)
        except KeyboardInterrupt:
            stop.set()
        return 0

    import bok

    stack_down = False

    def _stop_stack_once() -> None:
        """kill 路径与 main finally 共享的幂等停栈：cmd_down 只真跑一次
        （KILLSWITCH 在 worker 线程停过栈后，finally 不得对已拆的栈再拆一遍）。"""
        nonlocal stack_down
        if stack_down:
            return
        stack_down = True
        bok.cmd_down()

    _kill_stack_hook = _stop_stack_once
    bok.cmd_up()
    stop = threading.Event()
    worker = threading.Thread(
        target=functools.partial(heartbeat_loop, cfg, stop,
                                 license_key=args.license_key, state_file=state_file),
        daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        _stop_stack_once()
    # worker 线程经 _request_exit 终场时（restart/update=75）把退出码带给进程
    # ——非零交 schtasks RestartOnFailure / launchd KeepAlive 拉回即重启/上新版。
    return _requested_exit_code[-1] if _requested_exit_code else 0


if __name__ == "__main__":
    raise SystemExit(main())
