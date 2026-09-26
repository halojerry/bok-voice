"""prompt-cache-bytes 档位单测(阶段 A 第③项,2026-09-26)。

_default_prompt_cache_bytes 三级优先级:BOK_LLM_PROMPT_CACHE_BYTES 显式覆盖(最高)
> BOK_DEMO_PRESET=1 演示/单通档(恒 6GB,V10 实弹 12→6GB 单通无损省 6G)
> 内存分档(≥32GB → 12GB / 其余 → 6GB)。两个 env 都是 bok.py serve 进程自读的
sidecar 启动参数,不属 agent worker 运行时 env,不得进 _FORWARD_ENV。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402


def _tier(monkeypatch, mem_gib: float, **env: str) -> str:
    monkeypatch.setattr(bok, "_physical_mem_gib", lambda: mem_gib)
    for key in ("BOK_DEMO_PRESET", "BOK_LLM_PROMPT_CACHE_BYTES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return bok._default_prompt_cache_bytes()


def test_big_mem_default_is_12gb(monkeypatch):
    """48GB 机型默认档 → 12GB(4-6 路并发会话前缀互不逐出)。"""
    assert _tier(monkeypatch, 48.0) == "12GB"


def test_small_mem_default_is_6gb(monkeypatch):
    """16GB 机型默认档 → 6GB(现状不变)。"""
    assert _tier(monkeypatch, 16.0) == "6GB"


def test_demo_preset_forces_6gb_on_big_mem(monkeypatch):
    """48GB + BOK_DEMO_PRESET=1 → 6GB(演示/单通实测无损且省 6G 统一内存)。"""
    assert _tier(monkeypatch, 48.0, BOK_DEMO_PRESET="1") == "6GB"


def test_explicit_override_beats_demo_preset(monkeypatch):
    """显式 BOK_LLM_PROMPT_CACHE_BYTES 最高优先,压过演示档与内存分档。"""
    assert _tier(monkeypatch, 48.0, BOK_DEMO_PRESET="1",
                 BOK_LLM_PROMPT_CACHE_BYTES="8GB") == "8GB"


def test_demo_preset_no_regression_on_small_mem(monkeypatch):
    """16GB + BOK_DEMO_PRESET=1 → 6GB(演示档在小内存机型不劣化)。"""
    assert _tier(monkeypatch, 16.0, BOK_DEMO_PRESET="1") == "6GB"
