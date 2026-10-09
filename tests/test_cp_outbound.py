"""cp_outbound 共享出站单点单测（2026-10-09 Mimosa L3 收敛）。

纯离线：urlopen / httpx.request 全程 monkeypatch 桩，零网络。钉住面：
本地诊断档放行/拒绝矩阵（userinfo/非 http/环回外 host/BOK_PROBE_EXTRA_HOSTS
opt-in）、拒绝消息逐字节形状（「出站 URL 未过护栏（拒发）」）、两个 CP 便捷面
的头组装与序列化字节（mine_qa 形状 ensure_ascii=True 恒带 Content-Type vs
load_intent_catalog 形状 ensure_ascii=False 仅 body 才带）、HTTPError 内捕的
(status, parsed-or-raw) 元组形状。
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "packages" / "core") not in sys.path:
    sys.path.insert(0, str(ROOT / "packages" / "core"))
if str(ROOT / "scripts" / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import cp_outbound  # noqa: E402


class _FakeResp:
    """urlopen 桩的最小响应面（read/status + context manager）。"""

    def __init__(self, body: bytes = b"", status: int = 200):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(monkeypatch, *, body: bytes = b"", status: int = 200, calls: list | None = None):
    """桩掉 urllib.request.urlopen；calls 非空时逐次登记 (url, data, headers)。"""

    def _capture_open(request_obj, data=None, timeout=None):
        if calls is not None:
            calls.append({
                "url": request_obj.full_url,
                # body 两形态：data= 直传（mine_qa 形状）或 Request 构造期携带
                # （cp_request_status 形状）——进桩点前已归一到 request_obj.data。
                "data": data if data is not None else getattr(request_obj, "data", None),
                "timeout": timeout,
                "headers": dict(request_obj.header_items()),
            })
        return _FakeResp(body, status)

    # 打点打在 cp_outbound 消费的同一模块对象上（urllib.request 是进程单例），
    # 行为与直接 setattr(urllib.request, ...) 等价。
    monkeypatch.setattr(cp_outbound.urllib.request, "urlopen", _capture_open)


# ---- guard_url：本地诊断档判定矩阵 ----


def test_guard_url_passes_loopback_variants():
    for url in ("http://127.0.0.1:8000/api/x", "http://localhost:8000/", "https://[::1]:8000/v1"):
        assert cp_outbound.guard_url(url) == url


def test_guard_url_rejects_userinfo_non_http_foreign_host(monkeypatch):
    monkeypatch.delenv("BOK_PROBE_EXTRA_HOSTS", raising=False)
    for bad in (
        "http://user:pass@127.0.0.1:8000/api",
        "ftp://127.0.0.1/x",
        "file:///etc/passwd",
        "http://example.com/api",
        "http://10.0.0.5:8000/api",
        "http:///no-host",
    ):
        with pytest.raises(PermissionError) as excinfo:
            cp_outbound.guard_url(bad)
        # 消息逐字节形状（mine_qa/_safe_urlopen 家族既有形状）
        assert str(excinfo.value) == f"出站 URL 未过护栏（拒发）: {bad}"


def test_guard_url_env_extra_hosts_opt_in(monkeypatch):
    monkeypatch.delenv("BOK_PROBE_EXTRA_HOSTS", raising=False)
    with pytest.raises(PermissionError):
        cp_outbound.guard_url("http://cp.example.net/api")
    monkeypatch.setenv("BOK_PROBE_EXTRA_HOSTS", "cp.example.net, llm.example.net")
    assert cp_outbound.guard_url("http://cp.example.net/api") == "http://cp.example.net/api"


def test_guard_url_extra_hosts_param_remote_flag_mapping():
    # --allow-remote-host 语义映射：显式 host 进白名单（load_intent_catalog 用）
    assert cp_outbound.guard_url("http://192.168.1.5:8000", extra_hosts=("192.168.1.5",))
    with pytest.raises(PermissionError):
        cp_outbound.guard_url("http://192.168.1.5:8000")


# ---- safe_urlopen：闸在 sink 前（拒发=零调用）----


def test_safe_urlopen_blocks_before_socket(monkeypatch):
    calls: list = []
    _stub_urlopen(monkeypatch, calls=calls)
    req = urllib.request.Request("http://evil.example.com/api", method="GET")
    with pytest.raises(PermissionError):
        cp_outbound.safe_urlopen(req, timeout=5)
    assert calls == [], "闸拒=urlopen 不得被调用"


# ---- cp_request：mine_qa / import_xkt_qa 形状 ----


def test_cp_request_roundtrip_headers_and_body(monkeypatch):
    calls: list = []
    _stub_urlopen(monkeypatch, body=b'{"ok": true}', calls=calls)
    out = cp_outbound.cp_request(
        "http://127.0.0.1:8000", "/api/qa-entries", "tok-1",
        method="POST", payload={"question_text": "你哋幾時到"},
    )
    assert out == {"ok": True}
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "http://127.0.0.1:8000/api/qa-entries"
    assert call["timeout"] == 30.0
    assert call["headers"].get("Content-type") == "application/json"
    assert call["headers"].get("Authorization") == "Bearer tok-1"
    # mine_qa 家族形状：ensure_ascii 缺省 True（非 ASCII 转义）
    assert call["data"] == json.dumps({"question_text": "你哋幾時到"}).encode("utf-8")


def test_cp_request_no_token_no_auth_header(monkeypatch):
    calls: list = []
    _stub_urlopen(monkeypatch, body=b"[]", calls=calls)
    assert cp_outbound.cp_request("http://127.0.0.1:8000", "/api/x", "") == []
    assert "Authorization" not in calls[0]["headers"]


def test_cp_request_rejects_before_urlopen(monkeypatch):
    calls: list = []
    _stub_urlopen(monkeypatch, calls=calls)
    with pytest.raises(PermissionError):
        cp_outbound.cp_request("http://user:pass@127.0.0.1:8000", "/api/x", "t")
    with pytest.raises(PermissionError):
        cp_outbound.cp_request("http://example.com", "/api/x", "t")
    assert calls == []


# ---- cp_request_status：load_intent_catalog 形状 ----


def test_cp_request_status_empty_body_is_none_and_content_type_gate(monkeypatch):
    calls: list = []
    _stub_urlopen(monkeypatch, body=b"", calls=calls)
    status, parsed = cp_outbound.cp_request_status("http://127.0.0.1:8000", "/api/templates")
    assert (status, parsed) == (200, None)
    # GET 无 body：不带 Content-Type（load_intent_catalog 旧形状）
    assert "Content-type" not in calls[0]["headers"]


def test_cp_request_status_body_ensure_ascii_false_and_header(monkeypatch):
    calls: list = []
    _stub_urlopen(monkeypatch, body=b'{"revision": 3}', calls=calls)
    status, parsed = cp_outbound.cp_request_status(
        "http://127.0.0.1:8000", "/api/templates/t1", token="tk",
        method="PUT", payload={"graph_json": "投訴"},
    )
    assert (status, parsed) == (200, {"revision": 3})
    assert calls[0]["headers"].get("Content-type") == "application/json"
    assert calls[0]["headers"].get("Authorization") == "Bearer tk"
    # load_intent_catalog 旧形状：ensure_ascii=False（中文直出 UTF-8）
    assert calls[0]["data"] == json.dumps({"graph_json": "投訴"}, ensure_ascii=False).encode()


def test_cp_request_status_http_error_captured_as_tuple(monkeypatch):
    def _fake(req, data=None, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad", None, io.BytesIO(b'{"detail":"bad graph"}'))

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    status, parsed = cp_outbound.cp_request_status("http://127.0.0.1:8000", "/api/templates/t1")
    assert (status, parsed) == (400, {"detail": "bad graph"})


def test_cp_request_status_http_error_non_json_body_raw(monkeypatch):
    def _fake(req, data=None, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 502, "Bad gw", None, io.BytesIO(b"<html>gateway</html>"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    status, parsed = cp_outbound.cp_request_status("http://127.0.0.1:8000", "/api/templates/t1")
    assert status == 502
    assert parsed == "<html>gateway</html>"


# ---- httpx_send：探针族内核通道（httpx.Response 原生面契约）----


def test_httpx_send_passthrough_and_guard(monkeypatch):
    import httpx

    sent: list[dict] = []

    def _fake_request(method, url, **kw):
        sent.append({"method": method, "url": url, **kw})
        return httpx.Response(200, json={"ok": 1})

    monkeypatch.setattr(httpx, "request", _fake_request)
    resp = cp_outbound.httpx_send(
        "GET", "http://127.0.0.1:8000/api/calls/c1/turns",
        params=None, json=None, data=None, timeout=10.0, headers={"Authorization": "Bearer tk"},
    )
    assert resp.json() == {"ok": 1}
    assert sent == [{
        "method": "GET", "url": "http://127.0.0.1:8000/api/calls/c1/turns",
        "params": None, "json": None, "data": None, "timeout": 10.0,
        "headers": {"Authorization": "Bearer tk"},
    }]
    with pytest.raises(PermissionError):
        cp_outbound.httpx_send("GET", "http://example.com/api/x")
    assert len(sent) == 1, "闸拒=httpx 不得被调用"
