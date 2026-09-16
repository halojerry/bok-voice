"""node-agent：薄节点守护（spec §4.2/§4.3，bok.py serve 的无头演化）。

P0 职责：①向云 CP 心跳上报（失联 ≥max_missed 置 refuse_jobs 旗标，日志可见；
拒派发的执行端是 livekit load_threshold，P3 接 commands 通道后由指令精确控制）
②可选拉起全栈（复用 bok.cmd_up/cmd_down）③把 cpUrl/livekitUrl 注入 web 产物
（runtime-config.js），使同一份静态导出可作节点本地坐席工作台。
④服从远程停机开关（site-delivery Task 6）：root 吊销的心跳 401 detail 携带
机器可执行 action:"shutdown" → 停栈退出（绝不 self-heal）；license 吊销=永久
→ 连续 3 次后退避停栈；auto_clone 克隆吊销保留重注册复活路径。
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
from collections.abc import Callable
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
                 state_file: Path) -> str:
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
        print(f"[node-agent] cached token rejected ({code}) — re-registering", flush=True)
    node_id, token = register_once(cp_url, license_key, fingerprint)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # 先 0600 建档再写（write_text+chmod 有 0644 窗口）：token=本机凭据。
    fd = os.open(str(state_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # O_TRUNC 对已存在文件保留旧 mode——旧版 0644 残档在此扳回
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
    license_revoked_streak=license 吊销连续命中计数（≥3 走 kill，成功清零）。"""
    missed: int = 0
    license_revoked_streak: int = 0


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
    print(f"[node-agent] KILLSWITCH: {message}", flush=True)
    if _kill_stack_hook is not None:
        try:
            _kill_stack_hook()
        except Exception as exc:  # noqa: BLE001 - 停栈失败不阻断退出
            print(f"[node-agent] stack stop error: {exc!r}", flush=True)
    raise SystemExit(0)


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
    ok, body = heartbeat_once(cfg, metrics={"missed": missed})
    if ok:
        if hb is not None:
            hb.license_revoked_streak = 0
        return 0
    kind = classify_heartbeat_failure(body)
    if kind == "root_revoked":
        if _kill_enabled():
            _kill_on_revoke("revoked by control plane — stack stopped")
        print("[node-agent] KILLSWITCH (observe-only): control plane revoked this "
              "node (action=shutdown) — BOK_NODE_KILL_ON_REVOKE=0, stack NOT stopped",
              flush=True)
        return missed + 1
    if kind == "license_revoked":
        streak = (hb.license_revoked_streak if hb is not None else 0) + 1
        if hb is not None:
            hb.license_revoked_streak = streak
        if streak >= _LICENSE_REVOKE_KILL_AFTER and _kill_enabled():
            _kill_on_revoke("license revoked by control plane — stack stopped")
        observe = (streak >= _LICENSE_REVOKE_KILL_AFTER and not _kill_enabled())
        print(f"[node-agent] license revoked (streak {streak}/"
              f"{_LICENSE_REVOKE_KILL_AFTER}) — no self-heal: license revocation "
              f"is permanent, re-registration cannot revive"
              + (" [observe-only: BOK_NODE_KILL_ON_REVOKE=0]" if observe else ""),
              flush=True)
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
            print(f"[node-agent] re-register failed: {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 网络抖动不令守护进程死亡
            print(f"[node-agent] re-register failed: {exc!r}", flush=True)
    return missed + 1


def heartbeat_loop(cfg: NodeConfig, stop: threading.Event, *,
                   license_key: str = "", state_file: Path | None = None) -> None:
    hb = HeartbeatState()
    while not stop.wait(cfg.heartbeat_interval_s):
        hb.missed = heartbeat_tick(cfg, hb.missed, license_key=license_key,
                                   state_file=state_file, hb=hb)
        if should_refuse_jobs(hb.missed, cfg.max_missed):
            print(f"[node-agent] missed={hb.missed} >= {cfg.max_missed}: REFUSE_JOBS (L1)", flush=True)


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
