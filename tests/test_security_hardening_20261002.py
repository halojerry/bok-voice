"""安全分流跟进的三个纵深守卫单测（2026-10-02，Mimosa 扫描分流档的 actionable 项）：

- provider_health._safe_log_path：文件名纯净性（路径分隔/.. /NUL 拒绝 + 解析后
  必须仍在 log_dir 内，违反=跳过不抛——「读路径永不抛」契约）。
- node_agent._assert_cp_origin：出站钉扎（scheme+host+port 三元组，非 CP 同源
  fail-closed 抛 RuntimeError）。
- BokMarkdownSource：base_url scheme 白名单（http/https 之外的 file:// 等
  构造期即拒）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_node_agent():
    """按路径加载 tools/node_agent.py（tools 不是包；模块级只有 logging 装配，
    import 无网络/进程副作用）。"""
    mod_path = REPO / "tools" / "node_agent.py"
    spec = importlib.util.spec_from_file_location("bok_node_agent_under_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---- provider_health 文件名纯净性 ----


def test_safe_log_path_accepts_plain_filename(tmp_path):
    from bok_voice_obs.provider_health import _safe_log_path

    p = _safe_log_path(tmp_path, "agent.log")
    assert p is not None and p.name == "agent.log"


def test_safe_log_path_rejects_traversal_and_separators(tmp_path):
    from bok_voice_obs.provider_health import _safe_log_path

    for bad in ("../agent.log", "..\\agent.log", "sub/agent.log", "a\\b.log",
                "..", ".", "", "agent\x00.log"):
        assert _safe_log_path(tmp_path, bad) is None, bad


def test_safe_log_path_rejects_symlink_escape(tmp_path):
    """符号链接逃逸：log_dir 内的链接指向目录外 → resolve 后不在父目录内，拒绝。"""
    from bok_voice_obs.provider_health import _safe_log_path

    outside = tmp_path.parent / "bok_outside_secret.log"
    outside.write_text("x", encoding="utf-8")
    link = tmp_path / "leak.log"
    link.symlink_to(outside)
    assert _safe_log_path(tmp_path, "leak.log") is None


def test_scan_provider_health_survives_hostile_filenames(tmp_path):
    """files 参数被污染时端到端不抛、不读目标文件（纯守卫回归）。"""
    from bok_voice_obs.provider_health import scan_provider_health

    out = scan_provider_health(tmp_path, files=("../agent.log", "ok.log", "a/b.log"))
    assert out["available"] is True  # 目录在场；坏文件名只是跳过
    assert out["quota_2056"]["count"] == 0


# ---- node_agent 出站钉扎 ----


def test_assert_cp_origin_same_origin_passes():
    mod = _load_node_agent()
    assert mod._assert_cp_origin("http://cp.local:8000/api/x", "http://cp.local:8000") == \
        "http://cp.local:8000/api/x"
    # 末尾斜杠归一
    mod._assert_cp_origin("https://cp.local/api/x", "https://cp.local/")


def test_assert_cp_origin_foreign_origin_refused():
    import pytest

    mod = _load_node_agent()
    for url, base in [
        ("http://evil.local/api/x", "http://cp.local:8000"),      # 换 host
        ("https://cp.local:8000/api/x", "http://cp.local:8000"),  # 换 scheme
        ("http://cp.local:9999/api/x", "http://cp.local:8000"),   # 换 port
        ("http://cp.local:8000.evil.local/api/x", "http://cp.local:8000"),  # 后缀伪造
    ]:
        with pytest.raises(RuntimeError):
            mod._assert_cp_origin(url, base)


# ---- BokMarkdownSource scheme 白名单 ----


def test_markdown_source_accepts_http_https():
    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    BokMarkdownSource("http://127.0.0.1:8771/v1")
    BokMarkdownSource("https://bok.example.com/v1", token="t")


def test_markdown_source_rejects_other_schemes():
    import pytest

    from bok_voice_knowledge.markdown_source import BokMarkdownSource

    for bad in ("file:///etc", "ftp://x/v1", "gopher://x", "javascript:alert(1)", "/local/path"):
        with pytest.raises(ValueError):
            BokMarkdownSource(bad)
