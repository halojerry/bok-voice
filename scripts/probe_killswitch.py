"""远程停机开关（killswitch）目标语义探针：把「吊销节点=即刻断供云端」的
目标契约钉成可跑的红绿基线。

背景:
  2026-09-16 站点交付深测结论：节点吊销今天只是注册表标记位——被吊销节点
  同指纹重注册可原地复活（⑤）、云端通话面（建单/发 token）完全感知不到
  吊销（⑥）、心跳 401 体只是纯文本、没有可执行的 shutdown 行动指令（⑦）、
  吊销不可逆且没有解除路径（⑧）。本探针按**目标语义**断言：⑤⑥⑦⑧ 今天
  刻意红——红是现状未实装的正确写照，对应里程碑实装后自动转绿；
  不要为了转绿放宽断言，更不要在 CP 侧凑断言。

用法:
  python scripts/probe_killswitch.py --base-url http://127.0.0.1:18099 \
      [--root-user root --root-pass ***] [--timeout 10]
  加固档 CP（BOK_AUTH_REQUIRED=1 / BOK_CP_TOKEN 已设）必须带
  --root-user/--root-pass——license 签发、节点吊销/解除都是 root 面。

env（CP 侧相关；探针自身无专用 env）:
  BOK_AUTH_REQUIRED=1        CP 加固档：注册走 license 闸（探针自动切换流程）
  LIVEKIT_API_KEY/SECRET     CP 缺省时 /api/token 返 503——⑥ 的 token 腿自动
                             [skip]（环境限制，非节点语义）
  BOK_ROOT_USERNAME/PASSWORD 加固档 root 种子（与 --root-user/--root-pass 配套）

前置:
  已起 CP（python tools/bok.py serve，或 apps/control-plane 下裸起
  uvicorn control_plane.main:app）。CP 不可达 exit 2；任一步 FAIL exit 1。

步骤（每步打 PASS/FAIL，任一 FAIL → exit 1）:
  ⓪  （可选）root 登录拿 JWT——加固档 license/吊销面必需
  ①  注册拿 node_id/node_token（加固档：root 签 license → 带 key+指纹注册）
  ②  node_token 心跳 200
  ②b 完好性基线：节点在线时建最小通话 + 取房 token 成功（给 ⑥ 一个对照）
  ③  root 吊销节点 POST /api/nodes/{id}/revoke
  ④  心跳 401 且 detail 提及 revoked
  ⑤  sticky：同指纹重注册被拒 401（仅加固档；今天原地复活 200 → 红，刻意；
      开放流 [skip]——auth-off 本机单机信任不属 killswitch 威胁模型）
  ⑥  云端窒息点：revoked 节点打 POST /api/calls 与 /api/token 返 403（今天 → 红，刻意）
  ⑦  心跳 401 体 detail 携带机器可执行 action:"shutdown"（结构断言，两模式都跑；
      今天纯文本 → 红，刻意）
  ⑧  root POST /api/nodes/{id}/unrevoke 解除后重注册复活（仅加固档；今天 404 →
      红，刻意；开放流注册本不复用 node_id，[skip]）
  ⑨  license 吊销=永久：吊销 license 后同指纹/新指纹注册均 401 无 self-heal
      （仅加固档；开放流 CP 不拦 license 注册，[skip]）

纯 stdlib（urllib/hashlib/argparse）零三方依赖。
"""
from __future__ import annotations

import argparse
import hashlib
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


def _skip(label: str, detail: str = "") -> None:
    line = f"[skip] {label}"
    if detail:
        line += f" —— {detail}"
    print(line, flush=True)


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


def _mentions(body: Any, word: str) -> bool:
    """体里（含嵌套 detail 字典/纯文本）是否提及某词（大小写不敏感）。"""
    try:
        return word in json.dumps(body, ensure_ascii=False).lower()
    except Exception:
        return False


