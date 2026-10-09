"""BokMarkdownSource 出站守卫测试（2026-10-08 护栏升级，accepted-risk → fixed）。

覆盖 `_guard_outbound_url` 经构造器（保存期全验，DNS stub）与 read()/write()
（发送期复验）两个消费面：公网放行、环回/私网/云 metadata/IPv6 链路本地/
userinfo/scheme 拒绝、`BOK_KNOWLEDGE_ALLOW_PRIVATE=1` 显式放行口、read path
query 编码。全部 stub（getaddrinfo / urlopen），零真实网络。
"""
from __future__ import annotations

import socket

import pytest

# 文档注释位：守卫语义单点在 packages/knowledge/bok_voice_knowledge/markdown_source.py
# （地址判定单源委托 bok_voice_core.urlguard，与 CP 短信 webhook 同一守卫）。


_GLOBAL_IP = "8.8.8.8"  # 公网字面量（is_global），免 DNS

_ENV = "BOK_KNOWLEDGE_ALLOW_PRIVATE"


def _stub_dns(monkeypatch: pytest.MonkeyPatch, addrs: list[str] | None = None, *, error: bool = False) -> None:
    """stub urlguard 的 socket.getaddrinfo：构造器保存期 DNS 判定零真实网络。"""
    import bok_voice_core.urlguard as urlguard

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if error:
            raise OSError("dns broken")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))
            for addr in (addrs or [_GLOBAL_IP])
        ]

    monkeypatch.setattr(urlguard.socket, "getaddrinfo", fake_getaddrinfo)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _stub_urlopen(monkeypatch: pytest.MonkeyPatch, capture: list) -> None:
    """stub urllib.request.urlopen：捕获出站请求（Request 或裸 URL），零网络。"""
    import urllib.request

    def fake_urlopen(req, *args, **kwargs):
        capture.append(req)
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _captured_url(req) -> str:
    import urllib.request

    return req.full_url if isinstance(req, urllib.request.Request) else str(req)


def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)


# ---- 放行面（默认档=公网；放行口=env 显式） ----


def test_default_allows_public_ip_literal(monkeypatch):
    _no_env(monkeypatch)
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    src = BokMarkdownSource(f"http://{_GLOBAL_IP}:8771/v1", token="t")
    assert src.base_url == f"http://{_GLOBAL_IP}:8771/v1"


def test_default_allows_public_hostname_via_dns(monkeypatch):
    _no_env(monkeypatch)
    _stub_dns(monkeypatch, addrs=[_GLOBAL_IP])
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    BokMarkdownSource("https://bok.example.com/v1", token="t")


