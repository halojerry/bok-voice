"""systemd 常驻单元生成（Ubuntu 节点形态，2026-09-20）：纯函数 + 落盘语义钉。

契约要点（见 tools/systemd_units.py docstring）：
- Restart=on-failure 精确复刻 update(75) 拉回 / kill-switch exit(0) 保持死亡——
  「勿改 always」是 kill-switch 语义的承重件；
- ExecStart 参数引用（本仓 `--chat-template-kwargs '{"enable_thinking":false}'`
  含双引号与花括号）；
- 单元名白名单（[a-z0-9-]）拒绝任何非预期字符，名字不进路径/命令拼接；
- 本模块零 subprocess：装载动作由安装脚本/操作者执行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import systemd_units as sd  # noqa: E402


def test_unit_name_slug_and_whitelist():
    assert sd.unit_name("node-agent") == "bok-node-agent.service"
    assert sd.unit_name("control-plane") == "bok-control-plane.service"
    # 白名单外一律拒绝（纵深防御：名字永不来自用户输入）
    for bad in ("../evil", "a b", "UPPER", "a;rm -rf /", "", "a/b"):
        with pytest.raises(ValueError):
            sd.unit_name(bad)


def test_quote_arg_plain_and_json():
    # 纯字符直出（无需引号，保持可读）
    assert sd.quote_arg("/usr/bin/python3.12") == "/usr/bin/python3.12"
    assert sd.quote_arg("agent_runtime.main") == "agent_runtime.main"
    # 空格串 → 双引号
    assert sd.quote_arg("/opt/bok voice/out") == '"/opt/bok voice/out"'
    # JSON 片段（本仓 chat-template-kwargs）→ 引号包裹 + 内部引号转义
    quoted = sd.quote_arg('{"enable_thinking":false}')
    assert quoted.startswith('"') and quoted.endswith('"')
    assert '\\"enable_thinking\\"' in quoted


def test_build_unit_restart_semantics_and_env(tmp_path: Path):
    unit = sd.build_unit(
        "node-agent",
        ["/usr/bin/python3.12", "/opt/bok/tools/node_agent.py", "--cp-url", "http://127.0.0.1:8000"],
        {"PYTHONUNBUFFERED": "1", "BOK_LIVEKIT_BIND": "192.168.1.10"},
        root="/opt/bok",
        log_dir="/data/logs",
        comment="薄节点守护",
    )
    # kill-switch 承重件：on-failure（更新 75 拉回、熔断 0 不拉回）
    assert "Restart=on-failure" in unit
    assert "Restart=always" not in unit
    assert "WantedBy=multi-user.target" in unit
    assert "WorkingDirectory=/opt/bok" in unit
    assert "Environment=BOK_LIVEKIT_BIND=192.168.1.10" in unit
    assert "Environment=PYTHONUNBUFFERED=1" in unit
    assert "ExecStart=/usr/bin/python3.12 /opt/bok/tools/node_agent.py --cp-url http://127.0.0.1:8000" in unit
    assert "StandardOutput=append:/data/logs/node-agent.log" in unit
    assert "StandardError=append:/data/logs/node-agent.err.log" in unit


def test_build_unit_json_kwargs_survives_quoting():
    unit = sd.build_unit(
        "agent",
        ["/py", "-m", "agent_runtime.main", "--chat-template-kwargs", '{"enable_thinking":false}'],
        {},
        root="/opt/bok",
        log_dir="/tmp",
    )
    # systemd 双引号内转义：整段作为单一参数传给 llama-server/mlx
    assert '--chat-template-kwargs "{\\"enable_thinking\\":false}"' in unit


def test_write_and_remove_units(tmp_path: Path):
    unit_dir = tmp_path / "units"
    written = sd.write_units(
        [("node-agent", ["/py", "/opt/bok/tools/node_agent.py"], {"A": "1"}, "薄节点守护")],
        unit_dir,
        root="/opt/bok",
        log_dir=str(tmp_path / "logs"),
    )
    assert written == ["bok-node-agent.service"]
    path = unit_dir / "bok-node-agent.service"
    assert path.exists() and "Restart=on-failure" in path.read_text(encoding="utf-8")
    removed = sd.remove_units(["node-agent"], unit_dir)
    assert removed == ["bok-node-agent.service"]
    assert not path.exists()
    # 幂等：重复删除不报错、返回空
    assert sd.remove_units(["node-agent"], unit_dir) == []


def test_module_has_no_subprocess_surface():
    """零特权动作：模块不得调用任何外部命令（装载归安装脚本/操作者）。"""
    src = (ROOT / "tools" / "systemd_units.py").read_text(encoding="utf-8")
    assert "import subprocess" not in src
    assert "subprocess.run" not in src
    assert "os.system" not in src
    assert "popen" not in src.lower()
    assert "systemctl" in src  # 只出现在提示文本，不做调用
