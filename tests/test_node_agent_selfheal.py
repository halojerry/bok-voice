"""2026-09-16 深测修复回归：node-agent token 失效自愈与状态文件权限。"""
from __future__ import annotations

import os
import stat
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import node_agent


def _cfg(tmp_path):
    return node_agent.NodeConfig(cp_url="http://cp.test", node_token="dead-token",
                                 heartbeat_interval_s=60, fingerprint="f" * 64)


def test_heartbeat_tick_reregisters_on_unknown_token(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    calls = []

    def fake_once(c, metrics=None):
        calls.append(c.node_token)
        return (False, {"detail": "unknown node token"}) if c.node_token == "dead-token" \
            else (True, {})

    def fake_ensure(cp_url, license_key, fingerprint, state_file):
        cfg.node_token = "fresh-token"
        return "fresh-token"

    monkeypatch.setattr(node_agent, "heartbeat_once", fake_once)
    monkeypatch.setattr(node_agent, "ensure_token", fake_ensure)
    missed = node_agent.heartbeat_tick(cfg, 2, license_key="bokn_k",
                                       state_file=tmp_path / "state.json")
    assert missed == 0 and cfg.node_token == "fresh-token"
    assert calls == ["dead-token", "fresh-token"]


def test_heartbeat_tick_counts_missed_without_license_flow(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None: (False, {"detail": "network"}))
    assert node_agent.heartbeat_tick(cfg, 1) == 2  # 无 license 流不自愈


def test_ensure_token_writes_state_file_0600_from_creation(monkeypatch, tmp_path):
    state = tmp_path / "sub" / "node-state.json"
    monkeypatch.setattr(node_agent, "register_once",
                        lambda *a, **k: ("node-1", "tok-1"))
    monkeypatch.setattr(node_agent, "_post_json",
                        lambda *a, **k: (401, {}))  # 缓存探测直接 401
    token = node_agent.ensure_token("http://cp.test", "bokn_k", "f" * 64, state)
    assert token == "tok-1"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600


def test_heartbeat_tick_swallows_reregister_network_error(monkeypatch, tmp_path):
    """重注册窗口内网络抖动（URLError）不得穿透 tick 令守护进程退出/线程死亡。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None: (False, {"detail": "unknown node token"}))

    def fake_ensure(cp_url, license_key, fingerprint, state_file):
        raise urllib.error.URLError("network blip")

    monkeypatch.setattr(node_agent, "ensure_token", fake_ensure)
    assert node_agent.heartbeat_tick(cfg, 2, license_key="bokn_k",
                                     state_file=tmp_path / "state.json") == 3


def test_ensure_token_repairs_stale_0644_state_file(monkeypatch, tmp_path):
    """O_TRUNC 对已存在文件保留旧 mode——旧版 0644 崩溃残档写出后必须扳回 0600。"""
    state = tmp_path / "node-state.json"
    state.write_text('{"node_id": "old", "node_token": "stale"}', encoding="utf-8")
    os.chmod(state, 0o644)
    monkeypatch.setattr(node_agent, "register_once",
                        lambda *a, **k: ("node-1", "tok-1"))
    monkeypatch.setattr(node_agent, "_post_json",
                        lambda *a, **k: (401, {}))  # 缓存探测 401 → 重注册覆盖
    token = node_agent.ensure_token("http://cp.test", "bokn_k", "f" * 64, state)
    assert token == "tok-1"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