def test_allow_private_env_admits_loopback_and_lan(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    BokMarkdownSource("http://127.0.0.1:8771/v1")  # 历史缺省基址（环回）
    BokMarkdownSource("http://10.1.2.3:8771/v1")  # 实验室内网
    BokMarkdownSource("http://192.168.1.10/v1")


def test_allow_private_env_requires_exact_1(monkeypatch):
    # 严格等值 "1"：truthy 泛化值一律不放（避免配置歧义）。
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    for bad in ("true", "yes", "0", ""):
        monkeypatch.setenv(_ENV, bad)
        with pytest.raises(ValueError):
            BokMarkdownSource("http://127.0.0.1:8771/v1")


# ---- 拒绝面（默认档） ----


@pytest.mark.parametrize(
    "bad",
    [
        "http://127.0.0.1:8771/v1",  # 环回（含历史缺省基址）
        "http://169.254.169.254/v1",  # 云 metadata（link-local 恒拒）
        "http://[::1]:8771/v1",  # IPv6 环回
        "http://[fe80::1]/v1",  # IPv6 链路本地
        "http://fe80::1/v1",  # 未带方括号的残缺 IPv6（fail-closed）
        "http://10.0.0.5/v1",  # 私网
        "http://192.168.0.7/v1",  # 私网
        "http://user:pass@8.8.8.8/v1",  # userinfo（公网目标也拒）
    ],
)
def test_default_denies_private_and_userinfo(monkeypatch, bad):
    _no_env(monkeypatch)
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    with pytest.raises(ValueError) as ei:
        BokMarkdownSource(bad)
    # 错误消息带实际值方便运维定位。
    assert bad in str(ei.value)


def test_policy_error_message_carries_ops_hint(monkeypatch):
    # 地址策略类错误消息带放行口提示（userinfo 类错误不带——错误族不同）。
    _no_env(monkeypatch)
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    with pytest.raises(ValueError) as ei:
        BokMarkdownSource("http://127.0.0.1:8771/v1")
    assert _ENV in str(ei.value)


def test_default_denies_hostname_resolving_private(monkeypatch):
    _no_env(monkeypatch)
    _stub_dns(monkeypatch, addrs=["127.0.0.1"])
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    with pytest.raises(ValueError):
        BokMarkdownSource("http://localhost:8771/v1")


def test_dns_multi_record_any_private_rejected(monkeypatch):
    # 多 A 记录保守处理：任一条命中阻止段即拒。
    _no_env(monkeypatch)
    _stub_dns(monkeypatch, addrs=[_GLOBAL_IP, "10.0.0.9"])
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    with pytest.raises(ValueError):
        BokMarkdownSource("https://mixed.example.com/v1")


def test_dns_resolution_failure_fail_closed(monkeypatch):
    _no_env(monkeypatch)
    _stub_dns(monkeypatch, error=True)
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    with pytest.raises(ValueError):
        BokMarkdownSource("https://unreachable.example.com/v1")


def test_metadata_denied_even_with_allow_private(monkeypatch):
    # 放行口只开私网/环回，云 metadata 端无口子（与 webhook 先例同口径）。
    monkeypatch.setenv(_ENV, "1")
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    with pytest.raises(ValueError):
        BokMarkdownSource("http://169.254.169.254/v1")


@pytest.mark.parametrize(
    "bad",
    ["file:///etc", "ftp://x/v1", "gopher://x", "javascript:alert(1)", "/local/path"],
)
def test_non_http_scheme_rejected(monkeypatch, bad):
    _no_env(monkeypatch)
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    with pytest.raises(ValueError):
        BokMarkdownSource(bad)


# ---- read()/write() 发送期消费面（stub urlopen，零网络） ----


def test_read_encodes_path_query(monkeypatch):
    _no_env(monkeypatch)
    capture: list = []
    _stub_urlopen(monkeypatch, capture)
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    src = BokMarkdownSource(f"http://{_GLOBAL_IP}/v1")
    src.read("accounts/a/doc&x=1\nnext")
    url = _captured_url(capture[0])
    assert "&x=" not in url  # & 私加 query 注入被编码消除
    assert "\n" not in url  # CRLF 注入被编码消除
    assert "path=accounts%2Fa%2Fdoc%26x%3D1%0Anext" in url


def test_read_sends_auth_header_and_hits_endpoint(monkeypatch):
    _no_env(monkeypatch)
    capture: list = []
    _stub_urlopen(monkeypatch, capture)
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    src = BokMarkdownSource(f"http://{_GLOBAL_IP}/v1", token="t")
    src.read("accounts/a/doc.md")
    req = capture[0]
    assert _captured_url(req).endswith("/documents/read?path=accounts%2Fa%2Fdoc.md")
    assert req.get_header("Authorization") == "Bearer t"


def test_write_public_target_passes(monkeypatch):
    _no_env(monkeypatch)
    capture: list = []
    _stub_urlopen(monkeypatch, capture)
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    src = BokMarkdownSource(f"http://{_GLOBAL_IP}/v1")
    out = src.write("accounts/a/doc.md", "hello")
    assert _captured_url(capture[0]).endswith("/documents/write")
    assert out == {"ok": True}


def test_request_face_guard_reblocks_mutated_base_url(monkeypatch):
    # 纵深：构造后 base_url 被改坏（属性面污染）也要在发送期复验拦下，零出站。
    _no_env(monkeypatch)
    capture: list = []
    _stub_urlopen(monkeypatch, capture)
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    src = BokMarkdownSource(f"http://{_GLOBAL_IP}/v1")
    src.base_url = "http://169.254.169.254/v1"
    with pytest.raises(ValueError):
        src.read("accounts/a/doc.md")
    with pytest.raises(ValueError):
        src.write("accounts/a/doc.md", "x")
    assert capture == []
