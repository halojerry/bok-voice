"""BOK_WORKER_PORT 做实（2026-10-02 编排审计第二波 · PR-E item 2）。

背景：agent worker 自己读 `BOK_WORKER_PORT`（agent.py 缺省 8081），但 bok.py
硬编码 8081 于 worker specs / 就绪等待 / 孤儿清扫 / prod status / monitor——
错开端口（单机多栈并存）时探活/清扫全打缺省口。本文件钉 `_agent_worker_port`
的解析语义 + 各 AGENT-WORKER 专属站点的传播（B 线 interp 8082/8083 不读该键，
保持固定）。默认档与旧行为逐字节一致（WORKER_PORTS 静态表不变）。

约定：不碰真进程/真 app-data；_start_proc 全部打桩（照 test_linux_node_wiring
的桩法）。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402


# ---------------------------------------------------------------------------
# helper 解析语义
# ---------------------------------------------------------------------------


def test_default_and_env_override(monkeypatch) -> None:
    monkeypatch.delenv("BOK_WORKER_PORT", raising=False)
    assert bok._agent_worker_port() == 8081
    monkeypatch.setenv("BOK_WORKER_PORT", "9081")
    assert bok._agent_worker_port() == 9081


def test_blank_and_garbage_fall_back(monkeypatch) -> None:
    """空串/纯空白/非数字/越界 → 缺省 8081（错值不把探活指去死口）。"""
    for bad in ("", "   ", "not-a-port", "0", "-1", "70000"):
        monkeypatch.setenv("BOK_WORKER_PORT", bad)
        assert bok._agent_worker_port() == 8081, bad


def test_worker_ports_dynamic_agent_entry(monkeypatch) -> None:
    """动态 worker 表：agent 口吃 helper，interp 两件固定 8082/8083。"""
    monkeypatch.delenv("BOK_WORKER_PORT", raising=False)
    assert bok._worker_ports() == bok.WORKER_PORTS
    monkeypatch.setenv("BOK_WORKER_PORT", "9081")
    assert bok._worker_ports() == (
        ("agent-worker", 9081),
        ("interp-fwd", 8082),
        ("interp-rev", 8083),
    )
    # 静态默认表不随 env 漂移（健康面单点表语义不变）。
    assert bok.WORKER_PORTS == (
        ("agent-worker", 8081),
        ("interp-fwd", 8082),
        ("interp-rev", 8083),
    )


# ---------------------------------------------------------------------------
# AGENT-WORKER 专属站点传播
# ---------------------------------------------------------------------------


def test_worker_specs_port_follows_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bok, "_agent_worker_env", lambda py: {})
    monkeypatch.setattr(bok, "_realtime_demo_enabled", lambda: False)
    monkeypatch.delenv("BOK_WORKER_PORT", raising=False)
    specs = bok._worker_specs("/py")
    assert specs[0]["port"] == 8081
    monkeypatch.setenv("BOK_WORKER_PORT", "9081")
    specs = bok._worker_specs("/py")
    assert specs[0]["port"] == 9081
    assert [s["port"] for s in specs[1:]] == [8082, 8083], "interp 两件保持固定"


def test_desktop_targets_follow_env(monkeypatch) -> None:
    monkeypatch.delenv("BOK_WORKER_PORT", raising=False)
    assert bok._desktop_stack_targets() == [8000, 8787, 1235, 7880, 8081, 8082, 8083]
    monkeypatch.setenv("BOK_WORKER_PORT", "9081")
    assert bok._desktop_stack_targets() == [8000, 8787, 1235, 7880, 9081, 8082, 8083]


def test_orphan_owners_follow_env(monkeypatch) -> None:
    """孤儿清扫端口表：agent 条目吃 helper（身份标记不变），其余原样。"""
    monkeypatch.delenv("BOK_WORKER_PORT", raising=False)
    assert bok._orphan_port_owners() == bok._ORPHAN_PORT_OWNERS
    monkeypatch.setenv("BOK_WORKER_PORT", "9081")
    owners = dict(bok._orphan_port_owners())
    assert "agent_runtime" in " ".join(owners[9081])
    assert 8081 not in owners
    assert 8082 in owners and 8083 in owners, "interp 条目不动"
    assert 8787 in owners, "非 worker 条目不动"


def test_sweep_listeners_uses_dynamic_owners() -> None:
    """源级 pin：孤儿清扫迭代动态表（静态表是默认档，env 档必须同样被扫）。"""
    src = inspect.getsource(bok._sweep_orphan_listeners)
    assert "_orphan_port_owners()" in src, (
        "_sweep_orphan_listeners must iterate _orphan_port_owners()")


def test_shared_iteration_sites_use_dynamic_table() -> None:
    """三张共用健康面（status/doctor/prod status）迭代动态 worker 表。"""
    for fn in (bok.cmd_status, bok.cmd_doctor, bok.cmd_prod_status):
        src = inspect.getsource(fn)
        assert "_worker_ports()" in src, (
            f"{fn.__name__} must iterate _worker_ports() so BOK_WORKER_PORT "
            "does not desync health probes")


def test_monitor_banner_and_serve_wiring() -> None:
    """monitor 横幅与 serve 等待环都必须吃动态口。"""
    assert "_agent_worker_port()" in inspect.getsource(bok.cmd_monitor)
    assert "_desktop_stack_targets()" in inspect.getsource(bok.cmd_serve)


def test_relaxed_healthy_worker_http_surface_follows_env(monkeypatch) -> None:
    """放宽探活：错开端口仍有 /worker HTTP 面（否则退 TCP 丢真端点语义）。"""
    monkeypatch.setenv("BOK_WORKER_PORT", "9081")
    seen: list[str] = []

    def fake_urlopen(url, timeout=None):
        seen.append(url)
        raise OSError("boom")

    monkeypatch.setattr(bok.urllib.request, "urlopen", fake_urlopen)
    assert bok._relaxed_healthy(9081) is False
    assert seen and seen[0].endswith(":9081/worker"), seen
