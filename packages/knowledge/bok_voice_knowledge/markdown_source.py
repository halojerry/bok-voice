from __future__ import annotations

import os
from pathlib import Path

# scheme 白名单（构造器与守卫共用单点）。
_HTTP_SCHEMES = ("http", "https")

# 私网放行口（2026-10-08 护栏升级）：知识源 URL 是 admin/运维配置的，可能合法
# 指向实验室内网——env 显式置 "1" 才放行环回/私网/保留段；严格等值 "1"，
# 不做 truthy 泛化（"true"/"yes" 一律不放，避免配置歧义）。
_KNOWLEDGE_ALLOW_PRIVATE_ENV = "BOK_KNOWLEDGE_ALLOW_PRIVATE"


def _knowledge_allow_private() -> bool:
    """私网/环回放行口：仅 ``BOK_KNOWLEDGE_ALLOW_PRIVATE=1`` 显式放行。"""
    return os.environ.get(_KNOWLEDGE_ALLOW_PRIVATE_ENV, "").strip() == "1"


def _guard_outbound_url(url: str, *, resolve: bool) -> str:
    """BokMarkdownSource 出站守卫（2026-10-08 护栏升级，accepted-risk → fixed）。

    read()/write() 出站前与构造器统一过本闸（单一守卫函数）。校验：

    - scheme ∈ {http, https}（地址策略部分由 ``bok_voice_core.urlguard`` 单源
      承担——与 CP 短信 webhook / node-agent CP 基址同一守卫，不立第二份
      ipaddress 判定）、host 非空、无 userinfo（凭据只走 Authorization 头）；
    - 地址策略：IP 字面量或 DNS 解析后拒环回/私网/保留段（127/8、10/8、
      192.168/16、::1、fc00::/7 等）；云元数据/链路本地（169.254.169.254 族）、
      组播、未指定地址**恒拒**——``BOK_KNOWLEDGE_ALLOW_PRIVATE=1`` 放行口不
      开元数据端（与 webhook 先例同口径）；
    - 域名：解析失败=fail-closed 拒；多 A/AAAA 记录任一命中阻止段即拒；
    - ``resolve=False``（发送期复验）只验形状与字面量、不做 DNS——对标 CP
      短信 webhook「保存期全验（DNS）+ 发送期复验」双重校验先例；构造器=
      保存期 ``resolve=True`` 全验。

    **行为变化（默认档收紧）**：默认（env 未设）拒绝私网/环回目标。历史缺省
    基址 ``http://127.0.0.1:8771/v1`` 与一切内网知识源须显式
    ``BOK_KNOWLEDGE_ALLOW_PRIVATE=1`` 才可用。违规抛 ValueError（启动/请求面
    显式炸），消息带实际 URL 方便运维定位（URL 不含凭据：userinfo 被禁、
    token 只走请求头）。
    """
    from urllib.parse import urlsplit

    from bok_voice_core.urlguard import UrlGuardError, assert_public_http_url

    parts = urlsplit(url)
    if parts.username or parts.password:
        raise ValueError(
            f"出站 URL 不允许携带 userinfo（凭据只走 Authorization 头），拒发: {url!r}")
    try:
        return assert_public_http_url(
            url, allow_private=_knowledge_allow_private(), resolve=resolve)
    except UrlGuardError as exc:
        raise ValueError(
            "出站 URL 未过护栏（拒发；内网/环回知识源请显式 "
            f"{_KNOWLEDGE_ALLOW_PRIVATE_ENV}=1）: {url!r}: {exc}"
        ) from exc



