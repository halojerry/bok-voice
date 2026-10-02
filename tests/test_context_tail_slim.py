"""P1.2a 记忆滚动压缩（2026-09-21，§48 P1「恒定轮延迟」）。

背景（§46.1 受控实验）：尾部=每轮新 prefill 的全部成本，记忆行是唯一单调
增长项（6 行 ≈ +679 字 ≈ 每轮多 ~1.5s）。本文件钉住 `add_summary` 契约：

- 新档（默认，上限 400）：超限时**最旧两行各取前半并成一行**——行数有界、
  信息密度翻倍，绝不整行静默丢弃；
- kill-switch `BOK_CONTEXT_MEM_LEGACY=1`：回旧「drop-oldest」档（上限 1200），
  行为逐字节同旧；
- 显式 `max_summary_chars` 传参（测试/嵌入方）优先于 env 档。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402

_LONG = "x" * 150  # 单行满额素材（add_summary 单行截 200，150 全保留）


def test_default_cap_is_400(monkeypatch):
    monkeypatch.delenv("BOK_CONTEXT_MEM_LEGACY", raising=False)
    assert ContextState()._max_summary_chars == 400


def test_legacy_cap_and_drop_oldest(monkeypatch):
    monkeypatch.setenv("BOK_CONTEXT_MEM_LEGACY", "1")
    ctx = ContextState()
    assert ctx._max_summary_chars == 1200
    for i in range(12):
        ctx.add_summary("user", f"line{i} " + _LONG)
    # 旧行为：整行丢最旧——首行必然是较晚的行
    assert not ctx._summary_lines[0].startswith("user: line0")
    assert all(len(l) <= 210 for l in ctx._summary_lines)


def test_compress_merges_two_oldest_not_drop(monkeypatch):
    monkeypatch.delenv("BOK_CONTEXT_MEM_LEGACY", raising=False)
    ctx = ContextState()
    for i in range(8):
        ctx.add_summary("user", f"line{i} " + _LONG)
    joined = "\n".join(ctx._summary_lines)
    assert len(joined) <= 400
    # 并行不是丢弃：最旧内容在首条合并行里留痕（多轮再并允许逐级磨损——
    # 滚动摘要语义：越旧越糊，最新最真），且「；」合并分隔符在场。
    assert "line0" in ctx._summary_lines[0]
    assert any("；" in l for l in ctx._summary_lines[:-1])
    # 最新一行永不被动（永远完整在场）
    assert ctx._summary_lines[-1].startswith("user: line7")


def test_bounded_across_long_call(monkeypatch):
    """20 行长通话：尾部记忆段有界（≈cap），不再单调涨。"""
    monkeypatch.delenv("BOK_CONTEXT_MEM_LEGACY", raising=False)
    ctx = ContextState()
    for i in range(20):
        ctx.add_summary("user" if i % 2 else "assistant", f"輪{i} " + _LONG)
        assert len("\n".join(ctx._summary_lines)) <= 400 + 210  # 单行触发窗余量


def test_explicit_param_overrides_env(monkeypatch):
    monkeypatch.setenv("BOK_CONTEXT_MEM_LEGACY", "1")
    ctx = ContextState(max_summary_chars=100)
    assert ctx._max_summary_chars == 100


def test_few_short_lines_untouched(monkeypatch):
    """正常短通话（两三行短记忆）零压缩零截断——行为与旧档无差。"""
    monkeypatch.delenv("BOK_CONTEXT_MEM_LEGACY", raising=False)
    ctx = ContextState()
    ctx.add_summary("user", "客戶報咗單號")
    ctx.add_summary("assistant", "已覆三日內跟進")
    assert ctx._summary_lines == ["user: 客戶報咗單號", "assistant: 已覆三日內跟進"]
