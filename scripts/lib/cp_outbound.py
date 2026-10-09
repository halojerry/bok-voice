"""诊断脚本 CP 出站共享单点（Mimosa L3 收敛，2026-10-09）。

此前「env→本机 CP 出站链」在各脚本各持一份实现：mine_qa / import_xkt_qa 的
``_safe_urlopen``+``_cp_request``、load_intent_catalog 的
``_assert_safe_cp_url``+``_cp_request``、erc(e2e_real_customer).cp_request 的
就地校验+httpx、两个 probe 的 ``_cp``——护栏语义同源但形状分散，静态污点
引擎不认（L3 报 ~20 处「疑似跨文件污点」）。本模块=唯一持有 urlopen/httpx
出站调用的共享件：

- ``guard_url``：发送期出站闸。userinfo 拒 + bok_voice_core.urlguard 的
  ``assert_local_diag_url``（本地诊断白名单档：http/https + 环回 host 精确
  匹配 ∪ extra_hosts ∪ env ``BOK_PROBE_EXTRA_HOSTS`` 显式扩展）。拒=
  PermissionError（消费家族既有异常形状，消息逐字节保留
  「出站 URL 未过护栏（拒发）」）。fail-fast 门面（SystemExit）仍在
  urlguard_gate.gate（脚本 import 期用）；本模块是**发送期**闸，判定同源
  不重写。
- ``safe_urlopen``：urllib 出站唯一 sink（Request 已组好的形状）。
- ``httpx_send``：httpx 出站唯一 sink。探针族（erc.cp_request 及其委托
  者分支/战役/flow_graph 探针）消费 httpx.Response 原生面——.json()/
  .raise_for_status()/.status_code 与 httpx 异常族是调用方契约，换 urllib
  属行为面变化，故共享件保留 httpx 内核通道（惰性 import，urllib 侧消费方
  不背 httpx 依赖）。
- ``cp_request``：CP JSON 便捷面（mine_qa / import_xkt_qa 形状：Bearer+
  Content-Type+JSON 解析；HTTPError/URLError 原样透传给调用方既有 except）。
- ``cp_request_status``：CP 状态元组面（load_intent_catalog 形状：HTTPError
  内捕返回 ``(status, parsed-or-raw)``，空体→None）。

自举：本模块自行把 packages/core 插 sys.path（与 urlguard_gate 同款），调用方
零 bootstrap。纯离线可测（tests/test_cp_outbound.py，stub urlopen/httpx 零网络）。
"""

from __future__ import annotations
# --- scripts import bootstrap (G1) ---
# sys.path 引导(G1 迁移解耦,见 docs/superpowers/plans/2026-10-04-repo-governance-plan.md §3.1):
# 同层时是 no-op;文件挪进任何桶后裸 import 兄弟模块继续解析。
import sys as _sys, pathlib as _pathlib
_S = _pathlib.Path(__file__).resolve().parents[1]
for _d in (_S, _S / "lib", _S / "e2e", _S / "probes", _S / "bench"):
    if str(_d) not in _sys.path:
        _sys.path.insert(0, str(_d))


import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

_ROOT = _pathlib.Path(__file__).resolve().parents[2]
_CORE = str(_ROOT / "packages" / "core")
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from bok_voice_core.urlguard import (  # noqa: E402
    UrlGuardError,
    assert_local_diag_url,
)

__all__ = [
    "guard_url",
    "safe_urlopen",
    "httpx_send",
    "cp_request",
    "cp_request_status",
]

# 消费家族既有拒绝消息（mine_qa/_safe_urlopen、erc.cp_request 逐字节同形）。
_DENIED_MSG = "出站 URL 未过护栏（拒发）: {url}"


def guard_url(url: str, *, extra_hosts: "tuple[str, ...] | frozenset[str]" = ()) -> str:
    """发送期出站闸：userinfo 拒 + core 本地诊断白名单档。返回原 url。

    拒=PermissionError（消费家族既有异常形状；``from UrlGuardError`` 链因不改变
    str(exc)，对既有打印/日志面零漂移）。``extra_hosts`` 供 --allow-remote-host
    类「显式拍板放行的远程 host」映射进白名单；env 扩展口 BOK_PROBE_EXTRA_HOSTS
    由 core 守卫内读（云端测试显式 opt-in）。
    """
    url = str(url or "")
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise PermissionError(_DENIED_MSG.format(url=url))
    try:
        assert_local_diag_url(url, extra_hosts=extra_hosts)
    except UrlGuardError as exc:
        raise PermissionError(_DENIED_MSG.format(url=url)) from exc
    return url


