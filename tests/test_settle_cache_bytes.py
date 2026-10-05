"""BOK_SETTLE_CACHE_BYTES 做实（2026-10-02 编排审计第二波 · PR-E item 4）。

背景：该键此前只活在 `_start_settle_llm` 注释里（「16GB 机型可
BOK_SETTLE_CACHE_BYTES 显式下调」），argv 恒硬编码 4GB——注释承诺的旋钮不
存在。本文件钉 `_settle_cache_bytes()` 的缺省/覆盖语义 + argv 落点 + tier
打点（镜像 :1235 `_default_prompt_cache_bytes` 的 explicit 档先例）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bok  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402


def test_settle_cache_bytes_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("BOK_SETTLE_CACHE_BYTES", raising=False)
    assert bok.servers._settle_cache_bytes() == "4GB"
    monkeypatch.setenv("BOK_SETTLE_CACHE_BYTES", "2GB")
    assert bok.servers._settle_cache_bytes() == "2GB"
    monkeypatch.setenv("BOK_SETTLE_CACHE_BYTES", "   ")
    assert bok.servers._settle_cache_bytes() == "4GB", "空白=未设（缺省零漂移）"


def _run_settle_start(monkeypatch, tmp_path):
    started: list[list[str]] = []
    model = tmp_path / "settle-model"
    model.mkdir()
    monkeypatch.setenv("BOK_DEV_9B", "1")
    # 密闭纪律（2026-10-05 demo-cloud 实弹发现）：_start_settle_llm 的云姿势闸
    # 读真设置库——不打桩则测试结果随机跟随本机 DB 的路由车道面。此处钉全本地。
    patch_bok(monkeypatch, "_cloud_posture", lambda: {
        "asr_cloud": False, "asr_why": "",
        "llm_cloud": False, "llm_why": "",
        "settle_cloud": False, "settle_why": "",
        "mt_local": True, "mt_why": "",
    })
    patch_bok(monkeypatch, "healthy", lambda port: False)
    patch_bok(monkeypatch, "sidecar_python", lambda name: tmp_path / "py")
    patch_bok(monkeypatch, "_apply_mlx_template_fix", lambda py: None)
    patch_bok(monkeypatch, "_settle_llm_model", lambda cur: str(model))
    patch_bok(
        monkeypatch, "_start_proc",
        lambda args, pidfile, logfile, env=None, cwd=None: started.append(args))
    assert bok.servers._start_settle_llm({}, tmp_path, tmp_path) is True
    return started


def test_start_settle_llm_default_argv_byte_identical(monkeypatch, tmp_path, capsys) -> None:
    """缺省档：argv 逐字节同旧（128 槽/4GB/关思考），tier=default 打点。"""
    monkeypatch.delenv("BOK_SETTLE_CACHE_BYTES", raising=False)
    started = _run_settle_start(monkeypatch, tmp_path)
    argv = started[0]
    assert argv[argv.index("--prompt-cache-size") + 1] == "128"
    assert argv[argv.index("--prompt-cache-bytes") + 1] == "4GB"
    assert argv[argv.index("--chat-template-args") + 1] == '{"enable_thinking":false}'
    assert "settle prompt-cache 4GB (tier=default)" in capsys.readouterr().out


def test_start_settle_llm_env_override_lands(monkeypatch, tmp_path, capsys) -> None:
    """显式档：BOK_SETTLE_CACHE_BYTES 真进 argv，tier=explicit 打点。"""
    monkeypatch.setenv("BOK_SETTLE_CACHE_BYTES", "2GB")
    started = _run_settle_start(monkeypatch, tmp_path)
    argv = started[0]
    assert argv[argv.index("--prompt-cache-bytes") + 1] == "2GB"
    assert argv[argv.index("--prompt-cache-size") + 1] == "128"
    assert "settle prompt-cache 2GB (tier=explicit)" in capsys.readouterr().out