def _has_shutdown_action(body: Any) -> bool:
    """结构断言（⑦）：401 体必须携带**机器可执行**的行动指令，不是给人看的
    纯文本。首选 detail 为 dict 且 action=="shutdown"；序列化体退路至少同时
    含 "action" 与 "shutdown" 两个标记——"node revoked; please shutdown" 这类
    纯文本缺 "action"，不得假绿。"""
    if isinstance(body, dict):
        d = body.get("detail")
        if isinstance(d, dict) and str(d.get("action", "")).lower() == "shutdown":
            return True
    try:
        s = json.dumps(body, ensure_ascii=False).lower()
    except Exception:
        return False
    return "action" in s and "shutdown" in s


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bok 远程停机开关（killswitch）目标语义探针")
    parser.add_argument("--base-url", required=True, help="已起 CP 的基址，如 http://127.0.0.1:18099")
    parser.add_argument("--root-user", default="", help="可选：root 用户名（与 --root-pass 成对；加固档必需）")
    parser.add_argument("--root-pass", default="", help="可选：root 密码（与 --root-user 成对）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单请求超时秒数")
    args = parser.parse_args(argv)
    if bool(args.root_user) != bool(args.root_pass):
        parser.error("--root-user/--root-pass 必须成对提供")
    base = args.base_url.rstrip("/")
    ts = int(time.time())

    # 预检：CP 不可达是环境缺失（exit 2），不算探针失败。
    status, _ = _request("GET", f"{base}/health", timeout=args.timeout)
    if status == 0:
        print(f"KILLSWITCH PROBE：CP 不可达（{base}）——先起 CP 再跑探针", flush=True)
        return 2

    # ⓪ root 登录（可选前置；加固档 license/吊销面必需，auth-off CP 带上也无害）。
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
        _record(ok, "⓪ root 登录拿 JWT", f"HTTP {status}" + ("" if ok else f" {body}"))
    else:
        _skip("⓪ 未提供 --root-user/--root-pass，root 面步骤按开放流处理")

    # ① 注册：加固档（auth-on / CP token）裸注册 401 → 有 root 凭据时走完整
    # license 流（root 签发 → 带 key+指纹注册），开放流维持裸注册。
    # 指纹两模式都带上（开放端点今天收下但只存档不参与复用，行为零变化）——
    # ⑤⑧ 的同指纹语义才有载体（复用需 license_id+指纹同时在场）。
    fingerprint = hashlib.sha256(f"killswitch-fp-{ts}".encode()).hexdigest()
    reg_body: dict[str, Any] = {
        "name": f"killswitch-probe-{ts}", "platform": sys.platform,
        "version": "probe", "fingerprint": fingerprint}
    status, body = _request(
        "POST", f"{base}/api/nodes/register", token=jwt, body=reg_body,
        timeout=args.timeout,
    )
    license_used = ""
    register_recorded = False
    if status in (401, 403) and jwt:
        lic_status, lic_body = _request(
            "POST", f"{base}/api/nodes/licenses", token=jwt,
            body={"max_nodes": 2, "note": f"killswitch-probe-{ts}"}, timeout=args.timeout)
        license_key = str(lic_body.get("license_key") or "") if isinstance(lic_body, dict) else ""
        _record(
            lic_status == 200 and bool(license_key),
            "①a 加固档：root 签发节点 license",
            f"HTTP {lic_status}" + ("" if license_key else f" {lic_body}"),
        )
        reg_body.update({"license_key": license_key})
        license_used = license_key
        status, body = _request(
            "POST", f"{base}/api/nodes/register", token=jwt, body=reg_body,
            timeout=args.timeout,
        )
    elif status in (401, 403):
        _record(False, "① 注册拿 node_id/node_token",
                f"HTTP {status} {body} —— 加固档 CP 需要 --root-user/--root-pass 走 license 流")
        register_recorded = True
    reg: dict[str, Any] = body if isinstance(body, dict) else {}
    node_id = str(reg.get("node_id") or "")
    node_token = str(reg.get("node_token") or "")
    if not register_recorded:
        _record(
            status == 200 and node_id.startswith("node-") and bool(node_token),
            f"① 注册拿 node_id/node_token（name=killswitch-probe-{ts}，"
            f"{'license 流' if license_used else '开放流'}）",
            f"HTTP {status} node_id={node_id or body}",
        )

    # 注册失败=后续步骤失去载体：逐个 [skip] 后汇总 FAIL（exit 1）。
    if not node_id or not node_token:
        for label in ("② node_token 心跳",
                      "②b 完好性基线：在线建通话+取 token 成功",
                      "③ root 吊销节点",
                      "④ 吊销后心跳 401（detail 提及 revoked）",
                      "⑤ sticky：同指纹重注册被拒 401",
                      "⑥ 云端窒息点：revoked 节点建单/token 返 403",
                      "⑦ 心跳 401 detail 含 action:shutdown",
                      "⑧ unrevoke 解除后重注册复活"):
            _skip(label, "① 未拿到节点")
        if license_used:
            _skip("⑨ license 吊销=永久（注册 401 无 self-heal）", "① 未拿到节点")
        failed = [label for ok, label in _RESULTS if not ok]
        print(flush=True)
        print(f"KILLSWITCH PROBE FAIL：{len(failed)}/{len(_RESULTS)} -> {'; '.join(failed)}", flush=True)
        return 1

    created_calls: list[str] = []

    # ② 真 token 心跳（注册绑定了指纹 → 心跳必须带同指纹，否则按协议自动吊销）。
    status, body = _request(
        "POST", f"{base}/api/nodes/heartbeat", token=node_token,
        body={"metrics": {"probe": ts}, "fingerprint": fingerprint},
        timeout=args.timeout,
    )
    _record(
        status == 200 and isinstance(body, dict) and body.get("ok") is True,
        "② node_token 心跳",
        f"HTTP {status} {body}",
    )

    # ②b 完好性基线：节点在线时云端通话面畅通（建最小通话 + 取房 token）。
    # 这步证明的是 CP 自身能建单/发 token，给 ⑥ 的窒息断言一个对照；
    # token 腿 503 = CP 未配 LiveKit 凭据（环境限制），不拖垮本步，
    # 但 ⑥ 的 token 腿将一并 [skip]。
    call_status, call_body = _request(
        "POST", f"{base}/api/calls", token=jwt,
        body={"account_id": "acc-001"}, timeout=args.timeout)
    call_id = str(call_body.get("id") or "") if isinstance(call_body, dict) else ""
    if call_id:
        created_calls.append(call_id)
    room = call_id or f"killswitch-probe-{ts}"
    tok_status, tok_body = _request(
        "POST", f"{base}/api/token", token=jwt,
        body={"room_name": room}, timeout=args.timeout)
    creds_ok = tok_status in (200, 201)
    sanity_ok = call_status in (200, 201) and bool(call_id) and (
        creds_ok or tok_status == 503)
    detail = (f"calls HTTP {call_status} id={call_id or call_body}；"
              f"token HTTP {tok_status}")
    if tok_status == 503:
        detail += "（CP 未配 LiveKit 凭据，⑥ token 腿将 [skip]）"
    _record(sanity_ok, "②b 完好性基线：在线建通话+取 token 成功", detail)

    # ③ root 吊销节点（auth-off CP 无身份直通；auth-on 需 root JWT）。
    status, body = _request(
        "POST", f"{base}/api/nodes/{node_id}/revoke", token=jwt,
        timeout=args.timeout)
    _record(
        status in (200, 201) and isinstance(body, dict) and body.get("revoked") is True,
        "③ root 吊销节点 POST /api/nodes/{id}/revoke",
        f"HTTP {status} {body}",
    )

    # ④ 吊销即刻生效：心跳 401 且 detail 提及 revoked。
    status, body = _request(
        "POST", f"{base}/api/nodes/heartbeat", token=node_token,
        body={"metrics": {}, "fingerprint": fingerprint}, timeout=args.timeout)
    _record(
        status == 401 and _mentions(body, "revoked"),
        "④ 吊销后心跳 401（detail 提及 revoked）",
        f"HTTP {status} {body}",
    )

    # ⑤ sticky：同指纹重注册必须被拒（401）——吊销不能被原机注册自愈绕过。
    # 仅加固档断言（scope 裁决）：killswitch sticky 是加固档属性，auth-off 本机
    # 单机信任模型不属威胁范围（B4 先例）。今天加固档 register 幂等复活同一
    # node_id 并换发新 token（200）→ 刻意红。
    if license_used:
        status, body = _request(
            "POST", f"{base}/api/nodes/register", token=jwt, body=reg_body,
            timeout=args.timeout)
        reg2 = body if isinstance(body, dict) else {}
        same_node = str(reg2.get("node_id") or "") == node_id
        diag = ""
        if status == 200:
            diag = ("同 node_id 原地复活并换发新 token=目标未实装" if same_node
                    else "新建节点注册=目标未实装")
        _record(
            status == 401,
            "⑤ sticky：同指纹重注册被拒 401",
            f"HTTP {status} node_id={reg2.get('node_id') or body}（期望 401）{diag}",
        )
    else:
        _skip("⑤ sticky：同指纹重注册被拒 401",
              "开放流（auth-off）不属 killswitch 威胁模型（本机单机信任，B4 先例），"
              "sticky 语义仅加固档断言")

    # ⑥ 云端窒息点：revoked 节点的凭据打建单/发 token 必须被 403 拒——
    # 被吊销节点不得再消耗云端通话面。今天 CP 完全不感知节点状态 → 刻意红。
    c_status, c_body = _request(
        "POST", f"{base}/api/calls", token=node_token,
        body={"account_id": "acc-001"}, timeout=args.timeout)
    if isinstance(c_body, dict) and c_body.get("id"):
        created_calls.append(str(c_body["id"]))
    t_status, t_body = _request(
        "POST", f"{base}/api/token", token=node_token,
        body={"room_name": room}, timeout=args.timeout)
    choke_ok = c_status == 403 and (not creds_ok or t_status == 403)
    detail = (f"calls HTTP {c_status}（目标 403）；token HTTP {t_status}（目标 403）")
    if not creds_ok:
        detail += "；token 腿 [skip]：CP 未配 LiveKit 凭据，无法表达 403 语义"
    _record(choke_ok, "⑥ 云端窒息点：revoked 节点建单/token 返 403", detail)

    # ⑦ 行动指令（两模式都断言——心跳 401 拒绝路径与模式无关）：401 体 detail
    # 必须携带机器可执行的 action:"shutdown" 指令（结构断言，见
    # _has_shutdown_action），纯文本提及 shutdown 不算。今天 detail="node
    # revoked" → 刻意红。
    status, body = _request(
        "POST", f"{base}/api/nodes/heartbeat", token=node_token,
        body={"metrics": {}, "fingerprint": fingerprint}, timeout=args.timeout)
    _record(
        status == 401 and _has_shutdown_action(body),
        "⑦ 心跳 401 detail 含 action:shutdown",
        f"HTTP {status} {body}",
    )

    # ⑧ 受控解除（仅加固档）：root 专用 unrevoke，解除后同指纹重注册才允许
    # 复活——恢复路径必须经 root 显式操作，而非注册端点自愈。开放流注册本就
    # 不复用 node_id（复用需 license_id+指纹同时在场），无死行可解 → [skip]。
    # 今天加固档 unrevoke 404 → 刻意红（「未解除前不得复活」已由 ⑤ 钉住）。
    if license_used:
        u_status, u_body = _request(
            "POST", f"{base}/api/nodes/{node_id}/unrevoke", token=jwt,
            timeout=args.timeout)
        unrevoke_ok = u_status in (200, 201)
        detail = f"HTTP {u_status} {u_body}（期望 2xx）"
        if unrevoke_ok:
            r_status, r_body = _request(
                "POST", f"{base}/api/nodes/register", token=jwt, body=reg_body,
                timeout=args.timeout)
            reg3 = r_body if isinstance(r_body, dict) else {}
            revive_ok = r_status == 200 and str(reg3.get("node_id") or "") == node_id
            detail += f"；解除后重注册 HTTP {r_status} node_id={reg3.get('node_id') or r_body}"
            _record(unrevoke_ok and revive_ok, "⑧ unrevoke 解除后重注册复活", detail)
            node_token = str(reg3.get("node_token") or node_token)  # 复活换发新 token
        else:
            _record(False, "⑧ unrevoke 解除后重注册复活",
                    detail + "（未解除前不得复活已由⑤钉住）")
    else:
        _skip("⑧ unrevoke 解除后重注册复活",
              "开放流注册本不复用 node_id（复用需 license_id+指纹），"
              "unrevoke/复活契约仅加固档有意义")

    # ⑨ license 吊销=永久（仅加固档）：吊销 license 后，同指纹重注册与新指纹
    # 首注都 401——license 没有 self-heal 出路。开放流 CP 不拦 license 注册，
    # 此步无意义 → [skip]。
    if license_used:
        lic2_status, lic2 = _request(
            "POST", f"{base}/api/nodes/licenses", token=jwt,
            body={"max_nodes": 1, "note": f"killswitch-probe-licrev-{ts}"},
            timeout=args.timeout)
        lic2d = lic2 if isinstance(lic2, dict) else {}
        key2 = str(lic2d.get("license_key") or "")
        lid2 = str(lic2d.get("license_id") or "")
        ok = lic2_status == 200 and bool(key2) and bool(lid2)
        detail = f"license 签发 HTTP {lic2_status}"
        fp_b = hashlib.sha256(f"killswitch-fp-b-{ts}".encode()).hexdigest()
        reg_b: dict[str, Any] = {
            "name": f"killswitch-probe-b-{ts}", "platform": sys.platform,
            "version": "probe", "license_key": key2, "fingerprint": fp_b}
        nb_status, nb_body = _request(
            "POST", f"{base}/api/nodes/register", token=jwt, body=reg_b,
            timeout=args.timeout) if ok else (0, "license 签发失败")
        ok = ok and nb_status == 200
        detail += f"；node-B 注册 HTTP {nb_status}"
        rv_status, rv_body = _request(
            "POST", f"{base}/api/nodes/licenses/{lid2}/revoke", token=jwt,
            timeout=args.timeout) if ok else (0, "前置失败")
        ok = ok and rv_status in (200, 201)
        detail += f"；license 吊销 HTTP {rv_status}"
        # 同指纹重注册：license 已吊销 → 401（今天 register_licensed 先查
        # license status → 401 "license revoked"，本就绿）。
        rr_status, rr_body = _request(
            "POST", f"{base}/api/nodes/register", token=jwt, body=reg_b,
            timeout=args.timeout) if ok else (0, "前置失败")
        sticky_ok = rr_status == 401 and _mentions(rr_body, "revoked")
        detail += f"；同指纹重注册 HTTP {rr_status}（期望 401）{rr_body if rr_status != 401 else ''}"
        # 新指纹首注：换台机器也救不回——401（无 self-heal 出路）。
        reg_b2 = dict(reg_b, fingerprint=hashlib.sha256(
            f"killswitch-fp-b2-{ts}".encode()).hexdigest())
        nf_status, nf_body = _request(
            "POST", f"{base}/api/nodes/register", token=jwt, body=reg_b2,
            timeout=args.timeout) if ok else (0, "前置失败")
        fresh_ok = nf_status == 401 and _mentions(nf_body, "revoked")
        detail += f"；新指纹首注 HTTP {nf_status}（期望 401）{nf_body if nf_status != 401 else ''}"
        _record(ok and sticky_ok and fresh_ok,
                "⑨ license 吊销=永久（注册 401 无 self-heal）", detail)
    else:
        _skip("⑨ license 吊销=永久（注册 401 无 self-heal）",
              "开放流 CP（无 license 闸）不拦 license 注册，⑨ 仅加固档有意义")

    # 尽力清理探针建的通话（不记分；失败不影响结论）。
    for cid in created_calls:
        _request("POST", f"{base}/api/calls/{cid}/hangup", token=jwt,
                 body={}, timeout=args.timeout)

    failed = [label for ok, label in _RESULTS if not ok]
    print(flush=True)
    if failed:
        print(f"KILLSWITCH PROBE FAIL：{len(failed)}/{len(_RESULTS)} 步失败 -> {'; '.join(failed)}", flush=True)
        return 1
    print(f"KILLSWITCH PROBE PASS：{len(_RESULTS)}/{len(_RESULTS)} 步全过", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
