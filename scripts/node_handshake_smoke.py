"""节点握手链路冒烟（P1）：对一个已起 CP 全验 register → heartbeat → 鉴权 → 列表。

用法:
  python scripts/node_handshake_smoke.py --base-url http://127.0.0.1:18011 \
      [--root-user root --root-pass ***] [--timeout 10]

步骤（每步打 PASS/FAIL，任一 FAIL → exit 1）:
  ④a （可选）--root-user/--root-pass 提供时先登录拿 root JWT——auth-on CP 的
      register/license/列表都在 identity 门禁内，带 JWT 才能同时覆盖
      auth-on/auth-off 两种 CP 形态；
  ①  POST /api/nodes/register（name=smoke-<ts>）拿 node_id/node_token；
      加固模式裸注册 401 → ①a root 签发 license → 带 key+机器指纹注册；
  ⑥  license 流专属：克隆指纹心跳被拒（fingerprint_mismatch 自动吊销）+
      同指纹重注册幂等复用 node_id（恢复路径）；
  ②  node_token 心跳 → 200 且 ok:true；
  ③  伪造 token 心跳 → 401；
  ④b GET /api/nodes（root JWT）列表含该 node_id；
  ⑤  注册响应含 heartbeat_interval_s（>0）。

纯 stdlib（urllib）零三方依赖。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

_RESULTS: list[tuple[bool, str]] = []


def _record(ok: bool, label: str, detail: str = "") -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {label}"
    if detail:
        line += f" —— {detail}"
    print(line, flush=True)
    _RESULTS.append((ok, label))


def _request(
    method: str,
    url: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    """返回 (http_status, 解析后的 JSON 体)；连接级失败 status=0。"""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "null")
        except Exception:
            payload = None
        return exc.code, payload
    except Exception as exc:  # URLError/连接拒绝等——当步 FAIL，不带崩整个脚本
        return 0, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bok 节点握手链路冒烟")
    parser.add_argument("--base-url", required=True, help="已起 CP 的基址，如 http://127.0.0.1:18011")
    parser.add_argument("--root-user", default="", help="可选：root 用户名（与 --root-pass 成对）")
    parser.add_argument("--root-pass", default="", help="可选：root 密码（与 --root-user 成对）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单请求超时秒数")
    args = parser.parse_args(argv)
    if bool(args.root_user) != bool(args.root_pass):
        parser.error("--root-user/--root-pass 必须成对提供")
    base = args.base_url.rstrip("/")
    ts = int(time.time())

    # ④a root 登录（可选前置；auth-on CP 必需，auth-off CP 带上也无害）
    jwt = ""
    if args.root_user:
        status, body = _request(
            "POST", f"{base}/api/auth/login",
            body={"username": args.root_user, "password": args.root_pass},
            timeout=args.timeout,
        )
        ok = status == 200 and isinstance(body, dict) and bool(body.get("token"))
        if ok:
            jwt = str(body["token"])
        _record(ok, "④a root 登录拿 JWT", f"HTTP {status}" + ("" if ok else f" {body}"))
    else:
        print("[skip] ④ 未提供 --root-user/--root-pass，跳过节点列表确认", flush=True)

    # ① 注册：签发 node_id/node_token（明文只在本次响应出现一次）。
    # P1 节点鉴权：加固模式（auth-on / CP token）裸注册 401 → 有 root 凭据时
    # 走完整 license 流（root 签发 → 带 key+指纹注册），auth-off CP 维持裸注册。
    import hashlib

    fingerprint = hashlib.sha256(f"smoke-fp-{ts}".encode()).hexdigest()
    license_used = ""
    reg_body: dict[str, Any] = {
        "name": f"smoke-{ts}", "platform": sys.platform, "version": "smoke"}
    status, body = _request(
        "POST", f"{base}/api/nodes/register", token=jwt, body=reg_body,
        timeout=args.timeout,
    )
    if status in (401, 403) and jwt:
        lic_status, lic_body = _request(
            "POST", f"{base}/api/nodes/licenses", token=jwt,
            body={"max_nodes": 2, "note": f"handshake-smoke-{ts}"}, timeout=args.timeout)
        license_key = str(lic_body.get("license_key") or "") if isinstance(lic_body, dict) else ""
        _record(
            lic_status == 200 and bool(license_key),
            "①a 加固模式：root 签发节点 license",
            f"HTTP {lic_status}" + ("" if license_key else f" {lic_body}"),
        )
        reg_body.update({"license_key": license_key, "fingerprint": fingerprint})
        license_used = license_key
        status, body = _request(
            "POST", f"{base}/api/nodes/register", token=jwt, body=reg_body,
            timeout=args.timeout,
        )
    reg: dict[str, Any] = body if isinstance(body, dict) else {}
    node_id = str(reg.get("node_id") or "")
    node_token = str(reg.get("node_token") or "")
    _record(
        status == 200 and node_id.startswith("node-") and bool(node_token),
        f"① 注册拿 node_id/node_token（name=smoke-{ts}，{'license 流' if license_used else '开放流'}）",
        f"HTTP {status} node_id={node_id or body}",
    )

    # ② 真 token 心跳
    status, body = _request(
        "POST", f"{base}/api/nodes/heartbeat",
        token=node_token, body={"metrics": {"smoke": ts}}, timeout=args.timeout,
    )
    _record(
        status == 200 and isinstance(body, dict) and body.get("ok") is True,
        "② node_token 心跳",
        f"HTTP {status} {body}",
    )

    # ③ 伪造 token 心跳必须被拒
    status, body = _request(
        "POST", f"{base}/api/nodes/heartbeat",
        token=f"smoke-fake-{ts}", body={"metrics": {}}, timeout=args.timeout,
    )
    _record(status == 401, "③ 伪造 token 心跳被拒", f"HTTP {status}（期望 401）{body if status != 401 else ''}")

    # ④b root JWT 查节点列表，确认本节点已入册
    if args.root_user:
        if jwt:
            status, rows = _request("GET", f"{base}/api/nodes", token=jwt, timeout=args.timeout)
            ids = [str(r.get("node_id")) for r in rows] if isinstance(rows, list) else []
            _record(
                status == 200 and node_id in ids,
                "④ GET /api/nodes 列表含本节点",
                f"HTTP {status} 共 {len(ids)} 节点 命中={node_id in ids}",
            )
        # jwt 为空时 ④a 已记 FAIL，此处不再重复记
        else:
            print("[skip] ④b 登录失败，跳过节点列表确认", flush=True)

    # ⑤ 注册响应带心跳间隔约定
    interval = reg.get("heartbeat_interval_s")
    _record(
        isinstance(interval, int) and not isinstance(interval, bool) and interval > 0,
        "⑤ 注册响应含 heartbeat_interval_s",
        f"={interval!r}",
    )

    # ⑥ license 流专属：克隆检测 + 幂等复活（P1 机器码鉴权闭环）。
    if license_used and node_token:
        wrong_fp = hashlib.sha256(f"smoke-fp-clone-{ts}".encode()).hexdigest()
        status, body = _request(
            "POST", f"{base}/api/nodes/heartbeat", token=node_token,
            body={"metrics": {}, "fingerprint": wrong_fp}, timeout=args.timeout)
        _record(
            status == 401,
            "⑥a 克隆指纹心跳被拒（fingerprint_mismatch 自动吊销）",
            f"HTTP {status}（期望 401）{body if status != 401 else ''}",
        )
        # 原指纹重注册：幂等复活同一 node_id（换新 token 的恢复路径）。
        status, body = _request(
            "POST", f"{base}/api/nodes/register", token=jwt, body=reg_body,
            timeout=args.timeout)
        reg2 = body if isinstance(body, dict) else {}
        _record(
            status == 200 and str(reg2.get("node_id")) == node_id,
            "⑥b 同指纹重注册幂等复用 node_id",
            f"HTTP {status} {reg2.get('node_id') or body}（期望 {node_id}）",
        )
    else:
        print("[skip] ⑥ 非 license 流（auth-off CP），跳过克隆/复活断言", flush=True)

    failed = [label for ok, label in _RESULTS if not ok]
    print(flush=True)
    if failed:
        print(f"SMOKE FAIL：{len(failed)}/{len(_RESULTS)} 步失败 -> {'; '.join(failed)}", flush=True)
        return 1
    print(f"SMOKE PASS：{len(_RESULTS)}/{len(_RESULTS)} 步全过", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
