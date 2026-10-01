"""D1 槽位化 actor 冻结 round-trip：严格前缀（append-only）+ say 轮裸冻结 + 重建轮重锚。

机制（锁死设计决策①）：任务块随当轮 user 消息注入、经 ContextState 既有
record_applied_tail 冻结入史——历史重放逐字节原样，上一轮请求恒为下一轮严格
前缀（mlx KV-cache 铁律，tests/test_context_append_only.py 同款契约的槽位版）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from livekit.agents.llm import ChatContext  # noqa: E402

from agent_runtime.flow import FlowController  # noqa: E402
from agent_runtime.providers.livekit_plugins import ContextAwareLLM, ContextState  # noqa: E402
from agent_runtime.slot_actor import build_slot_system  # noqa: E402

_STEPS = json.dumps(
    [
        {"goal": "确认身份", "ref": "请问是陈先生吗？"},
        {"goal": "通知遗失", "ref": "很抱歉，您的货件遗失了，我们会负责赔偿。", "say": True},
        {"goal": "核实平台", "ref": "您是在拼多多还是淘宝买的？"},
    ],
    ensure_ascii=False,
)

_CARD = build_slot_system(
    persona={"name": "小蓝", "company": "集运中转仓", "language": "zh"},
    template=None,
    facts="",
    language="zh",
)


class _CaptureInner:
    def __init__(self):
        self.captured = []

    def on(self, *a, **k):
        pass

    async def chat(self, *, chat_ctx, **kw):
        self.captured.append(list(chat_ctx.items))
        return "ok"


def serialize(items) -> str:
    lines = []
    for it in items:
        c = getattr(it, "content", "")
        if isinstance(c, str):
            text = c
        else:
            text = "\n".join(str(x) for x in (c or []))
        lines.append(f"{getattr(it, 'role', '')}\x1f{text}")
    return "\n".join(lines)


def user_texts(items) -> list[str]:
    out = []
    for it in items:
        if getattr(it, "role", "") != "user":
            continue
        c = getattr(it, "content", "")
        out.append(c if isinstance(c, str) else "\n".join(str(x) for x in (c or [])))
    return out


def _make_slot(account: str = "acc-001"):
    ctx = ContextState(account_id=account)
    ctx.slot_mode = True
    ctx.set_slot_system(_CARD)
    ctx.set_user_language("zh")
    inner = _CaptureInner()
    llmw = ContextAwareLLM(inner=inner, context_state=ctx)
    fc = FlowController.from_template({"steps_json": _STEPS}, None)
    return ctx, fc, llmw, inner


def _chat(llmw, history):
    cc = ChatContext()
    for role, text in history:
        cc.add_message(role=role, content=text)
    asyncio.run(llmw.chat(chat_ctx=cc))


def test_slot_five_rounds_strict_prefix_and_frozen_history_immutable():
    """连续 5 轮：上一轮请求是下一轮严格前缀；历史 user 消息逐字节不变（无重写）。"""
    ctx, fc, llmw, inner = _make_slot()
    history = [("system", "人設base"), ("assistant", "您好，请问是陈先生吗？")]
    ctx.set_slot_step(fc.slot_step_view())
    ctx.set_last_reply("您好，请问是陈先生吗？")
    ctx.set_object_brief("拼多多客户")
    reqs = []
    for i, utt in enumerate(["是我", "什么时候到账", "好的", "你们是哪里的", "谢谢"], 1):
        history.append(("user", utt))
        fc.last_user_text = utt
        fc.last_verdict = "confirm" if i % 2 else "question"
        if i == 2:
            fc.advance()  # 进第 2 步（say，已读账本按已念处理）
            fc.note_step_said()
        if i == 4:
            fc.advance()  # 进第 3 步
        ctx.set_slot_step(fc.slot_step_view())
        _chat(llmw, history)
        history.append(("assistant", f"回复{i}"))
        ctx.set_last_reply(f"回复{i}")
        reqs.append(serialize(inner.captured[-1]))
    assert len(reqs) == 5
    for i in range(1, 5):
        assert reqs[i].startswith(reqs[i - 1] + "\n"), f"第{i + 1}轮不是第{i}轮严格前缀"
    # 历史 user 消息逐字节冻结（applied_tails 原样重放，无重写痕迹）。
    users_per_round = [user_texts(inner.captured[i]) for i in range(5)]
    for i in range(1, 5):
        assert users_per_round[i][:i] == users_per_round[i - 1][:i], (
            f"第{i + 1}轮的历史 user 消息被改写（非 append-only）"
        )
    # 最新 user 消息带任务块，旧冻结块照留（会话记忆）。
    assert "【当前步】第3/3步" in users_per_round[-1][-1]
    assert "【当前步】第1/3步" in users_per_round[-1][0]


def test_slot_say_round_bare_freeze_then_prefix_holds():
    """say 直念轮（无 LLM 请求）的 user 消息在后续轮裸冻结（EX-1 封洞槽位版）：
    下一真轮该消息只带润色正文（无任务块），且前缀链不断。"""
    ctx, fc, llmw, inner = _make_slot()
    history = [("system", "人設base"), ("assistant", "您好，请问是陈先生吗？")]
    ctx.set_slot_step(fc.slot_step_view())
    # R1：真 LLM 轮（第 1 步）。
    history.append(("user", "是我"))
    fc.last_user_text = "是我"
    fc.last_verdict = "confirm"
    fc.advance()  # → 第 2 步（say）
    ctx.set_slot_step(fc.slot_step_view())
    _chat(llmw, history)
    r1 = serialize(inner.captured[-1])
    history.append(("assistant", "R1回复"))
    ctx.set_last_reply("R1回复")
    # R2：say 直念轮——真钩子 note_step_said 后 push、直念作答、无 LLM 请求。
    history.append(("user", "知道了"))
    fc.last_user_text = "知道了"
    fc.last_verdict = "confirm"
    fc.note_step_said()
    ctx.set_slot_step(fc.slot_step_view())
    history.append(("assistant", "（直念）很抱歉，您的货件遗失了。"))
    ctx.set_last_reply("很抱歉，您的货件遗失了。")
    # R3：真 LLM 轮（第 3 步）——n_new=2：旧洞消息裸冻结。
    history.append(("user", "在拼多多买的"))
    fc.last_user_text = "在拼多多买的"
    fc.last_verdict = "confirm"
    fc.advance()  # → 第 3 步
    ctx.set_slot_step(fc.slot_step_view())
    _chat(llmw, history)
    r3 = serialize(inner.captured[-1])
    assert r3.startswith(r1 + "\n"), "say 轮后前缀链断裂"
    users = user_texts(inner.captured[-1])
    assert users[1].strip() == "知道了", f"say 轮洞消息必须裸冻结（无任务块）: {users[1]!r}"
    assert "【当前步】第3/3步" in users[-1]
    assert "【当前步】" not in users[1]


def test_slot_rebuild_rerenders_only_last_user_and_recloses_prefix():
    """抢跑重建（n_new==0 + 换步）：只重渲染末条 user 的任务块，旧消息不动；
    下一真轮以重建结果为严格前缀（链路重新闭合）。"""
    ctx, fc, llmw, inner = _make_slot()
    history = [("system", "人設base"), ("assistant", "您好")]
    ctx.set_slot_step(fc.slot_step_view())
    history.append(("user", "是我"))
    fc.last_user_text = "是我"
    fc.last_verdict = "confirm"
    ctx.set_slot_step(fc.slot_step_view())
    _chat(llmw, history)
    r1 = serialize(inner.captured[-1])
    # 推进（第 1→2 步）后按同历史重建：末条 user 重渲染新步任务块。
    fc.advance()
    ctx.set_slot_step(fc.slot_step_view())
    _chat(llmw, history)  # n_new==0
    r2 = serialize(inner.captured[-1])
    users2 = user_texts(inner.captured[-1])
    assert "【当前步】第2/3步" in users2[-1], "重建轮末条 user 必须对齐新步"
    assert "【当前步】第1/3步" not in users2[-1]
    # 下一真轮严格前缀。
    history.append(("assistant", "好的"))
    ctx.set_last_reply("好的")
    history.append(("user", "在拼多多买的"))
    fc.last_user_text = "在拼多多买的"
    fc.last_verdict = "confirm"
    fc.advance()  # → 第 3 步
    ctx.set_slot_step(fc.slot_step_view())
    _chat(llmw, history)
    r3 = serialize(inner.captured[-1])
    assert r3.startswith(r2 + "\n"), f"重建后前缀链未闭合\n---r2---\n{r2}\n---r3---\n{r3}"
    assert r1 != r2


def test_slot_set_step_revision_follows_step_identity():
    """prefill 投机 F6 稳定门契约：换步（身份键变）bump revision；同步内重推不 bump。"""
    ctx, fc, llmw, inner = _make_slot()
    r0 = ctx.revision
    ctx.set_slot_step(fc.slot_step_view())  # 第 1 步
    r1 = ctx.revision
    assert r1 > r0
    ctx.set_slot_step(fc.slot_step_view())  # 同步重推（视图内容同）
    assert ctx.revision == r1, "同一步重推不得虚增 revision"
    fc.advance()
    ctx.set_slot_step(fc.slot_step_view())  # 换步
    assert ctx.revision > r1
    fc.enter_closing()
    ctx.set_slot_step(fc.slot_step_view())  # 收尾态身份键变
    r3 = ctx.revision
    ctx.set_slot_step(fc.slot_step_view())
    assert ctx.revision == r3, "收尾态同步重推不得虚增 revision"
