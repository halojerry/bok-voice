"""C5(2026-09-13):read-only chat context 官方姿势探针测试。

9/12 单日 189 次 `trying to modify a read-only chat context` ERROR 的根因:
livekit 1.8 `Agent.chat_ctx` 返回 _ReadOnlyChatContext(只读视图),旧三处
`chat_ctx.items.append` 全部 RuntimeError 被 except-pass 吞——「手动补 user 轮」
(say-step/QA 快路)从未生效。官方解(错误信息原文):`.copy()` + 
`agent.update_chat_ctx()`。agent.py 已封装 `_try_append_user_message`
(entrypoint 闭包类内,无法直接单测——本文件用真 livekit 原语钉死姿势语义,
封装调用序列与之一致)。
"""

from __future__ import annotations

import pytest


def _make_ctx():
    from livekit.agents.llm import ChatContext

    return ChatContext(items=[])


def test_readonly_append_raises():
    """旧姿势必然失败(189 次 ERROR 的复现)——钉死根因,防回归回旧写法。"""
    from livekit.agents.llm.chat_context import _ReadOnlyChatContext

    ctx = _make_ctx()
    ro = _ReadOnlyChatContext(ctx.items)
    with pytest.raises(RuntimeError, match="read-only"):
        ro.items.append("x")  # type: ignore[arg-type]


def test_copy_then_append_official_path():
    """官方姿势:copy() 出可变副本(默认参数保留 items 原对象),append 畅通。"""
    ctx = _make_ctx()
    from livekit.agents.llm import ChatMessage

    msg = ChatMessage(content=["你好"], role="user")
    ctx.items.append(msg)
    c2 = ctx.copy()
    c2.items.append(ChatMessage(content=["第二句"], role="user"))
    # 原上下文不受污染(隔离性),副本含两条
    assert len(ctx.items) == 1
    assert len(c2.items) == 2
    # copy 保留原对象(KV 前缀字节稳定的锚点)
    assert c2.items[0] is ctx.items[0]


def test_agent_chat_ctx_property_is_readonly():
    """真 Agent 实例的 .chat_ctx 就是只读视图(与生产路径同一入口)。"""
    from livekit.agents import Agent

    agent = Agent(instructions="test")
    with pytest.raises(RuntimeError, match="read-only"):
        agent.chat_ctx.items.append("x")  # type: ignore[arg-type]
    # 官方更新入口存在且为 async(Agent.update_chat_ctx,voice/agent.py:236)
    import inspect

    assert inspect.iscoroutinefunction(Agent.update_chat_ctx)
