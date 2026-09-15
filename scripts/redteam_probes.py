"""Bok CP 认证/节点面红队探针（防御性自检，docs/SECURITY_REDTEAM.md [probe-1..9]）。

授权范围=本仓自有系统；探针只打**加固 CP**（BOK_AUTH_REQUIRED=1）：
自起实例（--self-host：宿主口 18015 + 临时 sqlite + 随机 root 密钥，跑完即清理）
或显式指定的授权目标（--base-url）。每条 PASS=防线守住；FAIL=防线被破（回归信号）。
SKIP=环境缺前置（不计失败）。退出码：任一 FAIL → 1，否则 0。

纯 stdlib（urllib），自起模式用 subprocess 起 uvicorn——照 scripts/node_handshake_smoke.py
的调用姿势但独立实现（不 import 其私有函数）。绝不打 :8000 开发栈。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SELF_HOST_PORT = 18015
SELF_HOST_BASE = f"http://127.0.0.1:{SELF_HOST_PORT}"
READY_TIMEOUT_S = 60.0
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---- 结果账本 ------------------------------------------------------------


@dataclass
class ProbeResult:
    """单条探针结果：ok=True PASS / False FAIL / None SKIP。"""

    probe: str
    ok: bool | None
    label: str
    detail: str = ""


@dataclass
class Ctx:
    """探针上下文：目标基址 + 可选凭据。"""

    base: str
    timeout: float = 10.0
    jwt: str = ""
    cp_token: str = ""
    results: list[ProbeResult] = field(default_factory=list)

    def record(self, probe: str, ok: bool | None, label: str, detail: str = "") -> None:
        tag = "PASS" if ok is True else ("FAIL" if ok is False else "SKIP")
        line = f"[{tag}] [{probe}] {label}"
        if detail:
            line += f" —— {detail}"
        print(line, flush=True)
        self.results.append(ProbeResult(probe, ok, label, detail))


# ---- HTTP 小件（独立实现，与 handshake smoke 同姿势） ----------------------


def _request(
    method: str,
    url: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any, float]:
    """返回 (http_status, 解析后的 JSON 体, 耗时 ms)；连接级失败 status=0。"""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, json.loads(raw), (time.monotonic() - start) * 1000
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "null")
        except Exception:
            payload = None
        return exc.code, payload, (time.monotonic() - start) * 1000
    except Exception as exc:  # URLError/连接拒绝——当步失败，不带崩整个脚本
        return 0, str(exc), (time.monotonic() - start) * 1000


def _fingerprint(tag: str) -> str:
    """伪造一台机器的指纹（sha256 hex，与 node-agent collect_fingerprint 同形）。"""
    return hashlib.sha256(f"redteam-fp-{tag}-{time.time()}-{secrets.token_hex(4)}".encode()).hexdigest()


def _register_node(ctx: Ctx, *, license_key: str = "", fingerprint: str = "") -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {
        "name": f"redteam-{secrets.token_hex(3)}",
        "platform": "redteam-probe",
        "version": "probe",
    }
    if license_key:
        body["license_key"] = license_key
    if fingerprint:
        body["fingerprint"] = fingerprint
    status, payload, _ = _request(
        "POST", f"{ctx.base}/api/nodes/register", body=body, timeout=ctx.timeout)
    return status, payload if isinstance(payload, dict) else {}


def _heartbeat(ctx: Ctx, node_token: str, fingerprint: str) -> tuple[int, dict[str, Any]]:
    status, payload, _ = _request(
        "POST", f"{ctx.base}/api/nodes/heartbeat", token=node_token,
        body={"metrics": {}, "fingerprint": fingerprint}, timeout=ctx.timeout)
    return status, payload if isinstance(payload, dict) else {}


def _issue_license(ctx: Ctx, max_nodes: int, note: str) -> tuple[str, str] | None:
    """root 签发 license，返回 (license_id, 明文 key)；失败返回 None（已记 FAIL 的前置）。"""
    status, payload, _ = _request(
        "POST", f"{ctx.base}/api/nodes/licenses", token=ctx.jwt,
        body={"max_nodes": max_nodes, "note": note}, timeout=ctx.timeout)
    if status == 200 and isinstance(payload, dict) and payload.get("license_key"):
        return str(payload["license_id"]), str(payload["license_key"])
    return None


def _forge_hs256_token(secret: bytes, claims: dict[str, Any]) -> str:
    """手搓一个 HS256 JWT（探针用：密钥是**错的**，期望服务端 401）。"""

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = b64(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _login_root(ctx: Ctx, username: str, password: str) -> bool:
    status, payload, _ = _request(
        "POST", f"{ctx.base}/api/auth/login",
        body={"username": username, "password": password}, timeout=ctx.timeout)
    ok = status == 200 and isinstance(payload, dict) and bool(payload.get("token"))
    if ok:
        ctx.jwt = str(payload["token"])
    return ok


# ---- probe-1..8（对 CP 实例） ---------------------------------------------


def probe_1_open_register(ctx: Ctx) -> None:
    """[probe-1] A1①：裸注册（无 license key、无任何凭据）必须 401。"""
    status, payload = _register_node(ctx)
    if status == 200:
        ctx.record("probe-1", False, "A1 裸注册被拒（401）",
                   f"HTTP 200 注册成功={payload.get('node_id')}——CP 未开加固模式或 license 闸失效")
        return
    ctx.record("probe-1", status == 401, "A1 裸注册被拒（401）",
               f"HTTP {status}（期望 401）{payload if status != 401 else ''}")


def probe_2_wrong_keys(ctx: Ctx) -> None:
    """[probe-2] A2②：错 license key 恒 401（暴力猜测无面；耗时仅信息性）。"""
    times: list[float] = []
    all_denied = True
    first_bad = ""
    for i in range(8):
        wrong = f"bokn_{secrets.token_urlsafe(18)}"  # 形态逼真、值随机
        status, _, ms = _request(
            "POST", f"{ctx.base}/api/nodes/register",
            body={"name": f"redteam-brute-{i}", "platform": "redteam-probe",
                  "version": "probe", "license_key": wrong,
                  "fingerprint": _fingerprint(f"brute-{i}")},
            timeout=ctx.timeout)
        times.append(ms)
        if status != 401:
            all_denied = False
            first_bad = f"第 {i} 把 key 得 HTTP {status}"
            break
    timing = (f"耗时 min/avg/max={min(times):.0f}/{sum(times) / len(times):.0f}/{max(times):.0f}ms"
              if times else "")
    if not all_denied:
        ctx.record("probe-2", False, "A2 错 key 恒 401（暴力猜测无面）", first_bad)
        return
    ctx.record("probe-2", True, "A2 错 key 恒 401（暴力猜测无面）",
               f"8 把随机 key 全 401；{timing}（sha256 摘要域比较，无逐位计时 oracle）")


def probe_3_token_theft(ctx: Ctx) -> None:
    """[probe-3] A3③：node_token 跨机使用 → fingerprint_mismatch 自动吊销（原指纹也死）。"""
    if not ctx.jwt:
        ctx.record("probe-3", None, "A3 token 窃取跨机被吊销", "缺 root 凭据（无法签发 license）")
        return
    lic = _issue_license(ctx, max_nodes=2, note="redteam-probe-3")
    if lic is None:
        ctx.record("probe-3", False, "A3 token 窃取跨机被吊销", "license 签发失败（前置）")
        return
    license_id, license_key = lic
    fp_a = _fingerprint("victim")
    status, reg = _register_node(ctx, license_key=license_key, fingerprint=fp_a)
    node_token = str(reg.get("node_token") or "")
    if status != 200 or not node_token:
        ctx.record("probe-3", False, "A3 token 窃取跨机被吊销",
                   f"合法 key+指纹 A 注册失败 HTTP {status} {reg}")
        return
    # 小偷拿同 token 换指纹 B 心跳 → 必须 401 且节点被自动吊销。
    status_b, body_b = _heartbeat(ctx, node_token, _fingerprint("thief"))
    # 吊销后原机同 token 原指纹心跳 → 也必须 401（同死，不是只拒单次）。
    status_a, body_a = _heartbeat(ctx, node_token, fp_a)
    ok = status_b == 401 and status_a == 401
    detail = (f"指纹 B 心跳 HTTP {status_b}（期望 401 fingerprint_mismatch）；"
              f"吊销后指纹 A 心跳 HTTP {status_a}（期望 401 revoked）")
    if status_b == 401 and status_a != 401:
        detail += "——异指纹拒了但节点未吊销（fingerprint_mismatch 自动吊销失效）"
    ctx.record("probe-3", ok, f"A3 token 窃取跨机被吊销（license={license_id[:12]}…）", detail)


def probe_4_revoke(ctx: Ctx) -> None:
    """[probe-4] A5⑤：root 吊销 license → 名下节点下一次心跳即 401。"""
    if not ctx.jwt:
        ctx.record("probe-4", None, "A5 license 吊销时效", "缺 root 凭据（无法签发/吊销 license）")
        return
    lic = _issue_license(ctx, max_nodes=1, note="redteam-probe-4")
    if lic is None:
        ctx.record("probe-4", False, "A5 license 吊销时效", "license 签发失败（前置）")
        return
    license_id, license_key = lic
    fp = _fingerprint("revoke")
    status, reg = _register_node(ctx, license_key=license_key, fingerprint=fp)
    node_token = str(reg.get("node_token") or "")
    if status != 200 or not node_token:
        ctx.record("probe-4", False, "A5 license 吊销时效", f"注册失败 HTTP {status} {reg}")
        return
    hb1, body1 = _heartbeat(ctx, node_token, fp)
    if hb1 != 200 or body1.get("ok") is not True:
        ctx.record("probe-4", False, "A5 license 吊销时效", f"吊销前心跳异常 HTTP {hb1} {body1}")
        return
    rev_status, rev_body, _ = _request(
        "POST", f"{ctx.base}/api/nodes/licenses/{license_id}/revoke",
        token=ctx.jwt, body={}, timeout=ctx.timeout)
    if rev_status != 200:
        ctx.record("probe-4", False, "A5 license 吊销时效", f"吊销调用失败 HTTP {rev_status} {rev_body}")
        return
    hb2, _ = _heartbeat(ctx, node_token, fp)
    ctx.record("probe-4", hb2 == 401, "A5 license 吊销时效（心跳即死）",
               f"吊销后心跳 HTTP {hb2}（期望 401 license_revoked）")


def probe_5_forged_jwt(ctx: Ctx) -> None:
    """[probe-5] A6⑥：错密钥伪造的 HS256 root JWT 必须 401。"""
    forged = _forge_hs256_token(
        b"redteam-wrong-secret-0123456789abcdef",
        {"sub": "redteam-forged", "name": "redteam", "role": "root", "org": "",
         "account": "", "iat": int(time.time()), "exp": int(time.time()) + 3600})
    status, _, _ = _request(
        "GET", f"{ctx.base}/api/users", token=forged, timeout=ctx.timeout)
    ctx.record("probe-5", status == 401, "A6 伪造 JWT（错密钥 HS256 root）被拒",
               f"GET /api/users HTTP {status}（期望 401，签名校验 algorithms 钉死 HS256）")


def probe_6_static_get(ctx: Ctx) -> None:
    """[probe-6] A8⑧：静态站 GET 豁免不 401（登录页可达），/api 数据不出豁免面。"""
    root_status, _, _ = _request("GET", f"{ctx.base}/", timeout=ctx.timeout)
    api_status, _, _ = _request("GET", f"{ctx.base}/api/users", timeout=ctx.timeout)
    ok = root_status != 401 and api_status == 401
    detail = (f"GET / → HTTP {root_status}（404=无静态产物/200=SPA，均合法，唯独不得 401）；"
              f"GET /api/users（无凭据）→ HTTP {api_status}（期望 401）")
    if root_status == 401:
        detail += "——静态 GET 豁免回归：登录页被拦死（auth.py static_get 分叉失效）"
    elif api_status != 401:
        detail += "——特权数据从豁免面漏出（严重）"
    ctx.record("probe-6", ok, "A8 静态 GET 豁免不泄露数据", detail)


def probe_7_quota(ctx: Ctx) -> None:
    """[probe-7] A4④ 配额侧：max_nodes=1 已占 → 异指纹第二台注册 403。"""
    if not ctx.jwt:
        ctx.record("probe-7", None, "A4 配额闸（异指纹第二台 403）", "缺 root 凭据")
        return
    lic = _issue_license(ctx, max_nodes=1, note="redteam-probe-7")
    if lic is None:
        ctx.record("probe-7", False, "A4 配额闸（异指纹第二台 403）", "license 签发失败（前置）")
        return
    _, license_key = lic
    status1, reg1 = _register_node(ctx, license_key=license_key, fingerprint=_fingerprint("quota-a"))
    if status1 != 200:
        ctx.record("probe-7", False, "A4 配额闸（异指纹第二台 403）", f"第一台注册失败 HTTP {status1} {reg1}")
        return
    status2, body2 = _register_node(ctx, license_key=license_key, fingerprint=_fingerprint("quota-b"))
    ctx.record("probe-7", status2 == 403, "A4 配额闸（异指纹第二台 403）",
               f"第二台（不同指纹）HTTP {status2}（期望 403 quota exhausted）{body2 if status2 != 403 else ''}")


def probe_8_machine_channel(ctx: Ctx, *, explicit: bool) -> None:
    """[probe-8] A7⑦：机器通道双向——正确 BOK_CP_TOKEN 直通 root 面，错误值 401。"""
    if not ctx.cp_token:
        ctx.record("probe-8", None, "A7 机器通道（CP token）双向",
                   "未提供 --cp-token（自起模式自动注入；外部目标需显式给）")
        return
    ok_status, ok_body, _ = _request(
        "POST", f"{ctx.base}/api/nodes/licenses", token=ctx.cp_token,
        body={"max_nodes": 1, "note": "redteam-probe-8"}, timeout=ctx.timeout)
    bad_status, _, _ = _request(
        "GET", f"{ctx.base}/api/nodes/licenses",
        token="redteam-definitely-wrong-token", timeout=ctx.timeout)
    ok = ok_status == 200 and bad_status == 401
    detail = (f"正确 CP token 直通 root 专属 license 面 HTTP {ok_status}"
              f"（期望 200，state.machine 过 require_role，{str(ok_body.get('license_id') or ok_body)[:20]}）；"
              f"错误 token HTTP {bad_status}（期望 401）")
    if explicit and not ok:
        detail += "——门不在或钥匙不对"
    ctx.record("probe-8", ok, "A7 机器通道（CP token）双向", detail)


# ---- probe-9（二进制扫描，独立参数） ---------------------------------------

# 只匹配**密钥值形态**：字段名（node_token/license_key 作 JSON key/argparse 用法）
# 合法存在于产物（`docs/NODE_PACKAGING.md` import 面分析），裸字段名不算命中。
_SECRET_PATTERNS: tuple[tuple[str, bytes], ...] = (
    # license key 前缀（仅服务端 nodes_store.create_license 生成；产物里出现即嵌入密钥）
    ("license key 前缀 bokn_", rb"bokn_[A-Za-z0-9_\-]{16,}"),
    # 字段名 + 长字面量赋值（硬编码 token/key 常量的典型形状）
    ("硬编码 token/key 赋值",
     rb"(?:node_token|license_key|api_key|secret|password|cp_token)"
     rb"['\"]?\s*[:=]\s*['\"][A-Za-z0-9_+/=\-]{24,}['\"]"),
)


def probe_9_scan_binary(ctx: Ctx, path: Path) -> None:
    """[probe-9] A9⑨：PyInstaller 产物 strings 扫密钥值模式，命中=FAIL。"""
    if not path.is_file():
        ctx.record("probe-9", None, "A9 二进制静态密钥扫描",
                   f"{path} 不存在——跳过（可先跑 scripts/build_node_agent.sh 生成）")
        return
    blob = path.read_bytes()
    hits: list[str] = []
    for name, pattern in _SECRET_PATTERNS:
        for match in re.finditer(pattern, blob):
            excerpt = match.group(0)[:64].decode("ascii", errors="replace")
            hits.append(f"{name} @0x{match.start():x}: {excerpt}")
    ctx.record("probe-9", not hits, "A9 二进制静态密钥扫描（dist/node-agent）",
               f"扫描 {path.name}（{len(blob)} 字节，{len(_SECRET_PATTERNS)} 组密钥值模式）"
               + (f"；命中 {len(hits)} 处：{'; '.join(hits[:4])}" if hits else "；零命中（源码零硬编码）"))


# ---- 自起加固 CP（--self-host） -------------------------------------------


def _repo_pythonpath() -> str:
    parts = [
        REPO_ROOT / "packages" / "core",
        REPO_ROOT / "packages" / "business-db",
        REPO_ROOT / "packages" / "knowledge",
        REPO_ROOT / "packages" / "observability",
        REPO_ROOT / "apps" / "control-plane",
    ]
    return os.pathsep.join(str(p) for p in parts)


@dataclass
class SelfHost:
    proc: subprocess.Popen[bytes]
    root_user: str
    root_pass: str
    cp_token: str
    workdir: Path


def _spawn_self_host(workdir: Path) -> SelfHost:
    """在临时目录起加固 CP（:18015）：全部密钥随机生成、audit/静态目录落在临时目录。"""
    root_user = "redteam-root"
    root_pass = secrets.token_urlsafe(18)
    cp_token = secrets.token_urlsafe(24)
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{workdir / 'redteam.db'}",
        "BOK_AUTH_REQUIRED": "1",
        "BOK_JWT_SECRET": secrets.token_urlsafe(32),
        "BOK_CP_TOKEN": cp_token,
        "BOK_ROOT_USERNAME": root_user,
        "BOK_ROOT_PASSWORD": root_pass,
        # audit（HOME 相对）与 vault（cwd 相对）都隔离进临时目录，跑完即清。
        "HOME": str(workdir),
        "VAULT_ROOT": str(workdir / "vault"),
        "PYTHONPATH": _repo_pythonpath(),
        "PYTHONUNBUFFERED": "1",
    })
    log_path = workdir / "cp.log"
    log_file = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "control_plane.main:app",
         "--host", "127.0.0.1", "--port", str(SELF_HOST_PORT), "--log-level", "warning"],
        cwd=str(workdir), env=env, stdout=log_file, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_file.close()
            tail = log_path.read_text(errors="replace")[-800:]
            raise RuntimeError(f"CP 进程提前退出（code={proc.returncode}）\n{tail}")
        status, _, _ = _request("GET", f"{SELF_HOST_BASE}/health", timeout=2.0)
        if status == 200:
            log_file.close()
            return SelfHost(proc=proc, root_user=root_user, root_pass=root_pass,
                            cp_token=cp_token, workdir=workdir)
        time.sleep(0.3)
    log_file.close()
    tail = log_path.read_text(errors="replace")[-800:]
    raise RuntimeError(f"CP {READY_TIMEOUT_S:.0f}s 未就绪（/health 不通）\n{tail}")


def _stop_self_host(sh: SelfHost) -> None:
    if sh.proc.poll() is None:
        sh.proc.terminate()
        try:
            sh.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sh.proc.kill()
            sh.proc.wait(timeout=5)


# ---- 主流程 ----------------------------------------------------------------


def _run_probes(ctx: Ctx, *, root_user: str, root_pass: str, has_explicit_cp_token: bool) -> None:
    probe_1_open_register(ctx)
    probe_2_wrong_keys(ctx)
    # root 登录（探针 3/4/7/8 的前置；失败记 FAIL——授权目标上凭据不工作是信号）。
    if root_user:
        if _login_root(ctx, root_user, root_pass):
            ctx.record("login", True, "root 登录拿 JWT", "probe-3/4/7/8 前置就绪")
        else:
            ctx.record("login", False, "root 登录拿 JWT", "登录失败——probe-3/4/7 将 SKIP、probe-4 前置缺失")
    else:
        ctx.record("login", None, "root 登录拿 JWT", "未提供 --root-user/--root-pass——probe-3/4/7 SKIP")
    probe_3_token_theft(ctx)
    probe_4_revoke(ctx)
    probe_5_forged_jwt(ctx)
    probe_6_static_get(ctx)
    probe_7_quota(ctx)
    probe_8_machine_channel(ctx, explicit=has_explicit_cp_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bok CP 认证/节点面红队探针（只打加固 CP；绝不打 :8000 开发栈）")
    parser.add_argument(
        "--base-url", default="",
        help="目标加固 CP 基址（如 https://cp.example.com）；--self-host 时可省略")
    parser.add_argument(
        "--self-host", action="store_true",
        help=f"自起加固 CP 实例（{SELF_HOST_BASE} + 临时 sqlite + 随机密钥），跑完即清理")
    parser.add_argument("--root-user", default="", help="root 用户名（外部目标探针 3/4/7 需要）")
    parser.add_argument("--root-pass", default="", help="root 密码（与 --root-user 成对）")
    parser.add_argument("--cp-token", default="", help="BOK_CP_TOKEN 值（probe-8；自起模式自动注入）")
    parser.add_argument("--scan-binary", default="", help="对二进制产物跑密钥扫描（probe-9，如 dist/node-agent）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单请求超时秒数")
    args = parser.parse_args(argv)
    if bool(args.root_user) != bool(args.root_pass):
        parser.error("--root-user/--root-pass 必须成对提供")
    if not args.self_host and not args.base_url:
        parser.error("--base-url 必填（或用 --self-host 自起实例）")
    base = (args.base_url or SELF_HOST_BASE).rstrip("/")
    if base.rstrip("/").endswith(":8000"):
        print("拒绝：:8000 是本机开发栈，红队探针不对它运行。", file=sys.stderr)
        return 2

    ctx = Ctx(base=base, timeout=args.timeout, cp_token=args.cp_token)
    start = time.monotonic()
    if args.self_host:
        with tempfile.TemporaryDirectory(prefix="bok-redteam-") as td:
            workdir = Path(td)
            try:
                sh = _spawn_self_host(workdir)
            except RuntimeError as exc:
                print(f"[FAIL] [self-host] 自起 CP 失败 —— {exc}", flush=True)
                return 1
            try:
                print(f"[info] 自起加固 CP {SELF_HOST_BASE}（临时库 {workdir / 'redteam.db'}）", flush=True)
                if not args.cp_token:
                    ctx.cp_token = sh.cp_token  # 自起实例的机器钥匙自动注入 probe-8
                _run_probes(ctx, root_user=sh.root_user, root_pass=sh.root_pass,
                            has_explicit_cp_token=bool(args.cp_token))
            finally:
                _stop_self_host(sh)
    else:
        _run_probes(ctx, root_user=args.root_user, root_pass=args.root_pass,
                    has_explicit_cp_token=bool(args.cp_token))
    if args.scan_binary:
        probe_9_scan_binary(ctx, Path(args.scan_binary))

    failed = [r for r in ctx.results if r.ok is False]
    passed = [r for r in ctx.results if r.ok is True]
    skipped = [r for r in ctx.results if r.ok is None]
    print(flush=True)
    print(f"REDTEAM {'FAIL' if failed else 'PASS'}："
          f"{len(passed)} PASS / {len(failed)} FAIL / {len(skipped)} SKIP"
          f"（耗时 {time.monotonic() - start:.1f}s）", flush=True)
    if failed:
        print("失败项：" + "; ".join(f"[{r.probe}] {r.label}" for r in failed), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
