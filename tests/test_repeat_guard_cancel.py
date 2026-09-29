"""guard cancel 缓冲不蒸发（P2.a，spec 2026-09-29 v2 §5）。

病灶（call-ed6aa9b8 三轮实证）：barge-in / watchdog force-interrupt cancel
SpeechHandle 时，`_RepeatSelfGuardStream._run` 的 CancelledError 直接 re-raise，
`_buf` 攒着的未播文本随协程蒸发——turns 账本只看到 tee 捕到的部分
（`interrupted reply ledgered chars=10` 三轮全灭），证据丢失。

修复契约：
- `pending_buffer` 属性（property → self._buf）：cancel 后仍可读残留缓冲；
- `_run` 的 CancelledError 分支：缓冲非空打 `REPEAT_GUARD_CANCEL_DROP chars=N`
  后 re-raise（行为零变化，只加可见性+可读性）；
- 正常流走完：pending_buffer 为空（_flush_at_end 已清）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

import pytest  # noqa: E402
from livekit.agents import llm  # noqa: E402

from agent_runtime.providers.livekit_plugins import _RepeatSelfGuardStream  # noqa: E402


class _HangingInnerStream(llm.LLMStream):
    """发若干无句界 chunk 后挂起（复刻慢速 LLM 流被 barge-in 打断的形状）。"""

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
        await self._hold.wait()  # 挂住（模拟 LLM 流未结束）


class _StreamInner(llm.LLM):
    provider = "fake-provider"  # LLMStream metrics 事件读 _llm.provider

    def __init__(self, chunks: list[str]):
        super().__init__()
        self._chunks = chunks

    def chat(self, *, chat_ctx, **kw):
        return _HangingInnerStream(self, self._chunks)


async def _drive(chunks: list[str], last_reply: str, soak_s: float = 0.2) -> _RepeatSelfGuardStream:
    """事件循环内构造（LLMStream.__init__ 自动 spawn _main_task 跑 _run）→
    soak → cancel 框架起的 task（复刻 barge-in 对 speech 管道的取消路径）。"""
    inner = _StreamInner(chunks)
    stream = inner.chat(chat_ctx=llm.ChatContext())
    guard = _RepeatSelfGuardStream(inner, stream, last_reply)  # plugin=LLM 实例，inner=流
    await asyncio.sleep(soak_s)  # 让 chunk 进缓冲
    guard._task.cancel()  # 框架 _main_task（驱动 guard._run 的本体）
    with pytest.raises((asyncio.CancelledError, Exception)):
        await guard._task
    return guard


def test_cancel_preserves_pending_buffer_and_logs(capsys):
    """流中途 cancel：缓冲进 pending_buffer + 打点；不再静默蒸发。"""

    async def _main() -> _RepeatSelfGuardStream:
        return await _drive(
            ["我哋係顺丰，有个包裹單號尾號七八九零運輸途中唔見咗"], last_reply="无关旧回复"
        )

    guard = asyncio.run(_main())
    assert guard.pending_buffer  # 缓冲可读（账本证据不丢）
    assert "REPEAT_GUARD_CANCEL_DROP chars=" in capsys.readouterr().out


def test_cancel_with_empty_buffer_no_log(capsys):
    """cancel 时缓冲已空（全部放行过）→ 不打点（零噪声）。"""

    async def _main() -> _RepeatSelfGuardStream:
        return await _drive(["你好。"], last_reply="无关")  # 带句界 → 已放行，_buf 空

    guard = asyncio.run(_main())
    assert guard.pending_buffer == ""
    assert "REPEAT_GUARD_CANCEL_DROP" not in capsys.readouterr().out


def test_pending_buffer_is_property_of_buf():
    """pending_buffer 是 _buf 的 property（零维护成本，读写同源）——不构造流
    （LLMStream 构造需 loop），直接验证属性语义用类探针最小替身。"""

    class _Bare:
        pending_buffer = _RepeatSelfGuardStream.pending_buffer

    b = _Bare()
    b._buf = "残留文本"
    assert b.pending_buffer == "残留文本"
