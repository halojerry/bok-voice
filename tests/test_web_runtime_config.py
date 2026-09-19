"""云托管管理台 runtime-config 注入单测（2026-09-19 Docker 模拟拓扑实测缺口收编）。

缺口：静态台 apiBase() 第一优先 window.__BOK_CONFIG__.cpUrl（节点托管由
node_agent 写同一文件），未注入回落构建期烤死的 127.0.0.1:8000——云端口形态
（如 prod 18010）下登录必 401 弹回。CP startup 在 BOK_CP_PUBLIC_URL 设置时
幂等写 /app/web-out/runtime-config.js；本文件钉住写入格式/URL 校验/失败不炸。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("DATABASE_URL", "")  # force in-memory repo for tests
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")

from control_plane import main as cp_main  # noqa: E402


def test_write_web_runtime_config_happy_path(tmp_path: Path):
    """合法 URL → 写入 apiBase() 认的格式，幂等可重写。"""
    out = cp_main._write_web_runtime_config("http://127.0.0.1:18011/", tmp_path)
    assert out is not None and out.is_file()
    body = out.read_text(encoding="utf-8")
    # 尾斜杠剥掉、json.dumps 转义、语句分号收尾——apiBase() 读 window 对象
    assert body == 'window.__BOK_CONFIG__ = {"cpUrl": "http://127.0.0.1:18011"};\n'
    # 幂等重写（换 URL 覆盖旧值）
    cp_main._write_web_runtime_config("https://cp.example.com", tmp_path)
    assert "cp.example.com" in out.read_text(encoding="utf-8")


def test_write_web_runtime_config_rejects_bad_urls(tmp_path: Path):
    """非 http(s)/含引号尖括号 → 拒写（进 JS 字符串与 HTML 上下文的注入面）。"""
    for bad in ("ftp://x", "javascript:alert(1)", 'http://x"a', "http://a<b", "http://a\\b", "  "):
        assert cp_main._write_web_runtime_config(bad, tmp_path) is None
    assert not (tmp_path / "runtime-config.js").exists()


def test_write_web_runtime_config_write_failure_never_raises(tmp_path: Path):
    """目标不可写（只读挂载等）→ 返回 None 绝不阻启动。"""
    ro = tmp_path / "ro"
    ro.mkdir()
    target = ro / "runtime-config.js"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o444)
    try:
        assert cp_main._write_web_runtime_config("http://ok:1", ro) is None
    finally:
        target.chmod(0o644)
