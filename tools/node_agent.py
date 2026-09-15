"""node-agent：薄节点守护（spec §4.2/§4.3，bok.py serve 的无头演化）。

P0 职责：①向云 CP 心跳上报（失联 ≥max_missed 置 refuse_jobs 旗标，日志可见；
拒派发的执行端是 livekit load_threshold，P3 接 commands 通道后由指令精确控制）
②可选拉起全栈（复用 bok.cmd_up/cmd_down）③把 cpUrl/livekitUrl 注入 web 产物
（runtime-config.js），使同一份静态导出可作节点本地坐席工作台。
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


@dataclass
class NodeConfig:
    cp_url: str
    node_token: str
    heartbeat_interval_s: int = 60
    max_missed: int = 3
    fingerprint: str = ""


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


def register_once(cp_url: str, license_key: str, fingerprint: str, *,
                  name: str = "", platform_label: str = "", version: str = "") -> tuple[str, str]:
    """带 license+指纹注册（加固模式必须）：返回 (node_id, node_token)。"""
    code, body = _post_json(
        f"{cp_url.rstrip('/')}/api/nodes/register",
        {"name": name, "platform": platform_label, "version": version,
         "license_key": license_key, "fingerprint": fingerprint},
    )
    if code != 200 or not body.get("node_token"):
        raise SystemExit(
            f"[node-agent] register failed ({code}): {body.get('detail') or body}")
    return str(body["node_id"]), str(body["node_token"])


def ensure_token(cp_url: str, license_key: str, fingerprint: str,
                 state_file: Path) -> str:
    """license 流的 token 生命周期：状态文件缓存 → 心跳探测 401 → 幂等重注册。

    同 (license, fingerprint) 重注册在 CP 侧复用 node_id 换新 token——机器
    重装/重启/换 token 都走这一条恢复路径，不烧 license 配额。
    """
    token = ""
    if state_file.is_file():
        try:
            token = str(json.loads(state_file.read_text(encoding="utf-8")).get("node_token") or "")
        except Exception:  # noqa: BLE001
            token = ""
    if token:
        code, _ = _post_json(
            f"{cp_url.rstrip('/')}/api/nodes/heartbeat",
            {"metrics": {}, "fingerprint": fingerprint},
            headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if code == 200:
            return token
        print(f"[node-agent] cached token rejected ({code}) — re-registering", flush=True)
    node_id, token = register_once(cp_url, license_key, fingerprint)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # 先 0600 建档再写（write_text+chmod 有 0644 窗口）：token=本机凭据。
    fd = os.open(str(state_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"node_id": node_id, "node_token": token}, ensure_ascii=False))
    print(f"[node-agent] registered as {node_id} (state -> {state_file})", flush=True)
    return token


def heartbeat_once(cfg: NodeConfig, metrics: dict | None = None) -> tuple[bool, dict]:
    body = json.dumps({"metrics": metrics or {}, "fingerprint": cfg.fingerprint}).encode()
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
        print(f"[node-agent] heartbeat failed: {exc!r} {detail}", flush=True)
        return False, detail
    except Exception as exc:  # 失联不抛——计数交给调用方
        print(f"[node-agent] heartbeat failed: {exc!r}", flush=True)
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


# token 失效的自愈判定：CP 心跳 401 的 wire detail（nodes_store/main.py）。
#   "license_required" 是 CP 内部 reason 键，线上 detail 实为
#   "node not licensed (hardened mode)"——一并按字面收录。
_TOKEN_DEAD_DETAILS = ("unknown node token", "revoked", "license_required", "not licensed")


def heartbeat_tick(cfg: NodeConfig, missed: int, *, license_key: str = "",
                   state_file: Path | None = None) -> int:
    """单次心跳；token 失效且带 license 流 → 同 (license,fingerprint) 幂等重注册
    自愈（2026-09-16 深测 P2：单请求顶掉真机后旧版 REFUSE_JOBS 挂到人工重启）。
    被克隆顶掉的场景双方互踢，官方指纹靠 60s 周期最终抢回——每轮至多一次重注册，
    无风暴。返回新 missed 计数。"""
    ok, body = heartbeat_once(cfg, metrics={"missed": missed})
    if not ok and license_key and state_file is not None:
        detail = str((body or {}).get("detail") or "")
        if any(mark in detail for mark in _TOKEN_DEAD_DETAILS):
            try:
                cfg.node_token = ensure_token(cfg.cp_url, license_key, cfg.fingerprint, state_file)
                ok, _ = heartbeat_once(cfg, metrics={"missed": 0})  # 新 token 立即复跳确认
                return 0 if ok else 1
            except SystemExit as exc:
                print(f"[node-agent] re-register failed: {exc}", flush=True)
    return 0 if ok else missed + 1


def heartbeat_loop(cfg: NodeConfig, stop: threading.Event, *,
                   license_key: str = "", state_file: Path | None = None) -> None:
    missed = 0
    while not stop.wait(cfg.heartbeat_interval_s):
        missed = heartbeat_tick(cfg, missed, license_key=license_key, state_file=state_file)
        if should_refuse_jobs(missed, cfg.max_missed):
            print(f"[node-agent] missed={missed} >= {cfg.max_missed}: REFUSE_JOBS (L1)", flush=True)


def main(argv=None) -> int:
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
    ap.add_argument("--ui-dir", default="", help="web 静态产物目录（提供则写 runtime-config.js）")
    ap.add_argument("--livekit-url", default="ws://127.0.0.1:7880")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args(argv)
    if not args.node_token and not args.license_key:
        ap.error("--node-token 或 --license-key 至少给一个")

    fingerprint = collect_fingerprint()
    token = args.node_token
    state_file = None
    if not token:
        state_file = (Path(args.state_file) if args.state_file
                      else Path.home() / ".bok" / "node-state.json")
        token = ensure_token(args.cp_url, args.license_key, fingerprint, state_file)

    cfg = NodeConfig(cp_url=args.cp_url, node_token=token,
                     heartbeat_interval_s=args.interval, fingerprint=fingerprint)
    if args.ui_dir:
        target = write_ui_config(Path(args.ui_dir), cfg.cp_url, args.livekit_url)
        print(f"[node-agent] ui config -> {target}", flush=True)

    if args.heartbeat_only:
        stop = threading.Event()
        print(f"[node-agent] heartbeat loop start (interval={cfg.heartbeat_interval_s}s)", flush=True)
        try:
            heartbeat_loop(cfg, stop, license_key=args.license_key, state_file=state_file)
        except KeyboardInterrupt:
            stop.set()
        return 0

    import bok

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
        bok.cmd_down()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
