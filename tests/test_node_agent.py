"""node-agent 守护（spec §4.2/§4.3）：心跳一次性调用、失联拒新 job 纯函数、
UI 运行时配置注入文件内容、节点本地 UI 静态托管（SPA 回退）。全部 monkeypatch，
不连真实 CP。"""

from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from node_agent import (
    NodeConfig,
    build_ui_server,
    dispatch_commands,
    heartbeat_once,
    should_refuse_jobs,
    write_ui_config,
)


def _cfg(**kw) -> NodeConfig:
    return NodeConfig(cp_url="http://cp.test", node_token="tok-1", **kw)


def test_heartbeat_once_posts_bearer_and_parses_commands():
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        body = json.loads(req.data.decode())
        # P1 起心跳体带机器指纹；P3 起带 version 与 acks。
        assert body == {"metrics": {"gpu": 0.5}, "fingerprint": "",
                        "version": "v0.3.0",
                        "acks": [{"id": "c1", "ok": False, "result": "boom"}]}

        class R:
            def read(self, n=-1):
                return json.dumps({"ok": True, "commands": [{"type": "noop"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    cfg = _cfg(version="v0.3.0")
    with patch("node_agent.urllib.request.urlopen", fake_urlopen):
        ok, resp = heartbeat_once(
            cfg, metrics={"gpu": 0.5},
            acks=[{"id": "c1", "ok": False, "result": "boom"}])
    assert ok is True
    assert resp["commands"][0]["type"] == "noop"
    assert captured["url"].endswith("/api/nodes/heartbeat")
    assert captured["headers"]["Authorization"] == "Bearer tok-1"


def test_dispatch_commands_actions_and_failure_acks(monkeypatch):
    """指令分发表：shutdown=0 / restart=75 / update 成功=75、失败=ack 回执不退场。"""
    import node_agent as na

    exits = []
    stopped = []
    monkeypatch.setattr(na, "_request_exit", lambda code: exits.append(code))
    monkeypatch.setattr(na, "_stop_stack_quiet", lambda reason: stopped.append(reason))
    hb = na.HeartbeatState()

    # shutdown → exit 0（保持死亡，RestartOnFailure 不拉回）
    dispatch_commands(_cfg(), [{"id": "a", "action": "shutdown"}], hb=hb)
    assert exits == [0]
    # restart → exit 75
    dispatch_commands(_cfg(), [{"id": "b", "action": "restart"}], hb=hb)
    assert exits == [0, 75]
    # update 成功 → exit 75
    monkeypatch.setattr(na, "perform_update", lambda cfg, ver, **kw: "")
    dispatch_commands(_cfg(version="v0"), [{"id": "c", "action": "update",
                                            "args": {"version": "v1"}}], hb=hb)
    assert exits == [0, 75, 75]
    # update 失败 → ack 回执（ok=false），进程不退场，继续旧版本服务
    monkeypatch.setattr(na, "perform_update", lambda cfg, ver, **kw: "download failed")
    dispatch_commands(_cfg(version="v0"), [{"id": "d", "action": "update",
                                            "args": {"version": "v1"}}], hb=hb)
    assert exits == [0, 75, 75]  # 无新增
    assert hb.pending_acks == [{"id": "d", "ok": False, "result": "download failed"}]
    # 未知动作：忽略
    dispatch_commands(_cfg(), [{"id": "e", "action": "rm -rf /"}], hb=hb)
    assert exits == [0, 75, 75]


def test_perform_update_verifies_downloads_and_overlays(tmp_path, monkeypatch):
    """update 执行体：tar.gz+sha256 下载校验 → 覆盖代码树；runtime 缺盘=只换代码。"""
    import io
    import tarfile

    import node_agent as na

    # 组一个假工件：tools/node_agent.py + VERSION(v9.9) + apps/x.py
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("pkg/tools/node_agent.py", b"# new agent"),
                           ("pkg/VERSION", b"v9.9\n"),
                           ("pkg/apps/x.py", b"# new app")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    payload = buf.getvalue()
    import hashlib

    good_sha = hashlib.sha256(payload).hexdigest() + "  bok-node-v9.9.tar.gz\n"

    def fake_download(url, token, dest, timeout=300):
        Path(dest).write_bytes(payload if str(dest).endswith(".tar.gz")
                               else good_sha.encode())

    monkeypatch.setattr(na, "_http_download", fake_download)
    root = tmp_path / "root"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "node_agent.py").write_text("# old")
    cfg = _cfg(version="v0.1.0")
    err = na.perform_update(cfg, "v9.9", root=root, stop_stack=False)
    assert err == ""
    assert (root / "VERSION").read_text().strip() == "v9.9"
    assert (root / "apps" / "x.py").read_text() == "# new app"
    assert (root / "tools" / "node_agent.py").read_text() == "# new agent"


def test_perform_update_rejects_bad_sha_and_bad_version(tmp_path, monkeypatch):
    import hashlib

    import node_agent as na

    cfg = _cfg(version="v0.1.0")
    assert na.perform_update(cfg, "", root=tmp_path) == "update: empty version"
    assert na.perform_update(cfg, "v0.1.0", root=tmp_path) == "update: already at v0.1.0"
    # 非法版本号（URL 路径段白名单外）——防御性拒绝，不发起任何请求
    assert "invalid version" in na.perform_update(cfg, "../etc", root=tmp_path)
    assert "invalid version" in na.perform_update(cfg, "a/b", root=tmp_path)

    def fake_download(url, token, dest, timeout=300):
        Path(dest).write_bytes(b"tampered" if str(dest).endswith(".tar.gz")
                               else (hashlib.sha256(b"other").hexdigest() + " x\n").encode())

    monkeypatch.setattr(na, "_http_download", fake_download)
    err = na.perform_update(cfg, "v9.9", root=tmp_path)
    assert err.startswith("sha256 mismatch")


def test_should_refuse_jobs_after_max_missed():
    assert should_refuse_jobs(0, 3) is False
    assert should_refuse_jobs(2, 3) is False
    assert should_refuse_jobs(3, 3) is True


def test_write_ui_config(tmp_path):
    out = write_ui_config(tmp_path, cp_url="https://cp.example.com", livekit_url="ws://10.0.0.5:7880")
    data = out.read_text()
    assert out.name == "runtime-config.js"
    assert "https://cp.example.com" in data and "ws://10.0.0.5:7880" in data


def _get(host: str, port: int, path: str) -> tuple[int, str]:
    """测试本地 UI 服务（仅 127.0.0.1 回环——测试自建自打，不出网）。"""
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode(errors="replace")
    finally:
        conn.close()


def test_ui_server_serves_spa_routes_and_real_assets(tmp_path):
    """UI 托管契约：/ 出口页；无扩展名路由回 index.html（前端路由接管）；
    带扩展名的缺失资产保留真 404。"""
    (tmp_path / "index.html").write_text("<html>bok-spa-entry</html>")
    (tmp_path / "runtime-config.js").write_text("window.__BOK_CONFIG__={};")
    (tmp_path / "_next").mkdir()
    (tmp_path / "_next" / "app.js").write_text("/*asset*/")

    server = build_ui_server(tmp_path, bind="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        # 出口页
        status, body = _get("127.0.0.1", port, "/")
        assert status == 200 and "bok-spa-entry" in body
        # 注入的配置文件可达
        status, body = _get("127.0.0.1", port, "/runtime-config.js")
        assert status == 200 and "__BOK_CONFIG__" in body
        # 无扩展名路由（客户端路由）→ SPA 回退
        status, body = _get("127.0.0.1", port, "/calls")
        assert status == 200 and "bok-spa-entry" in body
        # 真实资产存在 → 原样出
        status, body = _get("127.0.0.1", port, "/_next/app.js")
        assert status == 200 and "asset" in body
        # 缺失资产（带扩展名）→ 真 404 不吞
        status, _ = _get("127.0.0.1", port, "/_next/missing.js")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


def test_build_ui_server_requires_entry_html(tmp_path):
    try:
        build_ui_server(tmp_path, bind="127.0.0.1", port=0)
    except FileNotFoundError as exc:
        assert "index.html" in str(exc)
    else:
        raise AssertionError("empty ui-dir should refuse to serve")
