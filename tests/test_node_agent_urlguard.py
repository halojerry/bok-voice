"""node_agent 出站守卫接线测试（Mimosa SSRF 修复，2026-09-23）。

validate_cp_base 的「永不合法集」与合法三态、_NoRedirect 禁随、
_http_download dest 断言。全部离线（不打网络）。
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "tools"), str(ROOT / "packages" / "core")):
    if p not in sys.path:
        sys.path.insert(0, p)

import node_agent  # noqa: E402


def test_validate_cp_base_accepts_operator_legit_forms():
    # 云端公网 / 内网 CP / dev 缺省环回 —— 节点形态三态皆合法
    assert node_agent.validate_cp_base("http://127.0.0.1:8000")
    assert node_agent.validate_cp_base("https://cp.example.net")
    assert node_agent.validate_cp_base("http://10.1.2.3:8000")


def test_validate_cp_base_fatals_on_never_legit_set():
    for bad in (
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]/x",
        "ftp://cp.example.net/x",
        "http://224.0.0.1/x",
        "http://0.0.0.0/x",
        "http://fe80::1/x",  # 裸 IPv6 残缺形状
        "not a url",
    ):
        with pytest.raises(SystemExit, match="CP 基址不合规"):
            node_agent.validate_cp_base(bad)


def test_opener_does_not_follow_redirects():
    # Bearer 凭据不跟随重定向：30x 一律 RuntimeError（bok._NoRedirect 同款）
    handler = node_agent._NoRedirect()
    with pytest.raises(RuntimeError, match="redirect not allowed"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example.net/x")


def test_http_download_rejects_relative_or_traversal_dest():
    with pytest.raises(ValueError, match="absolute"):
        node_agent._http_download("http://127.0.0.1/x", "t", Path("rel/out.tar.gz"))
    with pytest.raises(ValueError, match="absolute"):
        node_agent._http_download("http://127.0.0.1/x", "t", Path("/tmp/../etc/passwd"))


def test_post_json_uses_guarded_opener(monkeypatch):
    # _post_json 走 _OPENER（禁随重定向面）而非裸 urlopen
    opened = []

    class _FakeResp:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeOpener:
        def open(self, req, timeout=None):
            opened.append((req.full_url, timeout))
            return _FakeResp()

    monkeypatch.setattr(node_agent, "_OPENER", _FakeOpener())
    code, body = node_agent._post_json("http://127.0.0.1:8000/api/nodes/register", {"a": 1})
    assert code == 200 and body == {"ok": True}
    assert opened and opened[0][0].endswith("/api/nodes/register")
