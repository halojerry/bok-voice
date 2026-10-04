"""systemd appliance 单元生成（2026-09-24 规格）单测——全平台可跑、零 systemd 执行。

钉住的契约（见 tools/systemd_units.py docstring）：
- 单元清单 = bok-cp / bok-agent-worker / bok-interp-fwd / bok-interp-rev /
  bok-node-agent 五个具体单元 + bok-sidecar@.service 模板（spec 定名）；
- 形状：After/Wants=network-online.target、Type=simple、WorkingDirectory、
  EnvironmentFile=/etc/bok/bok.env（只引用、永不代写）、Restart=always
  （**node-agent 例外 on-failure**：吊销熔断 exit(0) 不得被拉回）、
  RestartSec=3、WantedBy=multi-user.target；
- ExecStart 与端口记号逐单元钉死（cp --port 8000 进 argv；worker :8081 与
  interp :8082/:8083 是代码缺省，以 Description 记号 + INTERP_DIRECTION 内联钉）；
- 纯函数本位：模块零落盘零 subprocess（副作用唯一出口 = bok.py prod install）；
- bok.py Linux 分支只写暂存目录（--staging-dir / BOK_SYSTEMD_STAGING_DIR 可
  覆盖，缺省 <repo>/release-artifacts/systemd）、绝不碰 /etc，并打印
  cp + daemon-reload + enable --now 逐字装载指引。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
import systemd_units as sd  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402

_PY = "/opt/bok/.venv312/bin/python"
_ROOT = "/opt/bok"

_UNIT_NAMES = [
    "bok-cp.service",
    "bok-agent-worker.service",
    "bok-interp-fwd.service",
    "bok-interp-rev.service",
    "bok-node-agent.service",
    "bok-sidecar@.service",
]

_CONCRETE_NAMES = [n for n in _UNIT_NAMES if n != sd.SIDECAR_UNIT]


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return dict(sd.render_all_units(_ROOT, _PY))


# ---------------- 单元清单与公共形状 ----------------


def test_unit_set_matches_spec(rendered):
    """五具体单元 + 一模板，名字逐个钉死（spec 2026-09-24 定名）。"""
    assert list(rendered) == _UNIT_NAMES


@pytest.mark.parametrize("name", _UNIT_NAMES)
def test_common_unit_shape(rendered, name):
    text = rendered[name]
    # Restart 档逐单元分档（2026-09-24 修正）：node-agent 的吊销熔断=exit(0)，
    # on-failure 令吊销后保持 dead（2026-09-20 契约「勿改 always」的复归）；
    # 其余恒 always（clean stop 由 systemctl stop 天然不重启）。
    expected_restart = (
        "Restart=on-failure" if name == "bok-node-agent.service" else "Restart=always"
    )
    for marker in (
        "[Unit]", "[Service]", "[Install]",
        "After=network-online.target",
        "Wants=network-online.target",
        "Type=simple",
        "WorkingDirectory=/opt/bok",
        f"EnvironmentFile={sd.ENV_FILE}",
        expected_restart,
        "RestartSec=3",
        "WantedBy=multi-user.target",
    ):
        assert marker in text, f"{name} 缺 {marker!r}"
    # 非熔断单元不得回落 on-failure；任何单元不得回流旧档 RestartSec=10。
    if name != "bok-node-agent.service":
        assert "Restart=on-failure" not in text
    assert "RestartSec=10" not in text


@pytest.mark.parametrize("name", _UNIT_NAMES)
def test_env_file_referenced_exactly_once_never_written(rendered, name):
    # 只引用（恰好一行 EnvironmentFile=），不创建不落盘。
    assert rendered[name].count(f"EnvironmentFile={sd.ENV_FILE}") == 1


# ---------------- ExecStart / 端口记号逐单元 ----------------


def _exec_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.startswith("ExecStart=")]
    assert len(lines) == 1, "具体单元必须恰好一条 ExecStart"
    return lines[0]


def test_cp_execstart_uvicorn_port_8000(rendered):
    line = _exec_line(rendered["bok-cp.service"])
    assert line == (
        f"ExecStart={_PY} -m uvicorn control_plane.main:app"
        " --host 127.0.0.1 --port 8000")


def test_cp_bind_host_param_flows(rendered):
    units = dict(sd.render_all_units(_ROOT, _PY, cp_bind_host="0.0.0.0"))
    assert "--host 0.0.0.0" in _exec_line(units["bok-cp.service"])


def test_agent_worker_execstart_and_port_marker(rendered):
    text = rendered["bok-agent-worker.service"]
    assert _exec_line(text) == f"ExecStart={_PY} -m agent_runtime.main"
    assert ":8081" in text  # BOK_WORKER_PORT 代码缺省的端口记号（Description）


def test_interp_units_direction_inline_and_port_markers(rendered):
    fwd = rendered["bok-interp-fwd.service"]
    rev = rendered["bok-interp-rev.service"]
    # 方向键两单元相异、必须内联（共用同一份 EnvironmentFile 装不下互斥值）。
    assert "Environment=INTERP_DIRECTION=fwd" in fwd
    assert "Environment=INTERP_DIRECTION=rev" in rev
    assert "BOK_SERVICE=interp-fwd" in fwd and "BOK_SERVICE=interp-rev" in rev
    # 方向键不得跨单元串味。
    assert "INTERP_DIRECTION=rev" not in fwd and "INTERP_DIRECTION=fwd" not in rev
    assert "agent_runtime.interpret" in _exec_line(fwd)
    assert "agent_runtime.interpret" in _exec_line(rev)
    assert ":8082" in fwd and ":8083" in rev


def test_node_agent_execstart_and_ui_port_marker(rendered):
    text = rendered["bok-node-agent.service"]
    line = _exec_line(text)
    assert "tools/node_agent.py" in line
    assert "--cp-url" in line
    assert ":3000" in text  # UI 托管端口记号
    assert "Environment=PYTHONUNBUFFERED=1" in text


def test_node_agent_args_passthrough():
    name, text = sd.render_node_agent_unit(
        _ROOT, _PY, ["--cp-url", "http://10.0.0.5:8000", "--license-key", "bokn_x"])
    assert name == "bok-node-agent.service"
    line = _exec_line(text)
    assert "--cp-url http://10.0.0.5:8000" in line
    assert "--license-key bokn_x" in line


# ---------------- sidecar 模板 ----------------


def test_sidecar_template_documents_instances(rendered):
    text = rendered[sd.SIDECAR_UNIT]
    assert "%i" in text  # 模板单元
    # 逐实例真命令以注释给全（端口 + app-dir + venv python 绝对路径）。
    for port in ("8787", "8788", "8789"):
        assert port in text, f"sidecar 模板缺 :{port} 实例文档"
    for app_dir in ("services/qwen3-asr-sidecar",
                    "services/qwen3-tts-sidecar",
                    "services/bge-embed-sidecar"):
        assert app_dir in text
        venv = f"/opt/bok/{app_dir}/.venv/bin/python -m uvicorn app:app --app-dir {app_dir}"
        assert venv in text
    # 占位 ExecStart：占位值在场 + 「必须替换」警示在场（装载前逐实例替换）。
    assert "ExecStart=/bin/false" in text
    assert "占位" in text
    # 实例化命名约定随注释可发现。
    assert "bok-sidecar@asr.service" in text
    assert "bok-sidecar@tts.service" in text
    assert "bok-sidecar@embed.service" in text


# ---------------- 纯函数本位：模块零副作用面 ----------------


def test_module_has_no_subprocess_or_write_surface():
    src = (ROOT / "tools" / "systemd_units.py").read_text(encoding="utf-8")
    assert "import subprocess" not in src  # 模块 docstring 提及「零 subprocess」不算面
    assert "os.system" not in src
    assert "write_text" not in src and "mkdir" not in src and ".open(" not in src
    # systemctl 只出现在模板/示例文本，不做调用。
    assert "systemctl" in src


def test_unit_name_whitelist():
    assert sd.unit_name("cp") == "bok-cp.service"
    assert sd.unit_name("node-agent") == "bok-node-agent.service"
    for bad in ("../evil", "a b", "UPPER", "a;rm", "", "a/b", "sidecar@"):
        with pytest.raises(ValueError):
            sd.unit_name(bad)


def test_quote_arg_plain_and_json():
    assert sd.quote_arg(_PY) == _PY
    assert sd.quote_arg("agent_runtime.main") == "agent_runtime.main"
    assert sd.quote_arg("/opt/bok voice/out") == '"/opt/bok voice/out"'
    quoted = sd.quote_arg('{"enable_thinking":false}')
    assert quoted.startswith('"') and quoted.endswith('"')
    assert '\\"enable_thinking\\"' in quoted


def test_env_file_sample_lists_pythonpath_and_never_write_hint():
    sample = sd.env_file_sample(_ROOT)
    assert sd.ENV_FILE in sample
    assert f"PYTHONPATH={_ROOT}/packages/core:" in sample
    assert f"{_ROOT}/apps/agent" in sample  # 六段缺一不可（WorkingDirectory 只有根）
    assert "PYTHONUNBUFFERED=1" in sample


# ---------------- bok.py 接线：暂存目录解析 ----------------


def _linux(monkeypatch, tmp_path: Path) -> None:
    """模拟 Linux 档（桩法对齐 test_prod_windows：is_mac/is_linux 双桩），
    app-data 钉到 tmp、解释器钉到固定路径保证 ExecStart 可断言。"""
    patch_bok(monkeypatch, "is_mac", lambda: False)
    patch_bok(monkeypatch, "is_linux", lambda: True)
    patch_bok(monkeypatch, "app_data_dir", lambda: tmp_path)
    patch_bok(monkeypatch, "repo_python", lambda: Path(_PY))


def test_staging_dir_default(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    monkeypatch.delenv("BOK_SYSTEMD_STAGING_DIR", raising=False)
    assert bok._systemd_staging_dir("") == bok.ROOT / "release-artifacts" / "systemd"


def test_staging_dir_env_override_and_flag_precedence(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    monkeypatch.setenv("BOK_SYSTEMD_STAGING_DIR", str(tmp_path / "envstage"))
    assert bok._systemd_staging_dir("") == tmp_path / "envstage"
    assert bok._systemd_staging_dir(str(tmp_path / "flagstage")) == tmp_path / "flagstage"


def test_staging_dir_refuses_etc(monkeypatch, tmp_path):
    """NEVER write /etc 的硬闸：/etc 下任何位置（含 /etc/systemd/system）直接拒。
    /etc 在 macOS 是 /private/etc 符号链接，守卫必须 resolve 后仍成立。"""
    _linux(monkeypatch, tmp_path)
    monkeypatch.setenv("BOK_SYSTEMD_STAGING_DIR", "")
    for bad in ("/etc/systemd/system", "/etc/bok", "/etc"):
        with pytest.raises(SystemExit):
            bok._systemd_staging_dir(bad)


# ---------------- bok.py 接线：install / uninstall Linux 分支 ----------------


def _etc_units() -> set[str]:
    d = Path("/etc/systemd/system")
    if not d.is_dir():
        return set()
    return {p.name for p in d.glob("bok-*.service")}


def test_prod_install_linux_writes_staging_only_and_prints_load_steps(
        monkeypatch, tmp_path, capsys):
    _linux(monkeypatch, tmp_path)
    staging = tmp_path / "staging"
    before = _etc_units()
    rc = bok.cmd_prod_install(staging_dir=str(staging))
    out = capsys.readouterr().out
    assert rc == 0
    assert sorted(p.name for p in staging.glob("*.service")) == sorted(_UNIT_NAMES)
    # 逐字装载指引：cp → daemon-reload → enable --now（模板单元不进默认 enable 名单）。
    assert f"cp {staging}/*.service /etc/systemd/system/" in out
    assert "systemctl daemon-reload" in out
    enable_line = next(ln for ln in out.splitlines() if "enable --now" in ln)
    for unit in _CONCRETE_NAMES:
        assert unit in enable_line
    assert sd.SIDECAR_UNIT not in enable_line  # 模板要替换 ExecStart 后才启用
    assert sd.ENV_FILE in out  # env 示例随输出打印（不代写）
    assert _etc_units() == before  # /etc 零触碰


def test_prod_install_linux_node_agent_single_unit(monkeypatch, tmp_path, capsys):
    _linux(monkeypatch, tmp_path)
    staging = tmp_path / "staging"
    rc = bok.cmd_prod_install(
        staging_dir=str(staging), node_agent=True,
        node_args=["--cp-url", "http://10.0.0.5:8000", "--license-key", "bokn_x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert [p.name for p in staging.glob("*.service")] == ["bok-node-agent.service"]
    node_text = (staging / "bok-node-agent.service").read_text(encoding="utf-8")
    assert "--cp-url http://10.0.0.5:8000" in node_text
    assert "--license-key bokn_x" in node_text


def test_prod_install_linux_node_agent_requires_cp_url(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    rc = bok.cmd_prod_install(staging_dir=str(tmp_path), node_agent=True,
                              node_args=["--license-key", "x"])
    assert rc == 2


def test_prod_install_linux_default_staging_via_env(monkeypatch, tmp_path, capsys):
    _linux(monkeypatch, tmp_path)
    monkeypatch.setenv("BOK_SYSTEMD_STAGING_DIR", str(tmp_path / "env-out"))
    assert bok.cmd_prod_install() == 0
    assert (tmp_path / "env-out" / "bok-cp.service").exists()


def test_prod_uninstall_linux_clears_staging_and_legacy(monkeypatch, tmp_path, capsys):
    _linux(monkeypatch, tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "bok-cp.service").write_text("x", encoding="utf-8")
    (staging / "bok-node-agent.service").write_text("x", encoding="utf-8")
    (staging / "keep.txt").write_text("not-a-unit", encoding="utf-8")
    legacy = tmp_path / "units"  # 2026-09-20 旧档 app-data/units 兼容清扫
    legacy.mkdir()
    (legacy / "bok-agent.service").write_text("old", encoding="utf-8")
    (legacy / "com.bokvoice.bok-agent.plist").write_text("keep-me", encoding="utf-8")
    rc = bok.cmd_prod_uninstall(staging_dir=str(staging))
    out = capsys.readouterr().out
    assert rc == 0
    assert not list(staging.glob("bok-*.service"))
    assert (staging / "keep.txt").exists()  # 非单元文件不误删
    assert not list(legacy.glob("bok-*.service"))
    assert (legacy / "com.bokvoice.bok-agent.plist").exists()  # plist 归 mac 面，不动
    assert "daemon-reload" in out  # 系统面清理指引（root 逐字执行）


def test_prod_uninstall_linux_idempotent_on_empty_staging(monkeypatch, tmp_path):
    _linux(monkeypatch, tmp_path)
    assert bok.cmd_prod_uninstall(staging_dir=str(tmp_path / "missing")) == 0


# ---------------- CLI 旗标 ----------------


def test_parse_args_staging_dir_flag():
    args = bok.parse_args(["prod", "install", "--staging-dir", "/tmp/sd"])
    assert args.action == "install" and args.staging_dir == "/tmp/sd"
    assert bok.parse_args(["prod", "uninstall"]).staging_dir == ""
