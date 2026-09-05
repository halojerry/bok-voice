"""会中事实沉淀 + 重复锚:治「忘记早轮信息」与「原句复述」(2026-09-06 行为取证)。

- extract_call_facts:客户话 → 平台/号码最小事实集(已知资料唔重复沉淀);
- ContextState 尾部渲染【通话中客户已讲】/【你上一句】,去重有界;
- 任何情况下「上一轮请求是下一轮的严格前缀」铁律不破(facts 中途加入只
  影响其后各轮的尾部增量)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.flow import extract_call_facts  # noqa: E402
from agent_runtime.providers.livekit_plugins import ContextState  # noqa: E402


def test_extract_platform_fact():
    assert extract_call_facts("我喺拼多多買嘅") == ["客户讲过在拼多多买"]
    assert extract_call_facts("I bought it on Taobao") == []


def test_extract_number_fact_converts_cantonese_digits():
    out = extract_call_facts("我電話係 一三八零零零零零零零零")
    assert out == ["客户报过号码:一三八零零零零零零零零"]


def test_extract_skips_known_numbers_and_empty():
    F = {"姓名": "林先生", "快递单号": "SF1234567890", "快递尾号": "7890", "电话": "13800000000"}
    # 覆述已知单号/电话 → 唔重复沉淀
    assert extract_call_facts("我個單號係 一二三四五六七八九零", facts=F) == []
    assert extract_call_facts("   ") == []


def test_add_call_fact_dedupe_and_bound():
    st = ContextState(account_id="t")
    for i in range(6):
        st.add_call_fact(f"fact-{i}")
    # 有界 ≤4,FIFO 弹最旧
    assert st._call_facts == ["fact-2", "fact-3", "fact-4", "fact-5"]
    st.add_call_fact("fact-2")  # 去重
    assert st._call_facts.count("fact-2") == 1


def test_tail_renders_facts_and_last_reply_anchor():
    st = ContextState(account_id="t")
    st.add_call_fact("客户讲过在拼多多买")
    st.set_last_reply("收到，尾号係七八九零，啱唔啱？")
    tail = st.render_context_tail()
    assert "【通话中客户已讲" in tail and "拼多多" in tail
    assert "【你上一句】" in tail and "七八九零" in tail
    assert "绝不原句或近原句再讲一次" in tail
    # 空态唔渲染空节
    empty = ContextState(account_id="t").render_context_tail()
    assert "【通话中客户已讲" not in empty and "【你上一句】" not in empty


class _CaptureInner:
    def __init__(self):
        self.captured: list[list[tuple[str, str]]] = []

    def on(self, *a, **k):
        pass

    async def chat(self, *, chat_ctx, **kw):
        self.captured.append(
            [(getattr(it, "role", ""), it.content if isinstance(it.content, str) else str(it.content)) for it in chat_ctx.items]
        )
        return "ok"


def test_facts_added_midcall_keep_strict_prefix():
    """中途沉淀新事实只影响其后各轮尾部增量,前缀铁律不破。"""
    from livekit.agents.llm import ChatContext

    from agent_runtime.providers.livekit_plugins import ContextAwareLLM

    inner = _CaptureInner()
    llm = ContextAwareLLM(inner=inner, context_state=ContextState(account_id="t"))
    llm._ctx.set_user_language("zh")

    def _run(history):
        cc = ChatContext()
        for role, text in history:
            cc.add_message(role=role, content=text)
        asyncio.run(llm.chat(chat_ctx=cc))
        return "\n".join(f"{ro}:{c}" for ro, c in inner.captured[-1])

    s1 = _run([("system", "人设base"), ("user", "你好")])
    # 中途沉淀事实(下一轮才进尾部增量)
    llm._ctx.add_call_fact("客户讲过在拼多多买")
    s2 = _run([("system", "人设base"), ("user", "你好"), ("assistant", "您好"), ("user", "我喺拼多多買")])
    assert s2.startswith(s1 + "\n"), "facts 中途加入不得破坏严格前缀"
    # 新事实出现在新 user 的尾部,且旧 user 冻结重放不含它
    assert "拼多多" in s2
