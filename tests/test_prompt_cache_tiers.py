"""prompt-cache-bytes 档位单测(阶段 A 第③项,2026-09-26;同日二修)。

_default_prompt_cache_bytes 两级:BOK_LLM_PROMPT_CACHE_BYTES 显式覆盖(最高)
> 恒 6GB(2026-09-26 由「≥32GB 机型 12GB」下调:全栈实测 47/48G 占用、压缩器
24G,12GB cache 灌满而每通真命中的只有自家 1555-token 前缀;V10 实弹 12→6GB
单通无损。要回 12GB 用 env 显式覆盖)。该 env 是 bok.py serve 进程自读的
sidecar 启动参数,不属 agent worker 运行时 env,不得进 _FORWARD_ENV。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import bok  # noqa: E402
from _bokpatch import patch_bok  # noqa: E402


def _tier(monkeypatch, mem_gib: float, **env: str) -> str:
    patch_bok(monkeypatch, "_physical_mem_gib", lambda: mem_gib)
    for key in ("BOK_DEMO_PRESET", "BOK_LLM_PROMPT_CACHE_BYTES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return bok.servers._default_prompt_cache_bytes()


def test_big_mem_default_is_4gb(monkeypatch):
    """48GB 机型默认档 → 4GB(2026-09-26 下调:内存压力线实测,跨会话命中
    理论收益让位;要 12GB 走 env 显式覆盖)。"""
    assert _tier(monkeypatch, 48.0) == "4GB"


def test_small_mem_default_is_4gb(monkeypatch):
    """16GB 机型默认档 → 6GB(现状不变)。"""
    assert _tier(monkeypatch, 16.0) == "4GB"


def test_explicit_override_wins(monkeypatch):
    """显式 BOK_LLM_PROMPT_CACHE_BYTES 最高优先(回 12GB 的唯一路径)。"""
    assert _tier(monkeypatch, 48.0, BOK_LLM_PROMPT_CACHE_BYTES="12GB") == "12GB"


def test_demo_preset_no_regression_on_big_mem(monkeypatch):
    """48GB + BOK_DEMO_PRESET=1 → 6GB(演示档在大内存机型不劣化=与新默认一致)。"""
    assert _tier(monkeypatch, 48.0, BOK_DEMO_PRESET="1") == "4GB"
