"""W0-2 追加式尾部：上一轮请求必须是下一轮的严格前缀（KV-cache 铁律）。

2026-09-05 INFO 取证：旧设计尾部只拼在最后一条 user、下轮被剥 → 中途分叉，
cached 恒=system 锚点，对话历史每轮全量重 prefill。新设计把每个 user 的尾部
冻结进账本并逐轮原样重放 → 请求序列纯追加。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from livekit.agents import llm

from agent_runtime.providers.livekit_plugins import ContextAwareLLM, ContextState  # noqa: E402


class _CaptureInner:
    """抓每次 chat() 收到的 chat_ctx items（序列化成文本便于前缀断言）。"""

    def __init__(self):
        self.captured: list[list[tuple[str, str]]] = []

    def on(self, *a, **k):  # _bind_metrics_forward 需要
        pass

    async def chat(self, *, chat_ctx, **kw):
        self.captured.append(
            [(getattr(it, "role", ""), it.content if isinstance(it.content, str) else str(it.content)) for it in chat_ctx.items]
        )
        return "ok"


def _make_ctx():
    return ContextAwareLLM(inner=_CaptureInner(), context_state=ContextState(account_id="t"))


def _chat(llm, turns: list[str]):
    chat_ctx = llm.ChatContext()
    chat_ctx = llm._ctx  # noqa: F841 - 占位防误用
    return None


def _run(llm, history: list[tuple[str, str]]):
    from livekit.agents.llm import ChatContext

    cc = ChatContext()
    for role, text in history:
        cc.add_message(role=role, content=text)
    asyncio.run(llm.chat(chat_ctx=cc))
    return llm._inner.captured[-1]


def test_tail_persisted_requests_are_strict_prefixes():
    llm = _make_ctx()
    llm._ctx.set_user_language("zh")
    llm._ctx.set_flow("总览", "第1步：问候")
    r1 = _run(llm, [("system", "人设base"), ("user", "你好")])
    llm._ctx.set_flow("总览", "第2步：理赔方案")  # 步进发生在两轮之间
    r2 = _run(llm, [("system", "人设base"), ("user", "你好"), ("assistant", "您好"), ("user", "点解咁耐")])

    s1 = "\n".join(f"{ro}:{c}" for ro, c in r1)
    s2 = "\n".join(f"{ro}:{c}" for ro, c in r2)
    assert s2.startswith(s1 + "\n"), f"请求2必须是请求1的严格前缀延续\n---r1---\n{s1}\n---r2---\n{s2}"
    # 旧 user 的尾部被冻结重放（不是剥掉）
    assert any("第1步：问候" in c for ro, c in r2 if ro == "user")
    # 新 user 带新步骤尾部
    assert any("第2步：理赔方案" in c for ro, c in r2 if ro == "user")


def test_no_user_request_skips_tail_and_records_nothing():
    llm = _make_ctx()
    llm._ctx.set_user_language("zh")
    r1 = _run(llm, [("system", "人设base")])  # greeting 形状：无 user
    assert len(llm._ctx.applied_tails()) == 0
    r2 = _run(llm, [("system", "人设base"), ("user", "咩单")])
    s1 = "\n".join(f"{ro}:{c}" for ro, c in r1)
    s2 = "\n".join(f"{ro}:{c}" for ro, c in r2)
    assert s2.startswith(s1 + "\n")


def test_preemptive_rebuild_rewrites_stale_tail_on_last_user():
    """抢跑重建轮(n_new==0)且尾部已实质变化(推进,revision+1):末条 user 尾部
    要重渲染成当前版——否则重建请求仍带旧步骤语境,回复照旧步讲
    (2026-09-06 call-e6e5f18e:已推进第 4 步仍念第 3 步)。"""
    llm = _make_ctx()
    llm._ctx.set_user_language("zh")
    llm._ctx.set_flow("总览", "第1步：问候")
    _run(llm, [("system", "人设base"), ("user", "你好")])
    # 步进发生在「抢跑请求已按旧尾部发出」之后 → 框架作废旧抢跑、按同历史重建
    llm._ctx.set_flow("总览", "第2步：理赔方案")
    r2 = _run(llm, [("system", "人设base"), ("user", "你好")])  # n_new==0 重建
    # 重建请求里末条 user 尾部必须对齐到新步骤(唔再带旧步)
    assert any(ro == "user" and "第2步：理赔方案" in c for ro, c in r2)
    assert not any(ro == "user" and "第1步：问候" in c for ro, c in r2)
    # 账本同步:下一轮真实请求以重建结果为严格前缀,链路重新闭合
    r3 = _run(llm, [("system", "人设base"), ("user", "你好"), ("assistant", "好的"), ("user", "点解咁耐")])
    s2 = "\n".join(f"{ro}:{c}" for ro, c in r2)
    s3 = "\n".join(f"{ro}:{c}" for ro, c in r3)
    assert s3.startswith(s2 + "\n"), f"重建后下一轮必须以重建请求为严格前缀\n---r2---\n{s2}\n---r3---\n{s3}"
    # 新 user 带新步骤尾部,旧 user 冻结重放的也是新步骤(同一版)
    assert any(ro == "user" and "第2步：理赔方案" in c for ro, c in r3)


def test_preemptive_retry_without_revision_keeps_frozen_tail():
    """普通抢跑重试(无推进,revision 未变):原样重放冻结尾部,严格前缀不破。"""
    llm = _make_ctx()
    llm._ctx.set_user_language("zh")
    llm._ctx.set_flow("总览", "第1步：问候")
    r1 = _run(llm, [("system", "人设base"), ("user", "你好")])
    r2 = _run(llm, [("system", "人设base"), ("user", "你好")])  # 同历史重试
    s1 = "\n".join(f"{ro}:{c}" for ro, c in r1)
    s2 = "\n".join(f"{ro}:{c}" for ro, c in r2)
    assert s1 == s2, "无变化重试轮请求字节必须完全一致(严格前缀)"
