"""Ubuntu 节点接线（2026-09-20）：平台路径 / 模型表键 / LiveKit 配置补丁 /
通话面单点 的回归钉。

覆盖四个真阻断点：
1. app_data_dir 非 mac 落 XDG（旧版恒写 ~/Library/Application Support，Ubuntu 错目录）；
2. platform_key 非 mac 落 windows 模型表（旧版 Linux 会下 mlx 模型）；
3. bundled_llama 支持 Linux 档（runtime/llama/llama-server）；
4. _livekit_config_path env 覆盖（内网 bind / 云 CP webhook / 生产 keys）；
   与 cmd_up = 服务面 + 通话面 的两段串联（旧版 node_agent 拉不起 LiveKit/worker）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402


# ---- 1. app_data_dir 平台分档 ----


def test_app_data_dir_linux_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(bok._platform, "system", lambda: "Linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert bok.app_data_dir() == tmp_path / ".local" / "share" / "BokVoice"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert bok.app_data_dir() == tmp_path / "xdg" / "BokVoice"


def test_app_data_dir_mac_unchanged(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(bok._platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert bok.app_data_dir() == tmp_path / "Library" / "Application Support" / "BokVoice"


# ---- 2. platform_key / 3. bundled_llama ----


def test_platform_key_linux_uses_gpu_table(monkeypatch):
    monkeypatch.setattr(bok._platform, "system", lambda: "Linux")
    assert bok.platform_key() == "windows"  # llama.cpp GGUF + transformers
    monkeypatch.setattr(bok._platform, "system", lambda: "Darwin")
    assert bok.platform_key() == "mac"
    monkeypatch.setattr(bok._platform, "system", lambda: "Windows")
    assert bok.platform_key() == "windows"


def test_bundled_llama_linux_candidates(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(bok._platform, "system", lambda: "Linux")
    monkeypatch.setattr(bok, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(bok.os, "name", "posix")
    assert bok.bundled_llama() is None
    nested = tmp_path / "llama" / "linux"
    nested.mkdir(parents=True)
    (nested / "llama-server").write_text("x")
    assert bok.bundled_llama() == nested / "llama-server"


def test_bundled_llama_mac_none(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(bok._platform, "system", lambda: "Darwin")
    monkeypatch.setattr(bok, "runtime_root", lambda: tmp_path)
    (tmp_path / "llama").mkdir()
    (tmp_path / "llama" / "llama-server").write_text("x")
    assert bok.bundled_llama() is None  # mac 走 mlx，不吃 llama


# ---- 4. LiveKit 配置补丁 ----


def _write_base_livekit(tmp_path: Path) -> Path:
    base = tmp_path / "livekit.yaml"
    base.write_text(
        "port: 7880\n"
        "bind_addresses:\n"
        "  - 127.0.0.1\n"
        "rtc:\n"
        "  tcp_port: 7881\n"
        "keys:\n"
        "  devkey: devsecret\n"
        "webhook:\n"
        "  urls:\n"
        "    - http://127.0.0.1:8000/api/webhook/livekit\n"
        "  api_key: devkey\n",
        encoding="utf-8",
    )
    return base


@pytest.fixture()
def livekit_env(monkeypatch, tmp_path: Path):
    base_dir = tmp_path / "repo" / "services" / "livekit-server"
    base_dir.mkdir(parents=True)
    base = _write_base_livekit(base_dir)
    monkeypatch.setattr(bok, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path / "appdata")
    for k in ("BOK_LIVEKIT_BIND", "BOK_LIVEKIT_WEBHOOK_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    return base, tmp_path


def test_livekit_config_no_env_returns_shipped(livekit_env):
    base, _tmp = livekit_env
    assert bok._livekit_config_path() == base
    assert base.read_text(encoding="utf-8").startswith("port: 7880")


def test_livekit_config_patches_bind_webhook_keys(livekit_env, monkeypatch):
    base, tmp = livekit_env
    monkeypatch.setenv("BOK_LIVEKIT_BIND", "192.168.1.10,10.0.0.5")
    monkeypatch.setenv("BOK_LIVEKIT_WEBHOOK_URL", "https://cp.example.com/api/webhook/livekit")
    monkeypatch.setenv("LIVEKIT_API_KEY", "prodkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "prodsecret")
    out = bok._livekit_config_path()
    assert out == tmp / "appdata" / "run" / "livekit.yaml"
    text = out.read_text(encoding="utf-8")
    assert "  - 192.168.1.10\n  - 10.0.0.5\n" in text
    assert "127.0.0.1\n" not in text.split("rtc:")[0]  # 旧 bind 已被替换
    assert "    - https://cp.example.com/api/webhook/livekit\n" in text
    assert "http://127.0.0.1:8000/api/webhook/livekit" not in text
    assert "prodkey: prodsecret" in text and "devkey: devsecret" not in text
    assert "port: 7880" in text and "tcp_port: 7881" in text  # 其余段零改动


def test_livekit_config_only_bind_keeps_other_sections(livekit_env, monkeypatch):
    base, tmp = livekit_env
    monkeypatch.setenv("BOK_LIVEKIT_BIND", "192.168.1.10")
    text = bok._livekit_config_path().read_text(encoding="utf-8")
    assert "  - 192.168.1.10\n" in text
    assert "webhook:" in text and "http://127.0.0.1:8000/api/webhook/livekit" in text
    assert "devkey: devsecret" in text


# ---- 5. cmd_up = 服务面 + 通话面 ----


def test_cmd_up_runs_services_then_call_plane(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(bok, "_cmd_up_services", lambda: (calls.append("services"), 0)[1])
    monkeypatch.setattr(bok, "_start_call_plane", lambda py: (calls.append("call_plane"), True)[1])
    assert bok.cmd_up() == 0
    assert calls == ["services", "call_plane"]


def test_cmd_up_skips_call_plane_when_services_fail(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(bok, "_cmd_up_services", lambda: (calls.append("services"), 1)[1])
    monkeypatch.setattr(bok, "_start_call_plane", lambda py: (calls.append("call_plane"), True)[1])
    assert bok.cmd_up() == 1
    assert calls == ["services"]  # 服务面失败即短路，通话面不拉


def test_start_call_plane_waits_livekit_then_starts_workers(monkeypatch, tmp_path: Path):
    """通话面单点：LiveKit 就绪 → 三个 worker 全量 spawn → monitor（与 serve 同源）。"""
    started: list[str] = []
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bok, "_livekit_config_path", lambda: tmp_path / "livekit.yaml")
    monkeypatch.setattr(bok, "_embedded_livekit", lambda: None)
    monkeypatch.setattr(bok, "shutil_which", lambda name: None)
    monkeypatch.setattr(bok, "_start_proc", lambda argv, pidf, logf, **kw: started.append(str(argv[0])))
    monkeypatch.setattr(bok, "healthy", lambda port: port == 7880)
    monkeypatch.setattr(
        bok, "_worker_specs",
        lambda py: [
            {"name": "agent", "port": 8081, "argv": [str(py), "-m", "agent_runtime.main"],
             "pidfile": tmp_path / "a.pid", "logfile": tmp_path / "a.log", "env": {}},
        ],
    )
    monkeypatch.setattr(bok, "_ensure_monitor", lambda py: started.append("monitor"))
    assert bok._start_call_plane("/py") is True
    assert started == ["/py", "monitor"]


def test_start_call_plane_returns_false_without_livekit(monkeypatch, tmp_path: Path):
    started: list[str] = []
    monkeypatch.setattr(bok, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bok, "_livekit_config_path", lambda: tmp_path / "livekit.yaml")
    monkeypatch.setattr(bok, "_embedded_livekit", lambda: None)
    monkeypatch.setattr(bok, "shutil_which", lambda name: None)
    monkeypatch.setattr(bok, "healthy", lambda port: False)
    monkeypatch.setattr(bok, "_start_proc", lambda *a, **kw: started.append("proc"))
    monkeypatch.setattr(bok, "_worker_specs", lambda py: (_ for _ in ()).throw(AssertionError("worker 不应被拉起")))
    monkeypatch.setattr(bok.time, "sleep", lambda s: None)
    monkeypatch.setattr(bok.time, "monotonic", _deadline_clock())
    assert bok._start_call_plane("/py") is False


def _deadline_clock():
    """单调时钟桩：首次 0（未到期），后续跳过大限触发「未就绪 → 报错返回」。"""
    state = {"n": 0}

    def _clock() -> float:
        state["n"] += 1
        return 0.0 if state["n"] == 1 else 10_000.0

    return _clock
