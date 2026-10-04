"""bok up --models-only + model-plane 单元（opt-in）（PR-E item 5）。

背景：重启后 launchd 只拉 CP/LiveKit/worker——模型面（:8787/:1235/:1236/
:1237/:1239，云 TTS 档跳过 :8788）死等到人工跑 bok（已审计事故形状）。本波：
①`bok up --models-only` 只起模型面、跳过通话面；②`prod install
--with-model-plane`（默认 OFF，既有装机零变化）渲染可选 `bok-model-plane`
单元（RunAtLoad+KeepAlive，跑 `bok up --models-only`）。

约定：_start_proc / _cmd_up_services / _start_call_plane 全打桩，不碰真进程。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402


# ---------------------------------------------------------------------------
# ① cmd_up --models-only：只服务面，不碰通话面
# ---------------------------------------------------------------------------


def test_models_only_skips_call_plane(monkeypatch) -> None:
    calls: list[tuple] = []
    patch_bok(
        monkeypatch, "_cmd_up_services",
        lambda models_only=False: (calls.append(("services", models_only)), 0)[1])
    patch_bok(
        monkeypatch, "_start_call_plane",
        lambda py: (calls.append(("call_plane",)), True)[1])
    patch_bok(monkeypatch, "repo_python", lambda: "/py")
    assert bok.cmd_up(models_only=True) == 0
    assert calls == [("services", True)], "models-only 不得拉通话面"
    calls.clear()
    assert bok.cmd_up() == 0
    assert calls == [("services", False), ("call_plane",)], "缺省全栈行为零变化"


def test_models_only_service_set(monkeypatch, tmp_path) -> None:
    """fake _start_proc：模型面服务起（asr/tts/llm/mt/settle），通话面端口不出现。"""
    started: list[str] = []

    def fake_start(args, pidfile, logfile, env=None, cwd=None):
        joined = " ".join(str(a) for a in args)
        for port in ("8787", "8788", "1235", "1236", "1237", "7880", "8081", "8082", "8083"):
            if f"--port {port}" in joined:
                started.append(port)
        return 1

    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "cmd_download", lambda only=None: 0)
    sidecar = tmp_path / "sidecar-py"
    sidecar.write_text("")
    patch_bok(monkeypatch, "sidecar_python", lambda name: sidecar)
    patch_bok(monkeypatch, "model_path", lambda cur, key: str(tmp_path / key))
    patch_bok(monkeypatch, "healthy", lambda port: False)
    monkeypatch.setattr(bok.time, "sleep", lambda s: None)
    patch_bok(monkeypatch, "_start_proc", fake_start)
    patch_bok(monkeypatch, "_local_tts_needed", lambda: (True, "local"))
    patch_bok(monkeypatch, "_start_llm", lambda *a, **k: started.append("llm-lane"))
    patch_bok(monkeypatch, "_start_mt_llm", lambda *a, **k: (started.append("mt-lane"), True)[1])
    patch_bok(monkeypatch, "_start_settle_llm", lambda *a, **k: (started.append("settle-lane"), True)[1])
    patch_bok(monkeypatch, "_start_laya", lambda *a, **k: (started.append("laya-lane"), False)[1])
    patch_bok(monkeypatch, "_ports_down_after_grace", lambda targets, probe=None: [])
    assert bok._cmd_up_services(models_only=True) == 0
    assert "8787" in started and "8788" in started
    assert "llm-lane" in started and "mt-lane" in started and "settle-lane" in started
    for call_port in ("7880", "8081", "8082", "8083"):
        assert call_port not in started, f"通话面 {call_port} 不得被 models-only 拉起"


def test_models_only_argparse_and_main_wiring(monkeypatch) -> None:
    args = bok.parse_args(["up", "--models-only"])
    assert args.cmd == "up" and args.models_only is True
    assert bok.parse_args(["up"]).models_only is False
    seen: dict = {}
    patch_bok(monkeypatch, "cmd_up", lambda models_only=False: (seen.update(models_only=models_only), 0)[1])
    assert bok.main(["up", "--models-only"]) == 0
    assert seen["models_only"] is True


# ---------------------------------------------------------------------------
# ② 可选 model-plane 单元：默认 OFF，--with-model-plane 才渲染
# ---------------------------------------------------------------------------


def _patch_prod_unit_deps(monkeypatch, tmp_path: Path) -> None:
    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "repo_python", lambda: "py")
    patch_bok(monkeypatch, "_embedded_livekit", lambda: None)
    patch_bok(monkeypatch, "_agent_prod_env", lambda: {})
    patch_bok(monkeypatch, "_interp_env", lambda env: {})
    patch_bok(monkeypatch, "_control_plane_env", lambda db: {})


def test_prod_units_model_plane_default_off(monkeypatch, tmp_path) -> None:
    _patch_prod_unit_deps(monkeypatch, tmp_path)
    names = [n for n, *_ in bok.prod._prod_units()]
    assert "bok-model-plane" not in names, "默认渲染必须与既有装机逐字节一致"
    units = {n: args for n, args, _env, _c in bok.prod._prod_units(with_model_plane=True)}
    assert "bok-model-plane" in units
    argv = units["bok-model-plane"]
    assert argv[-2:] == ["up", "--models-only"]
    assert argv[-3].endswith("bok.py"), "单元跑本仓 bok 入口"


def test_prod_install_mac_plist_gated_by_flag(monkeypatch, tmp_path) -> None:
    _patch_prod_unit_deps(monkeypatch, tmp_path)
    patch_bok(monkeypatch, "is_mac", lambda: True)
    patch_bok(monkeypatch, "is_linux", lambda: False)
    assert bok.prod.cmd_prod_install() == 0
    unit_dir = tmp_path / "units"
    assert not (unit_dir / "com.bokvoice.bok-model-plane.plist").exists()
    assert bok.prod.cmd_prod_install(with_model_plane=True) == 0
    plist = unit_dir / "com.bokvoice.bok-model-plane.plist"
    assert plist.exists()
    text = plist.read_text(encoding="utf-8")
    assert "--models-only" in text
    assert "<key>RunAtLoad</key><true/>" in text
    assert "<key>KeepAlive</key><true/>" in text


def test_prod_uninstall_mac_removes_opt_in_model_plane(monkeypatch, tmp_path, capsys) -> None:
    """opt-in 单元装过 → 卸载面覆盖它（bootout+删 plist），不留残件。"""
    import subprocess as _sp

    _patch_prod_unit_deps(monkeypatch, tmp_path)
    patch_bok(monkeypatch, "is_mac", lambda: True)
    patch_bok(monkeypatch, "is_linux", lambda: False)
    monkeypatch.setattr(
        bok.subprocess, "run",
        lambda argv, **kw: _sp.CompletedProcess(argv, 0, stdout="", stderr=""))
    assert bok.prod.cmd_prod_install(with_model_plane=True) == 0
    unit_dir = tmp_path / "units"
    assert (unit_dir / "com.bokvoice.bok-model-plane.plist").exists()
    assert bok.prod.cmd_prod_uninstall() == 0
    assert not (unit_dir / "com.bokvoice.bok-model-plane.plist").exists()
    assert "bok-model-plane" in capsys.readouterr().out


def test_prod_argparse_and_main_wiring(monkeypatch) -> None:
    args = bok.parse_args(["prod", "install", "--with-model-plane"])
    assert args.with_model_plane is True
    assert bok.parse_args(["prod", "install"]).with_model_plane is False
    seen: dict = {}
    patch_bok(monkeypatch, "cmd_prod", lambda action, **kw: (seen.update(action=action, **kw), 0)[1])
    assert bok.main(["prod", "install", "--with-model-plane"]) == 0
    assert seen["with_model_plane"] is True
