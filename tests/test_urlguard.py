"""urlguard 出站守卫单测（Mimosa SSRF 修复，2026-09-23）。

纯离线：DNS 路径全部经 monkeypatch 桩掉（不打网络）。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "packages" / "core") not in sys.path:
    sys.path.insert(0, str(ROOT / "packages" / "core"))

from bok_voice_core.urlguard import (  # noqa: E402
    UrlGuardError,
    assert_local_diag_url,
    assert_public_http_url,
)


# ---- assert_local_diag_url ----


def test_local_diag_accepts_loopback_variants():
    assert assert_local_diag_url("http://127.0.0.1:8000/api/x") == "http://127.0.0.1:8000/api/x"
    assert assert_local_diag_url("http://localhost:8010/")
    assert assert_local_diag_url("https://127.0.0.1/api/token")
    assert assert_local_diag_url("http://[::1]:8788/v1/audio/speech")


def test_local_diag_rejects_non_http_scheme():
    for bad in ("ftp://127.0.0.1/x", "file:///etc/passwd", "httpx://127.0.0.1/x", ""):
        with pytest.raises(UrlGuardError):
            assert_local_diag_url(bad)


def test_local_diag_rejects_foreign_hosts():
    for bad in (
        "http://example.com/api",
        "http://192.168.1.5:8000/api",
        "http://10.0.0.8/api",
        "http://169.254.169.254/latest/meta-data",
        "http://evil.localhost/api",  # 后缀不算命中：字符串精确匹配
        "http:///no-host",
    ):
        with pytest.raises(UrlGuardError):
            assert_local_diag_url(bad)


def test_local_diag_extra_hosts_param_and_env(monkeypatch):
    monkeypatch.delenv("BOK_PROBE_EXTRA_HOSTS", raising=False)
    # extra_hosts 参数
    assert assert_local_diag_url("http://cloud-cp.internal:8000/api", extra_hosts=("cloud-cp.internal",))
    with pytest.raises(UrlGuardError):
        assert_local_diag_url("http://other-cp.internal:8000/api", extra_hosts=("cloud-cp.internal",))
    # env 扩展口（云端测试显式 opt-in）
    monkeypatch.setenv("BOK_PROBE_EXTRA_HOSTS", "cp.example.net, llm.example.net")
    assert assert_local_diag_url("http://cp.example.net/api")
    assert assert_local_diag_url("http://llm.example.net:1235/v1")
    with pytest.raises(UrlGuardError):
        assert_local_diag_url("http://not-in-env.example.net/api")


def test_local_diag_uppercase_host_normalized():
    assert assert_local_diag_url("http://LOCALHOST:8000/")


# ---- assert_public_http_url：IP 字面量 ----


def test_public_rejects_private_literals():
    for bad in (
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://192.168.0.1/x",
        "http://172.16.0.9/x",
        "http://0.0.0.0/x",
        "http://224.0.0.1/x",  # 组播
    ):
        with pytest.raises(UrlGuardError):
            assert_public_http_url(bad)


def test_public_metadata_blocked_even_with_allow_private():
    # 云元数据段（link-local）无口子：allow_private 也不放行
    for bad in ("http://169.254.169.254/latest/meta-data", "http://[fe80::1]/x"):
        with pytest.raises(UrlGuardError):
            assert_public_http_url(bad)
        with pytest.raises(UrlGuardError):
            assert_public_http_url(bad, allow_private=True)


def test_public_unbracketed_ipv6_rejected_shape_gate():
    # 裸 IPv6 会被 urlsplit 劈残（host="fe80"）——形状门直接拒，不进 DNS 路径
    for bad in ("http://fe80::1/x", "http://::1/x"):
        with pytest.raises(UrlGuardError, match="bracketed"):
            assert_local_diag_url(bad)
        with pytest.raises(UrlGuardError, match="bracketed"):
            assert_public_http_url(bad, resolve=False)


def test_public_allow_private_permits_lab_gateways():
    assert assert_public_http_url("http://10.1.2.3/sms", allow_private=True)
    assert assert_public_http_url("http://127.0.0.1:9999/sms", allow_private=True)
    assert assert_public_http_url("http://192.168.5.5/sms", allow_private=True)


def test_public_accepts_public_literal_and_scheme_gate():
    assert assert_public_http_url("http://93.184.216.34/callback")
    for bad in ("ftp://93.184.216.34/x", "javascript:alert(1)", "http://[fe80::db8]/x"):
        with pytest.raises(UrlGuardError):
            assert_public_http_url(bad)


# ---- assert_public_http_url：DNS 解析路径（monkeypatch 桩，零网络） ----


def _fake_resolver(addrs):
    def _resolve(host, *a, **kw):
        infos = []
        for addr in addrs:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            infos.append((family, None, None, "", (addr, 0)))
        return infos

    return _resolve


def test_public_domain_to_public_ip_passes(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver(["93.184.216.34"]))
    assert assert_public_http_url("https://sms-gateway.example.com/hook")


def test_public_domain_to_private_ip_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver(["10.0.0.9"]))
    with pytest.raises(UrlGuardError, match="blocked address"):
        assert_public_http_url("https://rebind.example.com/hook")
    # allow_private 下同一解析放行（实验室网关走域名）
    assert assert_public_http_url("https://rebind.example.com/hook", allow_private=True)


def test_public_domain_to_metadata_ip_rejected_even_allow_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver(["169.254.169.254"]))
    with pytest.raises(UrlGuardError):
        assert_public_http_url("https://meta.example.com/hook", allow_private=True)


def test_public_dns_failure_fail_closed(monkeypatch):
    def _boom(host, *a, **kw):
        raise OSError("nx")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(UrlGuardError, match="resolution failed"):
        assert_public_http_url("https://unresolvable.example.com/hook")


def test_public_resolve_false_skips_dns(monkeypatch):
    calls = []

    def _spy(host, *a, **kw):
        calls.append(host)
        return _fake_resolver(["93.184.216.34"])(host)

    monkeypatch.setattr(socket, "getaddrinfo", _spy)
    assert assert_public_http_url("https://already-validated.example.com/hook", resolve=False)
    assert calls == []  # 发送期复验不解析、不阻塞
    # 但字面量地址照拒（复验仍拦私网/元数据字面量）
    with pytest.raises(UrlGuardError):
        assert_public_http_url("http://169.254.169.254/x", resolve=False)


def test_error_messages_carry_no_query_string():
    # 异常消息只含 host/scheme：url 带 token query 也不进消息（日志/审计面防泄漏）
    try:
        assert_local_diag_url("http://evil.example.com/cb?token=sekrit-value")
    except UrlGuardError as exc:
        assert "sekrit-value" not in str(exc)
    try:
        assert_public_http_url("http://127.0.0.1/cb?token=sekrit-value")
    except UrlGuardError as exc:
        assert "sekrit-value" not in str(exc)
