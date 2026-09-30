"""打断失忆根修（2026-09-30 真机 batch-1 ③「被打断之后不结合上下文」）。

病灶：B4 打断补账只落 turns DB——分析侧知道、生成侧失忆。被打断的半句
不进 chat_ctx / set_last_reply 锚 / 复读账本，客户问「你刚讲咩」时 LLM
历史里那半句不存在（执行端与生成端脱节）。

本修三路并进生成端（speech watcher `_watch` 补账点）：
① chat_ctx 官方姿势注入 assistant 半句（copy→append→update_chat_ctx，
   插在本轮 user 轮后=纯追加，严格前缀契约不破）；
② set_last_reply 重复锚（模型看得见自己被掐前讲了什么）；
③ record_reply 复读账本（gen=llm，跨轮复读防线把半句当 LLM 历史比对）。

kill-switch BOK_INTERRUPT_CTX=0（默认 1），已入 _FORWARD_ENV。
源级 pin + ContextState 单元（repo 惯例：打断路径是闭包内协程，行为面
由 pin 钉住接线，语义面单测 ContextState 账本）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)
BOK_SRC = (ROOT / "tools" / "bok.py").read_text(encoding="utf-8")
LINT_SRC = (ROOT / "tests" / "test_reply_chokepoint_lint.py").read_text(
    encoding="utf-8"
)


def test_interrupt_ctx_gate_and_wiring_pinned():
    """三路注入的闸与接线在 B4 补账点（源级 pin）。"""
    assert 'os.environ.get("BOK_INTERRUPT_CTX", "1") == "1"' in AGENT_SRC
    # ① chat_ctx 注入走 PausableAgent 官方姿势包装
    assert "await agent._try_append_assistant_partial(_ic)" in AGENT_SRC
    # ② 重复锚
    assert "context_state.set_last_reply(_ic)" in AGENT_SRC
    # ③ 复读账本,gen=llm（跨轮复读防线比对面）
    assert 'context_state.record_reply(_ic, "llm")' in AGENT_SRC
    # 观测行在场（真机归因面）
    assert "interrupted reply -> ctx chars=" in AGENT_SRC


def test_assistant_partial_uses_official_path():
    """PausableAgent 包装：assistant ChatMessage + 委托 _try_append_user_message
    （C5 官方姿势 copy→append→update_chat_ctx，旧 items.append 打只读视图恒失败
    的坑不再犯）。"""
    assert "async def _try_append_assistant_partial" in AGENT_SRC
    assert "_lk_llm.ChatMessage(role=\"assistant\"" in AGENT_SRC
    assert "return await self._try_append_user_message(msg)" in AGENT_SRC


def test_forward_env_registered():
    """BOK_INTERRUPT_CTX 已立法（prod 封闭 env 面可达）。"""
    assert '"BOK_INTERRUPT_CTX"' in BOK_SRC


def test_lint_allowlist_covers_watch():
    """chokepoint lint 白名单收编 _watch 直写锚点（interrupted 无 item_added,
    chokepoint 结构性够不着,补账点直写是授权例外）。"""
    assert '("agent.py", "_watch"): "打断半句入锚"' in LINT_SRC


def test_contextstate_ledgers_accept_partial():
    """语义面：半句进锚（截 80）+ 复读账本（gen=llm 参与跨轮比对）。"""
    from agent_runtime.providers.livekit_plugins import ContextState

    ctx = ContextState(account_id="t")
    partial = "我帮你查一下呢个订单嘅物流情况，应该"
    ctx.set_last_reply(partial)
    ctx.record_reply(partial, "llm")
    assert ctx.last_reply == partial
    assert partial in ctx.reply_ledger()

    # 空串忽略（保持上一条锚/不入账本）
    ctx.set_last_reply("")
    assert ctx.last_reply == partial
    n = len(ctx.reply_ledger())
    ctx.record_reply("", "llm")
    assert len(ctx.reply_ledger()) == n

    # 超长半句截 80 字
    long_partial = "字" * 120
    ctx.set_last_reply(long_partial)
    assert len(ctx.last_reply) == 80
