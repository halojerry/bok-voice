"""D1 收尸 + 病理判别字段（2026-09-30 Phase 0 定案）。

病灶（agent.log 25 天全量取证）：被掐回复的 LLM 包装流在消费者断开后悬在
inner 链上永不收尾——`REPEAT_GUARD_CANCEL_DROP` 全时 0 次 = guard 的
CancelledError 分支从未被触发；病理形态=下一轮回复 LLM 正常完成（TTFT/gen
正常、guard/tee 有残留文本）但文本零 push 到 TTS（24/4114 轮，全部无
FIRST_CHUNK 行、stall 看门狗因 sent_any=False 咪有意沉默），客户听 6-8s
死寂后 watchdog 强断收 ack；force-interrupt 清场后 ack 能播=调度链可恢复。

修复契约：
- interrupted 补账点（pending_buffer 已读之后）显式 aclose 两层流引用：
  guard=缓冲任务树根 / reply=最外层（框架消费链），cancel_and_wait 打穿，
  CancelledError 分支自然触发；
- bidi PERF 中断变体补 pushed= 字段（每轮判别「零 push vs 零响应」）；
- watchdog 强断打病理现场任务快照 + py-spy 提示（print-only，无 subprocess）；
- kill-switch BOK_INTERRUPT_REAP（默认 1，0=不收尸），已入 _FORWARD_ENV。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import pytest  # noqa: E402
from livekit.agents import llm  # noqa: E402

from agent_runtime.providers.livekit_plugins import _RepeatSelfGuardStream  # noqa: E402

AGENT_SRC = (ROOT / "apps" / "agent" / "agent_runtime" / "agent.py").read_text(
    encoding="utf-8"
)
PLUGINS_SRC = (
    ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
).read_text(encoding="utf-8")
BOK_SRC = (ROOT / "tools" / "bok.py").read_text(encoding="utf-8")


class _HangingInnerStream(llm.LLMStream):
    """发若干无句界 chunk 后挂起（复刻被掐时 LLM 流未结束的形状）。"""

    def __init__(self, plugin, chunks: list[str]):
        from livekit.agents.types import APIConnectOptions

        super().__init__(
            llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions()
        )
        self._chunks = chunks
        self._hold = asyncio.Event()

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        for i, c in enumerate(self._chunks):
            self._event_ch.send_nowait(
                llm.ChatChunk(id=str(i), delta=llm.ChoiceDelta(content=c, role="assistant"))
            )
        await self._hold.wait()  # 挂住（inner 永不结束）


class _StreamInner(llm.LLM):
    provider = "fake-provider"

    def __init__(self, chunks: list[str]):
        super().__init__()
        self._chunks = chunks

    def chat(self, *, chat_ctx, **kw):
        return _HangingInnerStream(self, self._chunks)


def test_aclose_reaps_hanging_stream_and_fires_cancel_branch(capsys):
    """收尸路径（D1 核心）：消费者断开 → 流悬在 inner 上 → aclose 打穿任务根。

    契约：pending_buffer 先读（账本证据序）→ aclose → _task 收尾、
    CancelledError 分支触发（REPEAT_GUARD_CANCEL_DROP 首次真实可观测）。"""

    async def _main() -> None:
        inner = _StreamInner(["我哋係顺丰，有个包裹單號尾號七八九零運輸途中唔見咗"])
        stream = inner.chat(chat_ctx=llm.ChatContext())
        guard = _RepeatSelfGuardStream(inner, stream, "无关旧回复")
        await asyncio.sleep(0.2)  # chunk 进缓冲
        # 1) 补账点先读 pending_buffer（agent._watch 的现序）
        assert guard.pending_buffer
        # 2) 收尸：aclose = cancel_and_wait(_task)（D1 修复的执行体）
        await asyncio.wait_for(guard.aclose(), timeout=5.0)
        # 3) 任务根终结（不再悬死）
        assert guard._task.done()

    asyncio.run(_main())
    assert "REPEAT_GUARD_CANCEL_DROP chars=" in capsys.readouterr().out


def test_aclose_idempotent_for_already_clean_stream():
    """正常完稿流（缓冲空）aclose 不炸（收尸尽力而为面）。"""

    async def _main() -> None:
        inner = _StreamInner(["你好。"])
        stream = inner.chat(chat_ctx=llm.ChatContext())
        guard = _RepeatSelfGuardStream(inner, stream, "无关")
        await asyncio.sleep(0.2)
        await asyncio.wait_for(guard.aclose(), timeout=5.0)
        await asyncio.wait_for(guard.aclose(), timeout=5.0)  # 双收尸无妨

    asyncio.run(_main())


def test_reap_wiring_source_pins():
    """接线源级 pin：补账点两层收尸 + 闸 + 观测行 + 最外层引用存储。"""
    # agent.py 收尸块
    assert 'os.environ.get("BOK_INTERRUPT_REAP", "1") == "1"' in AGENT_SRC
    assert 'for _reap_layer in ("_last_guard_stream", "_last_reply_stream"):' in AGENT_SRC
    assert "await _reap_stream.aclose()" in AGENT_SRC
    assert "interrupted stream reaped layer=" in AGENT_SRC
    # livekit_plugins.py 最外层引用（chat 返回前的存储）
    assert "self._last_reply_stream = out" in PLUGINS_SRC
    # PERF 中断变体判别字段
    assert "pushed={int(bool(state.get('sent_any')))} (interrupted)" in PLUGINS_SRC
    # watchdog 病理现场快照（print-only）
    assert "[watchdog] dead-turn snapshot tasks=" in AGENT_SRC
    assert "py-spy dump --pid" in AGENT_SRC


# ---- 孤儿门控（2026-09-30 终修）----


class _Layer:
    """包装层占位：_task=本层泵任务（活着是常态），_inner=下一层。"""

    def __init__(self, task=None, inner=None):
        self._task = task
        self._inner = inner


def test_reap_gate_true_when_innermost_generation_done():
    """最内层生成任务已结束=真孤儿 → 允许收尸（外层泵活着不影响判据）。"""
    from agent_runtime.agent import _reap_generation_idle

    async def _main() -> None:
        done_gen = asyncio.create_task(asyncio.sleep(0))
        alive_pump = asyncio.create_task(asyncio.sleep(50))
        await asyncio.sleep(0.05)
        assert done_gen.done()
        raw = _Layer(task=done_gen)
        guard = _Layer(task=alive_pump, inner=raw)
        reply = _Layer(task=alive_pump, inner=guard)
        assert _reap_generation_idle(reply) is True
        assert _reap_generation_idle(guard) is True
        alive_pump.cancel()

    asyncio.run(_main())


def test_reap_gate_false_when_generation_in_flight():
    """最内层生成任务在途 → 跳过收尸（单槽竞态=抽地毯，call-3b776663 形态）。"""
    from agent_runtime.agent import _reap_generation_idle

    async def _main() -> None:
        alive_gen = asyncio.create_task(asyncio.sleep(50))
        alive_pump = asyncio.create_task(asyncio.sleep(50))
        await asyncio.sleep(0.05)
        raw = _Layer(task=alive_gen)
        guard = _Layer(task=alive_pump, inner=raw)
        assert _reap_generation_idle(guard) is False
        alive_gen.cancel()
        alive_pump.cancel()

    asyncio.run(_main())


def test_reap_gate_true_when_task_ref_unreachable():
    """拿不到任务引用（形状变化/裸对象）→ 保守放行收尸（尽力而为面）。"""
    from agent_runtime.agent import _reap_generation_idle

    assert _reap_generation_idle(_Layer()) is True
    assert _reap_generation_idle(object()) is True


def test_reap_gate_on_real_wrapper_chain():
    """真包装链（guard → 原生 LLMStream）：inner._task 在途 → False。"""

    async def _main() -> None:
        inner = _StreamInner(["我哋係顺丰有個包裹單號七八九零"])
        stream = inner.chat(chat_ctx=llm.ChatContext())
        guard = _RepeatSelfGuardStream(inner, stream, "无关旧回复")
        await asyncio.sleep(0.1)
        from agent_runtime.agent import _reap_generation_idle

        assert guard._inner is stream
        assert _reap_generation_idle(guard) is False, "挂起的 inner 流=生成在途"
        await asyncio.wait_for(guard.aclose(), timeout=5.0)
        stream._hold.set()  # 放行 inner 收尾
        await asyncio.sleep(0.1)
        assert _reap_generation_idle(guard) is True, "inner 任务结束后=孤儿"

    asyncio.run(_main())


def test_reap_gate_and_snapshot_source_pins():
    """终修接线源级 pin：门控调用 + 跳过观测行 + 全栈快照 + 调度判别子。"""
    assert "_reap_generation_idle" in AGENT_SRC
    assert "if not _reap_generation_idle(_reap_stream):" in AGENT_SRC
    assert "reap skipped layer=" in AGENT_SRC
    assert "[watchdog] dead-turn stacks:" in AGENT_SRC
    assert "[watchdog] dead-turn sched " in AGENT_SRC
    assert "preemptive=PARKED" in AGENT_SRC
    assert "paused_speech=1" in AGENT_SRC
    assert "REPEAT_GUARD_HEAD_FORCE_RELEASE" in PLUGINS_SRC


# ---- 级联关闭（2026-09-30 A 线对账 Critical-1）----


def test_aclose_cascades_to_inner_stream():
    """官方姿势 `async with` 同款:外层 aclose 必须把内层原生流一并收尾。

    病灶:包装链此前只关自己的泵任务,内层 MLX 流继续解码到自然完稿=被掐
    回复盗占 GPU(call-9af18da5 双句打断后 TTFT 5.3/6.8s 机理)。"""

    async def _main() -> None:
        inner = _StreamInner(["我哋係顺丰，有个包裹單號尾號七八九零運輸途中唔見咗"])
        stream = inner.chat(chat_ctx=llm.ChatContext())
        guard = _RepeatSelfGuardStream(inner, stream, "无关旧回复")
        await asyncio.sleep(0.1)
        assert not stream._task.done(), "内层流应仍在途(挂起形状)"
        await asyncio.wait_for(guard.aclose(), timeout=5.0)
        assert guard._task.done()
        assert stream._task.done(), "级联关闭:内层原生流任务必须一并收尾"

    asyncio.run(_main())


def test_aclose_cascade_idempotent():
    """重复 aclose(框架收一次、reap 再收一次)不炸。"""

    async def _main() -> None:
        inner = _StreamInner(["你好。"])
        stream = inner.chat(chat_ctx=llm.ChatContext())
        guard = _RepeatSelfGuardStream(inner, stream, "无关")
        await asyncio.sleep(0.1)
        await asyncio.wait_for(guard.aclose(), timeout=5.0)
        await asyncio.wait_for(guard.aclose(), timeout=5.0)
        assert stream._task.done()

    asyncio.run(_main())


def test_cascade_and_prewarm_wiring_source_pins():
    """级联 mixin + 双包装器 _prewarm_impl 透传 + 两线 TTS 收尾接线 pin。"""
    # 三包装流全部挂 mixin
    assert "class _CascadeCloseStreamMixin:" in PLUGINS_SRC
    for _cls in ("_StripTailAnchorStream", "_RepeatSelfGuardStream", "_PartialCaptureStream"):
        assert f"class {_cls}(_CascadeCloseStreamMixin, llm.LLMStream):" in PLUGINS_SRC
    # 官方每通 llm.prewarm() 只认 _prewarm_impl 覆写——双 LLM 包装器透传
    assert PLUGINS_SRC.count("async def _prewarm_impl(self) -> None:") >= 4  # mlx/mt/context/expr
    # A 线/B 线收线 TTS aclose
    assert "await asyncio.wait_for(tts_provider.aclose(), timeout=3.0)" in AGENT_SRC
    assert "tts aclose failed" in AGENT_SRC
    assert "tts aclose failed" in (
        ROOT / "apps" / "agent" / "agent_runtime" / "interpret.py"
    ).read_text(encoding="utf-8")


def test_forward_env_registered():
    """BOK_INTERRUPT_REAP 已立法（prod 封闭 env 面可达）。"""
    assert '"BOK_INTERRUPT_REAP"' in BOK_SRC