class LocalMarkdownSource:
    """MarkdownSource backed by a local vault directory.

    Paths are always namespaced under `accounts/{account_id}/...` so the caller
    must pass an explicit account-scoped path. This keeps data isolated by
    account from birth, and later can be swapped for a Bok HTTP client.
    """

    def __init__(self, vault_root: str | os.PathLike[str]):
        self.root = Path(vault_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        candidate = (self.root / path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("path escapes vault")
        return candidate

    def read(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> dict:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": path, "bytes": len(content.encode())}

    def versions(self, path: str) -> list[dict]:
        # Local MVP: single version; Bok-backed production can expose real version history.
        target = self._resolve(path)
        return [{"path": path, "version": 1, "exists": target.exists()}]

    def forget(self, path: str) -> dict:
        target = self._resolve(path)
        if target.exists():
            target.unlink()
        return {"path": path, "forgotten": True}


class BokMarkdownSource:
    """Optional MarkdownSource that proxies to a running Bok service via HTTP.

    Only used when BOK_URL is configured (later milestone). Kept as a concrete
    implementation so the KnowledgeService does not change when Bok is enabled.

    出站守卫（2026-10-08 护栏升级，accepted-risk → fixed）：构造器（保存期，
    DNS 全验）与 read()/write()（发送期复验）统一过 ``_guard_outbound_url``——
    scheme 白名单 http/https、host 非空、无 userinfo、环回/私网/保留段默认拒；
    云元数据/链路本地（169.254.169.254 族）/组播/未指定恒拒。**行为变化**：
    知识源 URL 允许是运维配置的合法内网目标，但默认（env 未设）会拒——内网/
    环回基址（含历史缺省 ``http://127.0.0.1:8771/v1``）须显式
    ``BOK_KNOWLEDGE_ALLOW_PRIVATE=1`` 才放行；违规抛 ValueError，消息带实际
    URL。``read()`` 的 path 参数经 urlencode 后拼 query（防 `&`/`#`/CRLF 注入）。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8771/v1", token: str = ""):
        # scheme 白名单（2026-10-02 安全分流跟进）：base_url 是操作员 env 配置
        # （BOK_URL），但 urllib 对 file:// 等非预期 scheme 会照单全收——构造期
        # 就拒绝，配置错误在启动面炸而不是请求面静默读本地文件。
        from urllib.parse import urlsplit

        scheme = urlsplit(base_url).scheme.lower()
        if scheme not in _HTTP_SCHEMES:
            raise ValueError(
                f"BokMarkdownSource base_url 仅支持 http/https，收到 {scheme!r}：{base_url!r}")
        # 出站守卫·保存期全验（2026-10-08 护栏升级）：DNS 解析后判地址策略，
        # 环回/私网/保留段默认拒——配置错误在启动面炸（既有风格）。
        _guard_outbound_url(base_url, resolve=True)
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"} if self.token else {"Content-Type": "application/json"}

    def _outbound_url(self, endpoint: str) -> str:
        """拼基址并过出站闸（发送期复验：形状+字面量，不做 DNS——保存期已全验，
        避免请求路径被解析阻塞；对标 CP 短信 webhook 双重校验先例）。"""
        return _guard_outbound_url(f"{self.base_url}{endpoint}", resolve=False)

    def read(self, path: str) -> str:
        import urllib.request
        from urllib.parse import urlencode, urlsplit

        # path 走 query 值编码（2026-10-08 护栏升级）：裸拼可注入 `&`/`#`/换行
        # （私加 query 参数 / 截断请求行），urlencode 消除注入面。
        endpoint = f"/documents/read?{urlencode({'path': path})}"
        url = self._outbound_url(endpoint)
        # 出站三验·发送期就地展开（2026-10-09）：协议/目标主机/解析后 IP 边界
        # 在 sink 调用点显式判定——语义与 _guard_outbound_url 同源（默认拒
        # 私网/环回，BOK_KNOWLEDGE_ALLOW_PRIVATE=1 放行，metadata/链路本地恒拒），
        # 就地展开是给静态污点引擎可见的 sink 前校验形状。
        import ipaddress
        import socket
        _parts = urlsplit(url)
        if _parts.scheme not in _HTTP_SCHEMES or not _parts.hostname:
            raise ValueError(f"出站 URL 未过护栏（协议/主机）: {url!r}")
        _port = _parts.port or (443 if _parts.scheme == "https" else 80)
        _allow = _knowledge_allow_private()
        for _info in socket.getaddrinfo(_parts.hostname, _port, proto=socket.IPPROTO_TCP):
            _ip = ipaddress.ip_address(_info[4][0])
            if _ip.is_link_local or _ip.is_multicast or _ip.is_unspecified:
                raise ValueError(f"出站 URL 命中恒拒地址段（metadata/链路本地）: {url!r}")
            if not _allow and (_ip.is_loopback or _ip.is_private or _ip.is_reserved):
                raise ValueError(
                    f"出站 URL 命中默认拒地址段（内网/环回请 {_KNOWLEDGE_ALLOW_PRIVATE_ENV}=1）: {url!r}")
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode()

    def write(self, path: str, content: str) -> dict:
        import urllib.request
        from urllib.parse import urlsplit

        import json

        # 出站闸门（2026-10-08 升级为 _guard_outbound_url 单一守卫）：原 sink 级
        # scheme/host/userinfo 就地校验收编进守卫（严格超集，另加地址策略），
        # 违规统一抛 ValueError。
        url = self._outbound_url("/documents/write")
        # 出站三验·发送期就地展开（2026-10-09）：与 read() 同款——协议/目标
        # 主机/解析后 IP 边界在 sink 调用点显式判定（引擎可见形状）。
        import ipaddress
        import socket
        _parts = urlsplit(url)
        if _parts.scheme not in _HTTP_SCHEMES or not _parts.hostname:
            raise ValueError(f"出站 URL 未过护栏（协议/主机）: {url!r}")
        _port = _parts.port or (443 if _parts.scheme == "https" else 80)
        _allow = _knowledge_allow_private()
        for _info in socket.getaddrinfo(_parts.hostname, _port, proto=socket.IPPROTO_TCP):
            _ip = ipaddress.ip_address(_info[4][0])
            if _ip.is_link_local or _ip.is_multicast or _ip.is_unspecified:
                raise ValueError(f"出站 URL 命中恒拒地址段（metadata/链路本地）: {url!r}")
            if not _allow and (_ip.is_loopback or _ip.is_private or _ip.is_reserved):
                raise ValueError(
                    f"出站 URL 命中默认拒地址段（内网/环回请 {_KNOWLEDGE_ALLOW_PRIVATE_ENV}=1）: {url!r}")
        req = urllib.request.Request(
            url,
            data=json.dumps({"path": path, "content": content}).encode(),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def versions(self, path: str) -> list[dict]:
        return [{"path": path, "version": 1}]

    def forget(self, path: str) -> dict:
        return {"path": path, "forgotten": True}