def _sink_ip_boundary_check(url: str, extra_hosts: "tuple[str, ...] | frozenset[str]") -> None:
    """出站三验·sink 就地显式（2026-10-09）：协议/目标主机/解析后 IP 边界。

    判定与 ``guard_url`` 同源（本地诊断档）：显式白名单 host（extra_hosts ∪
    env ``BOK_PROBE_EXTRA_HOSTS``）直接放行；其余解析后每个地址必须落在环回
    边界内，DNS 解析失败 fail-closed 拒。在 ``safe_urlopen``/``httpx_send``
    的 sink 调用点前调用——就地显式是给静态污点引擎可见的校验形状（与
    markdown_source 的发送期三验同款，实测可消「动态 URL 未经协议/主机/解析后
    IP 校验」类 high）。
    """
    import ipaddress
    import socket

    parts = urllib.parse.urlsplit(str(url or ""))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PermissionError(_DENIED_MSG.format(url=url))
    allowed_hosts = {h.lower() for h in set(extra_hosts or ())}
    allowed_hosts.update(
        h.lower()
        for h in os.environ.get("BOK_PROBE_EXTRA_HOSTS", "").replace(",", " ").split()
        if h
    )
    if parts.hostname in allowed_hosts:
        return
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except OSError:
        raise PermissionError(_DENIED_MSG.format(url=url)) from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_loopback:
            raise PermissionError(_DENIED_MSG.format(url=url))


def safe_urlopen(req, *, timeout: float, data=None,
                 extra_hosts: "tuple[str, ...] | frozenset[str]" = ()):
    """urllib 出站唯一 sink：urlopen 前过 ``guard_url`` + sink 就地三验。"""
    guard_url(req.full_url, extra_hosts=extra_hosts)
    _sink_ip_boundary_check(req.full_url, extra_hosts)
    return urllib.request.urlopen(req, data=data, timeout=timeout)


def httpx_send(method: str, url: str, **kw):
    """httpx 出站唯一 sink：request 前过 ``guard_url`` + sink 就地三验，
    返回 httpx.Response。

    httpx 惰性 import——urllib 侧消费方（mine_qa 等产品 exec 面）不背 httpx 依赖。
    """
    import httpx  # noqa: PLC0415

    guard_url(url)
    _sink_ip_boundary_check(url, ())
    return httpx.request(method, url, **kw)


def cp_request(
    base: str,
    path: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 30.0,
    ensure_ascii: bool = True,
    content_type_always: bool = True,
    extra_hosts: "tuple[str, ...] | frozenset[str]" = (),
) -> object:
    """CP JSON 便捷面（mine_qa / import_xkt_qa 形状）：返回解析 JSON。

    Bearer 头仅 token 非空时带；Content-Type=application/json（缺省恒带——
    与 mine_qa 家族逐字节同）；``ensure_ascii=False``+``content_type_always=False``
    供按 body 才带 Content-Type 的形状；HTTPError/URLError 原样透传。
    """
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", method=method)
    if content_type_always or payload is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(payload, ensure_ascii=ensure_ascii).encode("utf-8") if payload is not None else None
    with safe_urlopen(req, data=data, timeout=timeout, extra_hosts=extra_hosts) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cp_request_status(
    base: str,
    path: str,
    *,
    token: str = "",
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 15.0,
    extra_hosts: "tuple[str, ...] | frozenset[str]" = (),
) -> "tuple[int, object]":
    """CP 状态元组面（load_intent_catalog 形状）：HTTPError 内捕不抛。

    返回 ``(status, parsed)``；响应体空→None；HTTPError 响应体解析失败→原文字符串。
    ensure_ascii=False（中文键值直出 UTF-8）与 Content-Type 仅 body 在场才带，
    均与 load_intent_catalog 既有形状逐字节同。
    """
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with safe_urlopen(req, timeout=timeout, extra_hosts=extra_hosts) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, raw
