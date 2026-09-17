"""2026-09-16 深测修复回归：node-agent token 失效自愈与状态文件权限。
site-delivery Task 6 扩充：远程停机开关服从（root 吊销停栈退出 / license 吊销
退避致命 / auto_clone 复活保留 / 注册 sticky 拒绝致命），全部 monkeypatch 不连真 CP。"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import node_agent
import pytest


def _cfg(tmp_path):
    return node_agent.NodeConfig(cp_url="http://cp.test", node_token="dead-token",
                                 heartbeat_interval_s=60, fingerprint="f" * 64)


def test_heartbeat_tick_reregisters_on_unknown_token(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    calls = []

    def fake_once(c, metrics=None, acks=None):
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
                        lambda c, metrics=None, acks=None: (False, {"detail": "network"}))
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
                        lambda c, metrics=None, acks=None: (False, {"detail": "unknown node token"}))

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


# ---- Task 6：远程停机开关服从（root 吊销=停栈退出 / license 吊销=退避致命 /
# auto_clone=复活保留 / 注册 sticky=致命），wire 契约=control_plane/main.py
# node_heartbeat 401 detail 塑形 + scripts/probe_killswitch.py ④⑤⑦⑨。----

_SHUTDOWN_BODY = {"detail": {"reason": "node revoked", "action": "shutdown"}}


def test_classify_heartbeat_failure_matrix():
    """detail 三形态（dict / 纯文本 / 缺失）全矩阵：分类即处置的单一事实源。"""
    f = node_agent.classify_heartbeat_failure
    # root 吊销=结构化 shutdown 指令（dict，action 大小写不敏感）
    assert f(_SHUTDOWN_BODY) == "root_revoked"
    assert f({"detail": {"reason": "node revoked", "action": "Shutdown"}}) == "root_revoked"
    # dict 但无 action（防御支）：按文本走 auto_clone（复活路径保留，不得误杀）
    assert f({"detail": {"reason": "node revoked"}}) == "auto_clone_revoked"
    # auto_clone 克隆吊销=纯文本 node revoked
    assert f({"detail": "node revoked"}) == "auto_clone_revoked"
    # license 吊销=纯文本两种拼法（CP 线上 "license revoked"，store reason 连写式）
    assert f({"detail": "license revoked"}) == "license_revoked"
    assert f({"detail": "license_revoked"}) == "license_revoked"
    assert f({"detail": "unknown node token"}) == "token_stale"
    assert f({"detail": "license_required"}) == "unlicensed"
    assert f({"detail": "node not licensed (hardened mode)"}) == "unlicensed"
    # 缺失 detail / 空 body / None=连接异常路（heartbeat_once except 返回 {}）
    assert f({}) == "network"
    assert f(None) == "network"


def test_root_revoked_dict_stops_stack_and_exits_no_reregister(monkeypatch, tmp_path):
    """root 吊销（dict action=shutdown）：不重注册、停栈钩子执行、进程干净退出 0。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("BOK_NODE_KILL_ON_REVOKE", "1")
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None, acks=None: (False, _SHUTDOWN_BODY))
    stops, registers = [], []
    monkeypatch.setattr(node_agent, "_kill_stack_hook", lambda: stops.append(1))
    monkeypatch.setattr(node_agent, "ensure_token",
                        lambda *a, **k: registers.append(1) or ("node-x", "tok-x"))
    with pytest.raises(SystemExit) as ei:
        node_agent.heartbeat_tick(cfg, 0, license_key="bokn_k",
                                  state_file=tmp_path / "state.json")
    assert ei.value.code == 0
    assert stops == [1] and registers == []  # 自愈必须让位：绝不重注册


def test_heartbeat_loop_exits_process_on_root_revoked(monkeypatch, tmp_path):
    """进程退出路径走通：heartbeat_loop 内 tick 熔断 → SystemExit 传导出循环。"""
    cfg = _cfg(tmp_path)
    cfg.heartbeat_interval_s = 0
    monkeypatch.setenv("BOK_NODE_KILL_ON_REVOKE", "1")
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None, acks=None: (False, _SHUTDOWN_BODY))
    stops = []
    monkeypatch.setattr(node_agent, "_kill_stack_hook", lambda: stops.append(1))
    with pytest.raises(SystemExit) as ei:
        node_agent.heartbeat_loop(cfg, threading.Event(), license_key="bokn_k",
                                  state_file=tmp_path / "state.json")
    assert ei.value.code == 0 and stops == [1]


