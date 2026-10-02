"""EX-1 尾部封洞单测（2026-09-28）。

背景：本仓 mlx LRUPromptCache 只复用**严格前缀**——每个 user 消息首次进 LLM
请求时拼上「当时」的尾部（当前步/记忆/事实/锚），字节冻结进
ContextState._applied_tails 永久原样重放。非 LLM 回复车道（flow say 直念/QA
罐头/graph play/分支罐头）把客户消息 append 进 chat 后从不调 LLM，该消息永不入
尾部账本；下一真轮 n_new≥2，旧循环给每条新消息都渲染尾部：稳定段落在「已被替答」
的洞消息上、最新消息只拿紧凑标签，且末条账本稳定键为空 → 下轮又重发一次稳定段
（稳定段付两遍，实测 ~1975 未缓存 token ≈ 2.2-2.4s）。

修法：ContextAwareLLM.chat 的 `for idx in users[len(applied):]` 循环——除最后一条
外全部冻结裸体（无尾部、稳定键恒空），稳定段只由最新消息照常发出一次。

本测覆盖：洞场景形状（A 裸体 / B 带尾部）+ 常规轮零漂移 + 严格前缀跨洞 +
ContextState 裸体记账语义 + prune 对齐 + 源级 pin + 投机器预览一致性 + 字符节省。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "apps" / "agent"))

from livekit.agents.llm import ChatContext  # noqa: E402

from agent_runtime.prefill_speculator import PrefillSpeculator  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    ContextAwareLLM,
    ContextState,
)

_PLUGINS_SRC = _REPO / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"

STEP1 = "流程第 1/5 步\n这一步要达成:确认客户身份"
# 真实步骤稳定段有底稿/注意等长文本（典型 ~300 字）——用足量长度令「全量稳定段
# 显著长于紧凑标签」成立，字符节省断言才有机制意义。
STEP3 = (
    "流程第 3/5 步\n"
    "这一步要达成:向客户索取其本人的微信或 WhatsApp 号码，用于后续进度跟进。\n"
    "本步底稿(勿照念):唔好意思阻你少少时间，我哋需要你个常用嘅微信或者 WhatsApp 号码，"
    "方便同事将处理进度同步俾你。你而家报一个常用嘅号码俾我，我即刻帮你备注落系统度，"
    "之后再同你确认一次，确保同事一定联系得到你。"
)


@pytest.fixture(autouse=True)
def _disable_polish(monkeypatch):
    # 润色层与尾部封洞无关；关掉令 orig==final，断言基于原文。
    monkeypatch.setenv("BOK_ASR_POLISH", "0")


# ----------------------------------------------------------------- 测试替身

def _text(it) -> str:
    c = getattr(it, "content", "")
    if isinstance(c, str):
        return c
    return "".join(x for x in (c or []) if isinstance(x, str))


class _CaptureInner:
    """照抄 test_call_facts_anchor._CaptureInner：返回非 LLMStream 令 chat 直通。"""

    def __init__(self):
        self.captured: list[list] = []

    def on(self, *a, **k):
        pass

    async def chat(self, *, chat_ctx, **kw):
        self.captured.append(list(chat_ctx.items))
        return "ok"


def _snap(items) -> list[tuple[str, str]]:
    return [(getattr(it, "role", ""), _text(it)) for it in items]


def _mk():
    inner = _CaptureInner()
    ctx = ContextState(account_id="t")
    ctx.set_user_language("zh")
    ctx.set_flow_current(STEP1)
    llm = ContextAwareLLM(inner=inner, context_state=ctx)
    return inner, ctx, llm


def _run(llm: ContextAwareLLM, history: list[tuple[str, str]]) -> None:
    cc = ChatContext()
    for role, text in history:
        cc.add_message(role=role, content=text)
    asyncio.run(llm.chat(chat_ctx=cc))


def _user_texts(items) -> list[str]:
    return [t for r, t in _snap(items) if r == "user"]


_HIST_ROUND1 = [("system", "人设base"), ("user", "你好")]


def _hist_hole() -> list[tuple[str, str]]:
    return [
        ("system", "人设base"),
        ("user", "你好"),
        ("assistant", "您好，请问是陈小姐吗？"),
        ("user", "A洞消息"),          # 非 LLM 车道替答（未走 chat）→ 洞里那条
        ("user", "B最新客户话"),
    ]


# --------------------------------------------------------- 1. 洞场景形状

def test_hole_message_frozen_bare_newest_carries_tail():
    inner, ctx, llm = _mk()
    _run(llm, _HIST_ROUND1)
    assert len(ctx.applied_tails()) == 1
    # say 车道推进步骤（稳定键变）后 append 洞消息 A，再 append 最新 B；均未经 chat。
    ctx.set_flow_current(STEP3)
    _run(llm, _hist_hole())

    users = _user_texts(inner.captured[-1])
    assert len(users) == 3
    u_a, u_b = users[1], users[2]

    # A（洞消息）=裸体：无尾部、无稳定段。
    assert u_a == "A洞消息", f"洞消息必须裸体，实际={u_a!r}"
    assert "【现在这一步】" not in u_a
    assert "【" not in u_a
    # B（最新消息）=照常带尾部，且稳定段只发这一次。
    assert u_b.startswith("B最新客户话\n\n")
    assert "【现在这一步】" in u_b and "向客户索取其本人的微信" in u_b
    assert u_b.count("【现在这一步】") == 1

    # 账本：A 是普通账本项（orig==final==裸体），稳定键恒空；B 带稳定键。
    at = ctx.applied_tails()
    assert len(at) == 3
    assert at[-2][0] == "A洞消息" and at[-2][1] == "A洞消息"
    assert at[-1][0] == "B最新客户话" and at[-1][1].startswith("B最新客户话\n\n")
    sk = ctx._applied_stable_keys
    assert sk[-2] == "", "洞消息不得标记稳定段（否则最新消息会误判已发）"
    assert sk[-1] != "" and sk[-1] == ctx._stable_key


# ------------------------------------------------- 2. 常规轮零漂移回归

def test_normal_round_shape_unchanged(monkeypatch):
    """n_new==1 请求形状与修前逐字节同：尾部在场、稳定段只发一次。"""
    inner, ctx, llm = _mk()
    _run(llm, _HIST_ROUND1)
    users = _user_texts(inner.captured[-1])
    assert len(users) == 1
    assert users[0].startswith("你好\n\n")
    assert "【现在这一步】" in users[0]
    assert users[0].count("【现在这一步】") == 1
    # 账本只长 1 条（无裸体条目）；稳定键被正常消费。
    assert len(ctx.applied_tails()) == 1
    assert ctx._applied_stable_keys == [ctx._stable_key]

    # 同一步内的下一常规轮：紧凑标签，不再发稳定段（旧行为不变）。
    _run(
        llm,
        [
            ("system", "人设base"),
            ("user", "你好"),
            ("assistant", "您好。"),
            ("user", "我係陈小姐"),
        ],
    )
    users2 = _user_texts(inner.captured[-1])
    assert "【现在这一步】" not in users2[-1]
    assert "·继续】" in users2[-1]
    assert len(ctx.applied_tails()) == 2


# ----------------------------------------------------- 3. 严格前缀跨洞

def test_strict_prefix_and_bare_replay_across_hole():
    inner, ctx, llm = _mk()
    _run(llm, _HIST_ROUND1)
    ctx.set_flow_current(STEP3)
    _run(llm, _hist_hole())
    snap2 = _snap(inner.captured[-1])

    # 下一真轮：上一轮请求必须仍是这一轮的严格前缀（洞消息裸体重放字节一致）。
    _run(llm, _hist_hole() + [("assistant", "r2"), ("user", "C第三轮")])
    snap3 = _snap(inner.captured[-1])
    assert snap3[: len(snap2)] == snap2, "上一轮请求必须是下一轮的严格前缀"

    u_a2 = _user_texts(inner.captured[-2])[1]
    u_a3 = _user_texts(inner.captured[-1])[1]
    assert u_a2 == u_a3 == "A洞消息", "洞消息跨轮原样重放"


# ------------------------------------ 4. ContextState 裸体记账语义 / prune

def test_record_bare_tail_does_not_consume_stable_key():
    ctx = ContextState(account_id="t")
    ctx.set_user_language("zh")
    ctx.set_flow_current(STEP1)
    ctx.render_context_tail()  # 模拟某条消息发过稳定段（_last_emit_stable_key=K1）
    assert ctx._last_emit_stable_key == ctx._stable_key
    ctx.record_applied_tail("hole", "hole", bare=True)  # 洞消息裸体补冻
    assert ctx._applied_stable_keys[-1] == ""
    # 关键：裸体不消费稳定段身份——紧随其后的最新消息仍应发出稳定段。
    tail = ctx.render_context_tail()
    assert "【现在这一步】" in tail


def test_record_normal_tail_still_consumes_stable_key():
    ctx = ContextState(account_id="t")
    ctx.set_user_language("zh")
    ctx.set_flow_current(STEP1)
    first = ctx.render_context_tail()
    ctx.record_applied_tail("u1", "u1\n\n" + first)
    assert ctx._applied_stable_keys[-1] == ctx._stable_key
    # 普通记账消费稳定段身份 → 同一步后续只发紧凑标签。
    assert "【现在这一步】" not in ctx.render_context_tail()
    assert "·继续】" in ctx.render_context_tail()


def test_prune_applied_tails_keeps_bare_entries_aligned():
    ctx = ContextState(account_id="t")
    for i in range(3):
        ctx.record_applied_tail(f"n{i}", f"n{i}", bare=True)
    for i in range(3):
        ctx.record_applied_tail(f"t{i}", f"t{i}\n\ntail")
    assert len(ctx.applied_tails()) == 6
    assert len(ctx._applied_stable_keys) == 6
    ctx.prune_applied_tails(2)
    assert len(ctx.applied_tails()) == 2
    assert len(ctx._applied_stable_keys) == 2
    assert [e[0] for e in ctx.applied_tails()] == ["t1", "t2"]
    assert ctx._applied_stable_keys == ["", ""]  # 裸体在前，正常在后，自尾对齐


# ----------------------------------------------------------- 5. 源级 pin

def test_source_pin_bare_freeze_loop():
    src = _PLUGINS_SRC.read_text(encoding="utf-8")
    assert "new_indices = users[len(applied):]" in src
    i_bare = src.index("for idx in new_indices[:-1]:")
    i_last = src.index("for idx in new_indices[-1:]:")
    i_end = src.index("self._ctx.prune_applied_tails(keep=len(users))", i_last)
    bare_seg = src[i_bare:i_last]
    last_seg = src[i_last:i_end]

    # 裸体段：不渲染尾部、记裸体账（稳定键恒空）。
    assert "render_context_tail" not in bare_seg
    assert "record_applied_tail(orig, final, bare=True)" in bare_seg
    # 最新消息段：保留原行为（渲染尾部 + 普通记账）。
    assert "render_context_tail()" in last_seg
    assert "record_applied_tail(orig, final)" in last_seg

    # AST：裸体循环体内 record_applied_tail 必须带 bare=True 关键字，且不得调渲染。
    tree = ast.parse(src)
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        if ast.get_source_segment(src, node.iter) != "new_indices[:-1]":
            continue
        checked += 1
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
        names = {ast.unparse(c.func) for c in calls}
        assert "self._ctx.render_context_tail" not in names
        rec = [c for c in calls if ast.unparse(c.func) == "self._ctx.record_applied_tail"]
        assert rec, "裸体循环必须记账"
        assert any(
            any(k.arg == "bare" and getattr(k.value, "value", None) is True for k in c.keywords)
            for c in rec
        ), "裸体记账必须带 bare=True"
    assert checked == 1, "恰好一个裸体冻结循环"


# ------------------------------------- 6. 投机器预览与下一真轮一致性

def test_speculator_preview_matches_next_real_request_under_hole():
    """洞场景下，投机组装的「最新 user 段」须与下一真轮的最新消息逐字节一致。"""
    inner, ctx, llm = _mk()
    _run(llm, _HIST_ROUND1)
    ctx.set_flow_current(STEP3)
    _run(llm, _hist_hole())
    snap2 = _snap(inner.captured[-1])

    fires: list[list[dict]] = []

    async def _prewarm(msgs):
        fires.append(msgs)

    spec = PrefillSpeculator(_prewarm, ctx)
    spec.on_request_messages([{"role": r, "content": t} for r, t in snap2])
    spec.on_reply_history_text("r1历史原文")
    spec.set_busy(False)
    spec._last_fire_ts = 0.0
    spec._last_final_ts = 0.0
    c_raw = "唔該我而家想問下进度"

    async def _run_spec():
        spec.on_stable_prefix(c_raw)
        assert spec._task is not None
        await spec._task

    asyncio.run(_run_spec())
    assert len(fires) == 1
    preview_user = fires[0][-1]
    assert preview_user["role"] == "user"
    assert preview_user["content"].startswith(c_raw + "\n\n")

    # 下一真轮：同一条 C 原话——ContextAwareLLM 追加的尾部须与投机预览一致。
    _run(llm, _hist_hole() + [("assistant", "r2"), ("user", c_raw)])
    real_user = _user_texts(inner.captured[-1])[-1]
    assert real_user == preview_user["content"], "投机预览与真请求最新消息段必须逐字节一致"


# --------------------------------------------------- 7. 机制级字符节省

def test_hole_round_char_saving():
    """洞轮：洞消息尾 0 字符；下一真轮不再重发全量稳定段（旧行为会重发）。"""
    inner, ctx, llm = _mk()
    _run(llm, _HIST_ROUND1)
    ctx.set_flow_current(STEP3)
    _run(llm, _hist_hole())

    u = _user_texts(inner.captured[-1])
    a_raw, b_raw = "A洞消息", "B最新客户话"
    hole_tail_chars = len(u[1]) - len(a_raw)
    full_tail_chars = len(u[2]) - len(b_raw)  # 稳定段 + 增量段（=旧代码洞消息会背的尾部）
    assert hole_tail_chars == 0, "洞消息必须裸体"
    assert full_tail_chars > 0

    _run(llm, _hist_hole() + [("assistant", "r2"), ("user", "C第三轮")])
    u3 = _user_texts(inner.captured[-1])
    c_tail_chars = len(u3[-1]) - len("C第三轮")
    saved = full_tail_chars - c_tail_chars
    assert saved > 0, "最新消息稳定段已由上一轮最新消息携带，不得重发"
    print(
        "\nTAIL_HOLE_SAVING "
        f"hole_msg_tail_before={full_tail_chars} hole_msg_tail_after={hole_tail_chars} "
        f"next_round_tail_before={full_tail_chars} next_round_tail_after={c_tail_chars} "
        f"saved_chars≈{saved}"
    )
