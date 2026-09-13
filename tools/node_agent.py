"""node-agent：薄节点守护（spec §4.2/§4.3，bok.py serve 的无头演化）。

P0 职责：①向云 CP 心跳上报（失联 ≥max_missed 置 refuse_jobs 旗标，日志可见；
拒派发的执行端是 livekit load_threshold，P3 接 commands 通道后由指令精确控制）
②可选拉起全栈（复用 bok.cmd_up/cmd_down）③把 cpUrl/livekitUrl 注入 web 产物
（runtime-config.js），使同一份静态导出可作节点本地坐席工作台。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
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


def heartbeat_once(cfg: NodeConfig, metrics: dict | None = None) -> tuple[bool, dict]:
    body = json.dumps({"metrics": metrics or {}}).encode()
    req = urllib.request.Request(
        f"{cfg.cp_url.rstrip('/')}/api/nodes/heartbeat",
        data=body,
        headers={"Authorization": f"Bearer {cfg.node_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, json.loads(resp.read().decode())
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


def heartbeat_loop(cfg: NodeConfig, stop: threading.Event) -> None:
    missed = 0
    while not stop.wait(cfg.heartbeat_interval_s):
        ok, _ = heartbeat_once(cfg, metrics={"missed": missed})
        missed = 0 if ok else missed + 1
        if should_refuse_jobs(missed, cfg.max_missed):
            print(f"[node-agent] missed={missed} >= {cfg.max_missed}: REFUSE_JOBS (L1)", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bok 薄节点守护")
    ap.add_argument("--cp-url", required=True)
    ap.add_argument("--node-token", required=True)
    ap.add_argument("--heartbeat-only", action="store_true", help="不拉起全栈，只跑心跳")
    ap.add_argument("--ui-dir", default="", help="web 静态产物目录（提供则写 runtime-config.js）")
    ap.add_argument("--livekit-url", default="ws://127.0.0.1:7880")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args(argv)

    cfg = NodeConfig(cp_url=args.cp_url, node_token=args.node_token, heartbeat_interval_s=args.interval)
    if args.ui_dir:
        target = write_ui_config(Path(args.ui_dir), cfg.cp_url, args.livekit_url)
        print(f"[node-agent] ui config -> {target}", flush=True)

    if args.heartbeat_only:
        stop = threading.Event()
        print(f"[node-agent] heartbeat loop start (interval={cfg.heartbeat_interval_s}s)", flush=True)
        try:
            heartbeat_loop(cfg, stop)
        except KeyboardInterrupt:
            stop.set()
        return 0

    import bok

    bok.cmd_up()
    stop = threading.Event()
    worker = threading.Thread(target=heartbeat_loop, args=(cfg, stop), daemon=True)
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
