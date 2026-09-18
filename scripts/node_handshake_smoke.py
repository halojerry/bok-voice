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
    raw: bytes | None = None,
    content_type: str = "application/json",
    timeout: float = 10.0,
) -> tuple[int, Any]:
    """返回 (http_status, 解析后的 JSON 体)；连接级失败 status=0。

    raw=非 JSON 体（W2 日志束 gzip 直传），content_type 随体声明。"""
    if raw is not None:
        data = raw
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None
    headers = {"Content-Type": content_type}
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

    # ② 真 token 心跳（P2-7 心跳指纹协议强制：license 流注册绑定了指纹，
    # 心跳必须带同指纹——缺=按 fingerprint_mismatch 自动吊销；开放流无绑定，
    # 带了也不参与校验）。
    hb_body: dict[str, Any] = {"metrics": {"smoke": ts}}
    if license_used:
        hb_body["fingerprint"] = fingerprint
    status, body = _request(
        "POST", f"{base}/api/nodes/heartbeat",
        token=node_token, body=hb_body, timeout=args.timeout,
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
        # 重注册=换新 token（旧 token 随覆盖失效）——后续 ⑦⑧ 步必须拿新 token，
        # 否则心跳全 401（CI 首跑实爆，2026-09-18）。
        new_token = str(reg2.get("node_token") or "")
        if status == 200 and new_token:
            node_token = new_token
    else:
        print("[skip] ⑥ 非 license 流（auth-off CP），跳过克隆/复活断言", flush=True)

    # ⑦ commands 通道（P3，2026-09-17）：入队 → 心跳领走 → version 收敛关单。
    target_ver = f"smoke-{ts}"
    if jwt and node_id and node_token:
        status, body = _request(
            "POST", f"{base}/api/nodes/{node_id}/commands", token=jwt,
            body={"action": "update", "version": target_ver}, timeout=args.timeout)
        cmd_ok = status == 200 and isinstance(body, dict) and str(body.get("id", "")).startswith("cmd-")
        cmd_id = str(body.get("id") or "") if isinstance(body, dict) else ""
        _record(cmd_ok, "⑦a root 入队 update 指令", f"HTTP {status} {body}")
        if cmd_ok:
            hb_body2: dict[str, Any] = {"metrics": {}}
            if license_used:
                hb_body2["fingerprint"] = fingerprint
            status, body = _request(
                "POST", f"{base}/api/nodes/heartbeat", token=node_token,
                body=hb_body2, timeout=args.timeout)
            got = body.get("commands", []) if isinstance(body, dict) else []
            _record(
                status == 200 and any(str(c.get("id")) == cmd_id for c in got),
                "⑦b 心跳领走指令（pending→delivered）",
                f"HTTP {status} commands={[c.get('action') for c in got]}",
            )
            status, body = _request(
                "POST", f"{base}/api/nodes/heartbeat", token=node_token,
                body={**hb_body2, "version": target_ver}, timeout=args.timeout)
            status2, body2 = _request(
                "GET", f"{base}/api/nodes/{node_id}/commands", token=jwt,
                timeout=args.timeout)
            ledger = body2.get("commands", []) if isinstance(body2, dict) else []
            row = next((c for c in ledger if str(c.get("id")) == cmd_id), {})
            _record(
                status == 200 and status2 == 200 and row.get("status") == "done",
                "⑦c 心跳 version 收敛 → 指令自动关单 done",
                f"HTTP {status}/{status2} status={row.get('status')}",
            )
    else:
        print("[skip] ⑦ 无 root 凭据/节点身份，跳过 commands 通道断言", flush=True)

    # ⑧ 工件下载鉴权（去 GitHub 化交付链）：无凭据 401、有凭据缺工件 404——
    # 401 与 404 的区分证明「鉴权在端点内真实发生」而非路径直达。
    dl_url = f"{base}/api/nodes/downloads/pkg/latest/bok-node-latest.tar.gz"
    status, _body = _request("GET", dl_url, timeout=args.timeout)
    _record(status == 401, "⑧a 无凭据工件下载被拒", f"HTTP {status}（期望 401）")
    status, _body = _request(
        "GET", dl_url, token=node_token or f"smoke-fake-{ts}", timeout=args.timeout)
    if node_token:
        _record(status == 404, "⑧b node_token 过鉴权（缺工件=404 非 401）",
                f"HTTP {status}")
    else:
        print("[skip] ⑧b 无节点 token（开放流裸注册失败时），跳过", flush=True)

    # ⑨ 远程日志通道（W2，2026-09-18）：node_token 上传 gzip 束 → root 清单可见。
    # 与 ⑧ 同理证明「豁免表不等于无门」——上传端点内 node_token 闸真实在岗。
    if node_token:
        import gzip as _gzip

        payload = _gzip.compress(f"[smoke] log bundle {ts}".encode())
        up_status, up_body = _request(
            "POST", f"{base}/api/nodes/logs", token=node_token,
            raw=payload, content_type="application/gzip", timeout=args.timeout)
        listed: list = []
        if up_status == 200 and jwt and node_id:
            status, body = _request(
                "GET", f"{base}/api/nodes/{node_id}/logs", token=jwt,
                timeout=args.timeout)
            if status == 200 and isinstance(body, list):
                listed = body
        _record(up_status == 200 and isinstance(up_body, dict)
                and up_body.get("ok") is True and (not jwt or len(listed) >= 1),
                "⑨ 节点日志上报→root 清单可见",
                f"HTTP {up_status} files={len(listed)}")
    else:
        print("[skip] ⑨ 无节点 token，跳过日志通道断言", flush=True)

    failed = [label for ok, label in _RESULTS if not ok]
    print(flush=True)
    if failed:
        print(f"SMOKE FAIL：{len(failed)}/{len(_RESULTS)} 步失败 -> {'; '.join(failed)}", flush=True)
        return 1
    print(f"SMOKE PASS：{len(_RESULTS)}/{len(_RESULTS)} 步全过", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
