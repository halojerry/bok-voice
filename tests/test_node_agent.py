"""node-agent 守护（spec §4.2/§4.3）：心跳一次性调用、失联拒新 job 纯函数、
UI 运行时配置注入文件内容。全部 monkeypatch，不连真实 CP。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from node_agent import NodeConfig, heartbeat_once, should_refuse_jobs, write_ui_config


def _cfg(**kw) -> NodeConfig:
    return NodeConfig(cp_url="http://cp.test", node_token="tok-1", **kw)


def test_heartbeat_once_posts_bearer_and_parses_commands():
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        body = json.loads(req.data.decode())
        assert body == {"metrics": {"gpu": 0.5}}

        class R:
            def read(self, n=-1):
                return json.dumps({"ok": True, "commands": [{"type": "noop"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    with patch("node_agent.urllib.request.urlopen", fake_urlopen):
        ok, resp = heartbeat_once(_cfg(), metrics={"gpu": 0.5})
    assert ok is True
    assert resp["commands"][0]["type"] == "noop"
    assert captured["url"].endswith("/api/nodes/heartbeat")
    assert captured["headers"]["Authorization"] == "Bearer tok-1"


def test_should_refuse_jobs_after_max_missed():
    assert should_refuse_jobs(0, 3) is False
    assert should_refuse_jobs(2, 3) is False
    assert should_refuse_jobs(3, 3) is True


def test_write_ui_config(tmp_path):
    out = write_ui_config(tmp_path, cp_url="https://cp.example.com", livekit_url="ws://10.0.0.5:7880")
    data = out.read_text()
    assert out.name == "runtime-config.js"
    assert "https://cp.example.com" in data and "ws://10.0.0.5:7880" in data