def test_root_revoked_kills_without_license_flow(monkeypatch, tmp_path):
    """--node-token 直连模式（无 license 流）同样必须服从停机指令。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("BOK_NODE_KILL_ON_REVOKE", "1")
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None, acks=None: (False, _SHUTDOWN_BODY))
    stops = []
    monkeypatch.setattr(node_agent, "_kill_stack_hook", lambda: stops.append(1))
    with pytest.raises(SystemExit):
        node_agent.heartbeat_tick(cfg, 0)
    assert stops == [1]


def test_root_revoked_observe_only_env_disables_kill(monkeypatch, tmp_path, capsys):
    """BOK_NODE_KILL_ON_REVOKE=0 观察档：逐轮大声日志、不停栈不退出、按失联计数。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("BOK_NODE_KILL_ON_REVOKE", "0")
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None, acks=None: (False, _SHUTDOWN_BODY))
    stops, registers = [], []
    monkeypatch.setattr(node_agent, "_kill_stack_hook", lambda: stops.append(1))
    monkeypatch.setattr(node_agent, "ensure_token",
                        lambda *a, **k: registers.append(1) or ("node-x", "tok-x"))
    assert node_agent.heartbeat_tick(cfg, 0, license_key="bokn_k",
                                     state_file=tmp_path / "state.json") == 1
    assert node_agent.heartbeat_tick(cfg, 1, license_key="bokn_k",
                                     state_file=tmp_path / "state.json") == 2
    assert stops == [] and registers == []
    assert capsys.readouterr().out.count("KILLSWITCH (observe-only)") == 2


def test_auto_clone_revoked_keeps_self_heal(monkeypatch, tmp_path):
    """纯文本 "node revoked"（克隆检出 auto_clone）=复活路径保留：重注册自愈，
    绝不触发 kill（回归钉死既有行为）。"""
    cfg = _cfg(tmp_path)
    calls = []

    def fake_once(c, metrics=None, acks=None):
        calls.append(c.node_token)
        return (False, {"detail": "node revoked"}) if c.node_token == "dead-token" \
            else (True, {})

    def fake_ensure(cp_url, license_key, fingerprint, state_file):
        cfg.node_token = "fresh-token"
        return "fresh-token"

    monkeypatch.setenv("BOK_NODE_KILL_ON_REVOKE", "1")
    monkeypatch.setattr(node_agent, "heartbeat_once", fake_once)
    monkeypatch.setattr(node_agent, "ensure_token", fake_ensure)
    monkeypatch.setattr(node_agent, "_kill_stack_hook",
                        lambda: (_ for _ in ()).throw(AssertionError("clone revoke must not kill")))
    missed = node_agent.heartbeat_tick(cfg, 5, license_key="bokn_k",
                                       state_file=tmp_path / "state.json")
    assert missed == 0 and cfg.node_token == "fresh-token"
    assert calls == ["dead-token", "fresh-token"]


