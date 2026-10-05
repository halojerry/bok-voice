"""bok tts-pregen / tts-mine 子进程 env 组装回归（G2 W③ 前置修复）。

d5aeab0 的机械改写把 `_bake_ssl_cert_file(env, …)` 写成了
`env._bake_ssl_cert_file(env, …)`——本地 dict `env` 遮蔽 bokctl.env 模块，
两条命令真跑即 AttributeError（`--help` 不执行函数体、套件未覆盖 → 潜伏）。
本测试钉：两条命令经 `bok.main([...])` 全链分发后
① 不炸（AttributeError 消失=修复生效）；
② argv[1] 指向 scripts/runtime/ 下正确脚本；
③ 子进程 env kwarg 带 PYTHONPATH。
不断言 SSL_CERT_FILE（_bake 条件注入，与故障面无关）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402


def _fake_run(seen: dict):
    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0)

    return fake_run


def test_tts_pregen_invokes_runtime_script_with_repo_env(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(bok.subprocess, "run", _fake_run(seen))
    patch_bok(monkeypatch, "repo_python", lambda: Path("/fake/venv/bin/python"))
    rc = bok.main(["tts-pregen"])
    assert rc == 0
    assert seen["argv"][1].endswith("scripts/runtime/pregen_tts.py")
    assert "PYTHONPATH" in seen["env"]


def test_tts_mine_invokes_runtime_script_with_repo_env(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(bok.subprocess, "run", _fake_run(seen))
    patch_bok(monkeypatch, "repo_python", lambda: Path("/fake/venv/bin/python"))
    rc = bok.main(["tts-mine"])
    assert rc == 0
    assert seen["argv"][1].endswith("scripts/runtime/mine_qa.py")
    assert "PYTHONPATH" in seen["env"]
