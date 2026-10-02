"""LLM 包装层 metrics 转发单测（2026-09-29，RCA §0.3 回归钉）。

事故存档:ExprAwareLLM 的 ``_bind_metrics_forward`` 曾被 _prewarm_impl 插入
接缝吞进方法体(且 ``inner`` 在该方法作用域不存在 → NameError 被预热兜底吞)
——LLM_TTFT_MS / PERCEIVED_MS 全灭一整天,P3 的 cached 前缀测量面跟着失明。
本文件钉死:**绑定必须发生在构造期**,prewarm 与 metrics 互不相干。

链:MlxLlmLLM(emit 在创建流的对象上) → ExprAwareLLM → ContextAwareLLM,
最外层(session 挂监听者)必须收到 metrics_collected。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    ContextAwareLLM,
    ExprAwareLLM,
)


class _FakeInner:
    """鸭型内芯:提供 .on/.emit(转发绑定与手动触发)。"""

    def __init__(self):
        self._handlers: dict[str, list] = {}

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args, **kwargs):
        for h in list(self._handlers.get(event, [])):
            h(*args, **kwargs)


class _Evt:
    pass


def test_expr_aware_binds_metrics_at_construction():
    """ExprAwareLLM 构造即绑定:内芯 emit → 包装层收到(不依赖 prewarm)。"""
    inner = _FakeInner()
    wrap = ExprAwareLLM(inner, emotion_state=None)
    hits: list[int] = []
    wrap.on("metrics_collected", lambda *a, **k: hits.append(1))
    inner.emit("metrics_collected", _Evt())
    assert hits, "ExprAwareLLM 未在构造期绑定 metrics 转发(接缝回归?)"


def test_context_aware_binds_metrics_at_construction():
    """ContextAwareLLM 同款钉(当前健康,防将来同病)。"""
    inner = _FakeInner()
    wrap = ContextAwareLLM(inner, context_state=None)
    hits: list[int] = []
    wrap.on("metrics_collected", lambda *a, **k: hits.append(1))
    inner.emit("metrics_collected", _Evt())
    assert hits, "ContextAwareLLM 未在构造期绑定 metrics 转发"


def test_full_chain_two_level_forward():
    """全链:内芯 emit → Expr → Context 最外层到达(session 视角)。"""
    inner = _FakeInner()
    outer = ContextAwareLLM(ExprAwareLLM(inner, emotion_state=None), context_state=None)
    hits: list[int] = []
    outer.on("metrics_collected", lambda *a, **k: hits.append(1))
    inner.emit("metrics_collected", _Evt())
    assert hits, "两层包装链 metrics 中断"


def test_source_bind_not_inside_prewarm():
    """源级 pin:ExprAwareLLM 的绑定行不在 _prewarm_impl 方法体内。"""
    src = (
        Path(__file__).resolve().parents[1]
        / "apps"
        / "agent"
        / "agent_runtime"
        / "providers"
        / "livekit_plugins.py"
    ).read_text(encoding="utf-8")
    block = src.split("class ExprAwareLLM")[1].split("def chat(")[0]
    prewarm_part = block.split("async def _prewarm_impl")[1] if "_prewarm_impl" in block else ""
    assert "_bind_metrics_forward" not in prewarm_part, (
        "绑定被吞进 _prewarm_impl(2026-09-29 事故形状,禁止复发)"
    )
    init_part = block.split("def __init__")[1].split("async def")[0]
    assert "_bind_metrics_forward" in init_part, "绑定必须留在 __init__"
