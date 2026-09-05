"""会话首轮真实前缀预热（LLM_PREFIX_PREWARM，默认 1）单测。

p6 台账：会话首轮 TTFT 2.1-3.2s、cached=0（mlx prompt cache 空，整段 system
全量 prefill ~1.4s）。FIX：session 建好后 fire-and-forget 一个「真实 prompt
形状」的 1-token 请求把 merged system 前缀烧进 mlx prompt cache——turn-1 真
请求共享 system 前缀 → cached≈system 长度。本档钉死：

- 请求体形状与 ContextAwareLLM.chat 合并规则同构：system = 稳定指令前缀
  （【用户语言】/【回复节奏】/【应答准则】/话术总览）+ 人设 base；易变尾部
  （当前步等）拼喺 fake user 轮尾部；
- max_tokens=1（只暖 cache，唔生成内容）；
- 开关 LLM_PREFIX_PREWARM（默认开）。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.agent import (  # noqa: E402
    _build_prefix_prewarm_messages,
    _prefix_prewarm_enabled,
)
from agent_runtime.providers.livekit_plugins import ContextState, MlxLlmLLM  # noqa: E402


def test_prefix_prewarm_messages_match_real_prompt_shape():
    """请求体 = merged system（前缀+人设）+ fake user 轮（尾部拼尾），标记齐全。"""
    ctx = ContextState(account_id="acc-test")
    ctx.set_user_language("cantonese")
    ctx.set_flow("第 1 步：问候并索要单号\n第 2 步：确认损坏情况", "第 1 步：问候并索要单号")
    ctx.set_object_brief("背景：客户反映集运件损坏\n备注：已投保")
    instructions = "你是小美，代表Bok集运。角色基调：专业客服。"

    msgs = _build_prefix_prewarm_messages(ctx, instructions)

    assert [m["role"] for m in msgs] == ["system", "user"]
    system = msgs[0]["content"]
    # 稳定指令前缀标记（KV-cache 关键段）逐个在场
    assert "【用户语言】" in system
    assert "cantonese" in system or "粤语" in system
    assert "【回复节奏】" in system
    assert "【应答准则】" in system
    assert "话术流程总览" in system
    assert "【对象档案】" in system
    # 人设 base 拼喺前缀之后（同一条 system）
    assert "你是小美" in system
    # fake user 轮带易变尾部（当前步）——与 ContextAwareLLM 拼接位一致
    user = msgs[1]["content"]
    assert user.startswith("你好。")
    assert "【现在这一步】" in user
    assert "第 1 步：问候并索要单号" in user


def test_prefix_prewarm_messages_without_optional_sections():
    """无话术/无档案/空 instructions：system 只有通用准则段，user 无尾部。"""
    ctx = ContextState(account_id="acc-test")
    ctx.set_user_language("zh")
    ctx.set_flow("", "")
    msgs = _build_prefix_prewarm_messages(ctx, "")
    assert msgs[0]["role"] == "system"
    assert "【用户语言】" in msgs[0]["content"]
    assert "话术流程总览" not in msgs[0]["content"]
    assert msgs[1]["content"] == "你好。"


def test_prewarm_request_single_call_max_tokens_1():
    """经 MlxLlmLLM.prefix_prewarm：恰一次请求、max_tokens=1、system 内容带标记。"""

    class _FakeCompletions:
        def __init__(self):
            self.calls: list[dict] = []

        async def create(self, **kw):
            self.calls.append(kw)
            return types.SimpleNamespace(choices=[])

    class _FakeChat:
        def __init__(self):
            self.completions = _FakeCompletions()

    llm = MlxLlmLLM(model="test-model")
    fake = _FakeChat()
    llm._client = types.SimpleNamespace(chat=fake)

    ctx = ContextState(account_id="acc-test")
    ctx.set_user_language("cantonese")
    ctx.set_flow("总览A", "当前步B")
    msgs = _build_prefix_prewarm_messages(ctx, "你是小美。")

    asyncio.run(llm.prefix_prewarm(msgs))

    assert len(fake.completions.calls) == 1
    call = fake.completions.calls[0]
    assert call["max_tokens"] == 1
    assert call["model"] == "test-model"
    # 冷启动竞态：预热会排在开场白 prefill 后面，共享 client read=5s 会提前放弃
    # ——per-request read=30s 钉死（实测 APITimeoutError 根因）。
    assert getattr(call["timeout"], "read", None) == 30.0
    sent = call["messages"]
    assert sent[0]["role"] == "system"
    assert "【用户语言】" in sent[0]["content"]
    assert "话术流程总览" in sent[0]["content"]


def test_prefix_prewarm_enabled_default_and_env_off(monkeypatch):
    monkeypatch.delenv("LLM_PREFIX_PREWARM", raising=False)
    assert _prefix_prewarm_enabled() is True
    monkeypatch.setenv("LLM_PREFIX_PREWARM", "0")
    assert _prefix_prewarm_enabled() is False
    monkeypatch.setenv("LLM_PREFIX_PREWARM", "1")
    assert _prefix_prewarm_enabled() is True


def test_prefix_prewarm_turn1_shape_with_greeting():
    """W0-2：greeting_text 提供时，预热形状=[system, assistant(开场白), user]
    ——与真实 turn-1 请求同构（旧 v1 把 instructions 拼进 system 致分叉 cached=0）。"""
    ctx = ContextState(account_id="acc-test")
    ctx.set_user_language("cantonese")
    ctx.set_flow("总览A", "当前步B")
    msgs = _build_prefix_prewarm_messages(ctx, "你是小美。", "你好，请讲下你个单号。")
    assert [m["role"] for m in msgs] == ["system", "assistant", "user"]
    assert msgs[1]["content"] == "你好，请讲下你个单号。"
    assert "你是小美" in msgs[0]["content"]
    assert "【现在这一步】" in msgs[2]["content"]
    # instructions(=人设 base)照旧喺 system[0]；greeting 生成期那条独立尾 system
    # 唔会出现在 turn-1,故 assistant 轮就是开场白原文而非任何指令。
    assert "【用户语言】" in msgs[0]["content"]
