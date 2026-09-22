"""出站 URL 守卫（Mimosa SSRF 修复单点，2026-09-23，审计 scan-…44f1c2c94126）。

仓库此前三种复制粘贴姿势（snippet_seed_mining.guard_url / probe_intent_mine 与
probe_cache_discipline 的环回白名单 / t56r1_probe 的构造+复验）收敛为一模块：

- ``assert_public_http_url``：**外部端点**（CP 短信 webhook 等）——scheme 白名单
  http/https + IP 字面量/DNS 解析后拒非公网地址；云元数据段（link-local，
  169.254.169.254 族）**恒拒**（``allow_private`` 也不放行——那是给实验室私网
  网关的口子，不是给元数据端的）。配置保存期建议 ``resolve=True`` 全验（含
  DNS），发送期可 ``resolve=False`` 只验字面量（避免结算尾钩子被解析阻塞）。
- ``assert_local_diag_url``：**本地诊断端点**（探针/e2e/load 脚本、sidecar 的
  环回默认）——host 白名单 {127.0.0.1, localhost, ::1} ∪ extra_hosts ∪ env
  扩展口 ``BOK_PROBE_EXTRA_HOSTS``（逗号分隔；云端 CP/LLM 测试**显式 opt-in**）。
  不解析 DNS：诊断脚本面对的应是钉死端点，解析会阻塞且引入 TOCTOU。

纪律：fail-closed（解析失败/形状坏一律 ``UrlGuardError``，绝不放行）；纯函数、
仅标准库、离线可测；异常消息只含 host/scheme，不含 query/凭据。守卫消灭的是
配置错误与直连内网/元数据；校验与请求之间的 TOCTOU 窗口由消费方短超时请求
兜底（本守卫不做请求）。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

__all__ = ["UrlGuardError", "assert_public_http_url", "assert_local_diag_url"]


class UrlGuardError(ValueError):
    """URL 守卫拒绝（fail-closed）。消息可直进日志/审计，不含敏感值。"""


# 环回白名单（本地诊断语义）：字符串精确匹配。127.0.0.2 等「环回段其它地址」
# 不放——诊断端点的仓库惯例只有 127.0.0.1/localhost，收紧不放松。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# env 扩展口（本地诊断族）：逗号分隔 host。云端 CP/LLM 真栈测试时显式 opt-in，
# 默认空=纯环回。脚本侧 dev 面 env，不进 _FORWARD_ENV（worker/prod 不消费）。
_EXTRA_HOSTS_ENV = "BOK_PROBE_EXTRA_HOSTS"


def _split_url(url: str) -> tuple[str, str]:
    """urlsplit + scheme/host 硬校验；返回 (scheme, lower-host)。"""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https"):
        raise UrlGuardError(f"url scheme not http/https: {parts.scheme!r}")
    # 不带方括号的 IPv6（"http://fe80::1/x"）会被 urlsplit 劈成 host="fe80"、
    # 端口串位——这种残缺 host 走 DNS 路径纯属垃圾进垃圾出，一律拒（实测 macOS
    # 上 getaddrinfo("fe80") 可解析出不可控地址）。规范形态必须 "[fe80::1]"。
    if "::" in (parts.netloc or "") and "[" not in (parts.netloc or ""):
        raise UrlGuardError("ipv6 host must be bracketed: [...]")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise UrlGuardError("url has no host")
    return parts.scheme, host


def _parse_ip(host: str) -> "ipaddress.IPv4Address | ipaddress.IPv6Address | None":
    """host 是 IP 字面量则解析（IPv6 zone 段剥掉），否则 None。"""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _ip_blocked(
    ip: "ipaddress.IPv4Address | ipaddress.IPv6Address", allow_private: bool
) -> bool:
    """公网守卫的地址判定：元数据/组播/未指定恒拒；环回/私网/保留默认拒。"""
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True  # 云元数据（169.254.169.254 ∈ link-local）无口子
    if allow_private:
        return False  # 实验室私网网关显式放行（含环回）
    return ip.is_loopback or ip.is_private or ip.is_reserved


def assert_public_http_url(url: str, *, allow_private: bool = False, resolve: bool = True) -> str:
    """外部端点守卫：http/https + 解析后非环回/私网/保留/元数据段。返回原 url。

    ``allow_private=True`` 放行环回/私网/保留（实验室网关），link-local（云元数据）、
    组播、未指定地址**仍拒**。``resolve=False`` 跳过 DNS（只验字面量与形状）——
    发送期复验用，避免阻塞；保存期应 ``resolve=True`` 全验。DNS 失败=fail-closed 拒。
    """
    _scheme, host = _split_url(url)
    literal = _parse_ip(host)
    if literal is not None:
        if _ip_blocked(literal, allow_private):
            raise UrlGuardError(f"host address not allowed: {host}")
        return url
    if not resolve:
        return url  # 域名：保存期已全验，发送期只复验形状/字面量
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise UrlGuardError(f"host resolution failed: {host}") from exc
    if not infos:
        raise UrlGuardError(f"host resolved to nothing: {host}")
    for info in infos:
        addr = str(info[4][0])
        ip = _parse_ip(addr.split("%", 1)[0])
        if ip is None:
            continue
        if _ip_blocked(ip, allow_private):
            raise UrlGuardError(f"host resolves to blocked address ({addr}): {host}")
    return url


def assert_local_diag_url(
    url: str, *, extra_hosts: "tuple[str, ...] | frozenset[str]" = ()
) -> str:
    """本地诊断端点守卫：host ∈ 环回白名单 ∪ extra_hosts ∪ env 扩展口。返回原 url。

    只认 http/https 与**钉死 host**（字符串精确匹配，不解析 DNS、不做后缀匹配——
    ``evil.localhost`` 不中 ``localhost``）。env 扩展口 ``BOK_PROBE_EXTRA_HOSTS``
    逗号分隔，供云端 CP/LLM 真栈测试显式声明目标。
    """
    _scheme, host = _split_url(url)
    allowed = set(_LOOPBACK_HOSTS)
    allowed.update(str(h).strip().lower() for h in extra_hosts if str(h).strip())
    env_extra = os.environ.get(_EXTRA_HOSTS_ENV, "")
    allowed.update(h.strip().lower() for h in env_extra.split(",") if h.strip())
    if host not in allowed:
        raise UrlGuardError(f"host not in local-diag allowlist: {host}")
    return url