def test_license_revoked_fatal_after_three_consecutive(monkeypatch, tmp_path):
    """license 吊销（重注册无出路）：×2 存活（退避观察窗），×3 走 kill 路径。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("BOK_NODE_KILL_ON_REVOKE", "1")
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None, acks=None: (False, {"detail": "license revoked"}))
    stops, registers = [], []
    monkeypatch.setattr(node_agent, "_kill_stack_hook", lambda: stops.append(1))
    monkeypatch.setattr(node_agent, "ensure_token",
                        lambda *a, **k: registers.append(1) or ("node-x", "tok-x"))
    hb = node_agent.HeartbeatState()
    assert node_agent.heartbeat_tick(cfg, 0, license_key="bokn_k",
                                     state_file=tmp_path / "s.json", hb=hb) == 1
    assert node_agent.heartbeat_tick(cfg, 1, license_key="bokn_k",
                                     state_file=tmp_path / "s.json", hb=hb) == 2
    assert stops == [] and registers == [] and hb.license_revoked_streak == 2
    with pytest.raises(SystemExit) as ei:
        node_agent.heartbeat_tick(cfg, 2, license_key="bokn_k",
                                  state_file=tmp_path / "s.json", hb=hb)
    assert ei.value.code == 0
    assert stops == [1] and registers == []


def test_license_revoked_streak_resets_on_success(monkeypatch, tmp_path):
    """偶发 license_revoked 后恢复成功 → 连击清零，不得跨轮累计假致命。"""
    cfg = _cfg(tmp_path)
    responses = [
        (False, {"detail": "license revoked"}),
        (False, {"detail": "license revoked"}),
        (True, {}),
        (False, {"detail": "license revoked"}),
        (False, {"detail": "license revoked"}),
    ]
    monkeypatch.setenv("BOK_NODE_KILL_ON_REVOKE", "1")
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None, acks=None: responses.pop(0))
    stops = []
    monkeypatch.setattr(node_agent, "_kill_stack_hook", lambda: stops.append(1))
    hb = node_agent.HeartbeatState()
    # 心跳循环自然序列：失联计 0→1→2，成功归 0，再 0→1→2（不得累计成 5 连击）
    for missed_in, expected in zip((0, 1, 2, 0, 1), (1, 2, 0, 1, 2)):
        assert node_agent.heartbeat_tick(cfg, missed_in, license_key="bokn_k",
                                         state_file=tmp_path / "s.json", hb=hb) == expected
    assert stops == [] and hb.license_revoked_streak == 2


def test_register_once_node_revoked_is_fatal(monkeypatch):
    """注册撞 root 吊销 sticky 拒绝：RegisterRevoked 致命退出+明文一行（无重试）。"""
    monkeypatch.setattr(node_agent, "_post_json",
                        lambda *a, **k: (401, {"detail": "node revoked"}))
    with pytest.raises(SystemExit) as ei:
        node_agent.register_once("http://cp.test", "bokn_k", "f" * 64)
    assert "FATAL" in str(ei.value.code) and "revoked" in str(ei.value.code)


def test_register_once_license_revoked_is_fatal(monkeypatch):
    monkeypatch.setattr(node_agent, "_post_json",
                        lambda *a, **k: (401, {"detail": "license revoked"}))
    with pytest.raises(SystemExit) as ei:
        node_agent.register_once("http://cp.test", "bokn_k", "f" * 64)
    assert "FATAL" in str(ei.value.code) and "revoked" in str(ei.value.code)


def test_register_once_non_revoked_failure_stays_ordinary_exit(monkeypatch):
    """配额/未知 key 等非吊销失败维持原 SystemExit 语义（无 FATAL 前缀）。"""
    monkeypatch.setattr(node_agent, "_post_json",
                        lambda *a, **k: (403, {"detail": "license quota exhausted (1/1)"}))
    with pytest.raises(SystemExit) as ei:
        node_agent.register_once("http://cp.test", "bokn_k", "f" * 64)
    assert "FATAL" not in str(ei.value.code) and "quota" in str(ei.value.code)


def test_ensure_token_probe_shutdown_body_kills_without_reregister(monkeypatch, tmp_path):
    """缓存 token 探测命中 root 吊销 shutdown 指令：直接停机退出，绝不重注册。"""
    state = tmp_path / "node-state.json"
    state.write_text(json.dumps({"node_id": "n1", "node_token": "dead"}),
                     encoding="utf-8")
    probed = []

    def fake_post(url, payload, headers=None, timeout=10):
        probed.append(url)
        return (401, _SHUTDOWN_BODY)

    monkeypatch.setattr(node_agent, "_post_json", fake_post)
    monkeypatch.setattr(node_agent, "register_once",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not re-register on shutdown")))
    monkeypatch.setattr(node_agent, "_kill_stack_hook", None)  # 启动早期无栈可停
    with pytest.raises(SystemExit) as ei:
        node_agent.ensure_token("http://cp.test", "bokn_k", "f" * 64, state)
    assert ei.value.code == 0
    assert probed == ["http://cp.test/api/nodes/heartbeat"]


def test_ensure_token_probe_license_revoked_fatal(monkeypatch, tmp_path):
    """探测命中 license 吊销：重注册无出路 → RegisterRevoked 致命退出。"""
    state = tmp_path / "node-state.json"
    state.write_text(json.dumps({"node_id": "n1", "node_token": "dead"}),
                     encoding="utf-8")
    monkeypatch.setattr(node_agent, "_post_json",
                        lambda *a, **k: (401, {"detail": "license revoked"}))
    monkeypatch.setattr(node_agent, "register_once",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not re-register on revoked license")))
    with pytest.raises(SystemExit) as ei:
        node_agent.ensure_token("http://cp.test", "bokn_k", "f" * 64, state)
    assert "FATAL" in str(ei.value.code) and "license revoked" in str(ei.value.code)


def test_heartbeat_tick_does_not_swallow_sticky_register_refusal(monkeypatch, tmp_path):
    """自愈重注册撞 RegisterRevoked：必须原样穿透（不吞成失联计数继续跑）。"""
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("BOK_NODE_KILL_ON_REVOKE", "1")
    monkeypatch.setattr(node_agent, "heartbeat_once",
                        lambda c, metrics=None, acks=None: (False, {"detail": "node revoked"}))

    def fake_ensure(cp_url, license_key, fingerprint, state_file):
        raise node_agent.RegisterRevoked("[node-agent] FATAL: registration refused — revoked.")

    monkeypatch.setattr(node_agent, "ensure_token", fake_ensure)
    with pytest.raises(SystemExit) as ei:
        node_agent.heartbeat_tick(cfg, 2, license_key="bokn_k",
                                  state_file=tmp_path / "state.json")
    assert "FATAL" in str(ei.value.code)
