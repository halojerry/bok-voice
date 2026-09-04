"""Qwen3-TTS sidecar P4-A/B 回归:纯标点短路 + 流式生成器锁属主线程纪律。

背景(P4 实证):
- B:input='。' 等纯标点进 Qwen3-TTS 会连环 hallucinate 4-30s 爆段(QWEN3_TTS_BYTES
  983040-1530240)。sidecar 短路是 agent 侧过滤之外的兜底。
- A:旧 /v1/audio/speech 流式路径把「持有 _gen_lock 的同步生成器」直接交给
  Starlette 线程池逐 next() 迭代——跳线程 close 会炸
  "cannot release un-acquired lock"(tts.log 实证)并泄漏锁,之后所有合成请求
  永久卡死,agent 语音队列被堵死(下一轮 LLM 永远轮唔到 = en 轮挂死根因)。
  修复后:专职 producer 线程独占生成器生命周期,acquire/release/关闭同线程。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "qwen3-tts-sidecar"))

import app as tts_app  # noqa: E402


def test_sidecar_short_circuits_punctuation_only_chunks(monkeypatch):
    """纯标点/空白输入:synthesize_chunks 零帧、连模型加载都唔触发。"""
    loaded = {"n": 0}

    def _boom():
        loaded["n"] += 1
        raise AssertionError("纯标点输入绝不应该触发模型加载")

    monkeypatch.setattr(tts_app.service, "ensure_loaded", _boom)
    frames = list(
        tts_app.service.synthesize_chunks(text="。！？ ", language="zh", voice="zh-female")
    )
    assert frames == []
    assert loaded["n"] == 0


def test_sidecar_short_circuits_punctuation_only_synthesize(monkeypatch):
    """整段路径同款短路:返回空 PCM、唔加载模型。"""

    def _boom():
        raise AssertionError("纯标点输入绝不应该触发模型加载")

    monkeypatch.setattr(tts_app.service, "ensure_loaded", _boom)
    assert tts_app.service.synthesize(text="。。。", language="zh") == b""


def test_streaming_endpoint_closes_generator_in_producer_thread(monkeypatch):
    """流式端点:生成器 acquire/close 必须同线程(旧实现跳线程 → RLock 泄漏)。

    用带真 RLock 的假生成器模拟 synthesize_chunks(锁跨 yield 持有,与真实现同构):
    - 提前断开消费(模拟打断/挂断时的客户端断开);
    - 断言 close 喺 acquire 的同一根线程发生,且锁可被其他线程重新获取(冇泄漏)。
    """
    lock = threading.RLock()
    thread_ids = {"acquire": None, "close": None}
    released = threading.Event()

    def _fake_chunks(**kwargs):
        # 模拟 _synthesize_mlx_stream 同构:锁横跨所有 yield(acquire 后挂起喺 yield)。
        lock.acquire()
        thread_ids["acquire"] = threading.get_ident()
        try:
            for i in range(100):
                yield (i == 0, b"\x01\x02" * 100)
        finally:
            # close()/GeneratorExit 喺 yield 点抛入:finally 喺【执行 close 的线程】跑。
            thread_ids["close"] = threading.get_ident()
            lock.release()
            released.set()

    monkeypatch.setattr(tts_app.service, "synthesize_chunks", _fake_chunks)

    async def _drive():
        resp = await tts_app.audio_speech(
            {"input": "你好。", "language": "zh", "voice": "zh-female", "streaming": True}
        )
        got = 0
        async for frame in resp.body_iterator:
            # 端点契约:body 必须是裸 PCM bytes(synthesize_chunks 回 (is_first, frame)
            # 元组,端点负责拆包——曾经整包元组 yield 出去炸成 'tuple' has no 'encode')。
            assert isinstance(frame, bytes), type(frame)
            got += 1
            if got >= 2:
                break  # 模拟客户端提前断开
        await resp.body_iterator.aclose()

    asyncio.run(asyncio.wait_for(_drive(), timeout=10))

    # producer 线程异步收尾:等它跑完 finally 再断言(防竞态假红/假绿)。
    assert released.wait(timeout=5), "producer 线程应喺断开后关闭生成器"
    assert thread_ids["acquire"] is not None
    # close 必须与 acquire 同线程(RLock 属主线程纪律);旧实现由 consumer/GC
    # 线程 close → RuntimeError("cannot release un-acquired lock") + 锁泄漏。
    assert thread_ids["close"] == thread_ids["acquire"], thread_ids
    assert released.is_set(), "生成器应喺断开后被 producer 线程收尾关闭"

    # 锁冇泄漏:另一根线程可即时获取(带超时,卡死即失败)。
    acquired: list[bool] = []

    def _try_acquire():
        acquired.append(lock.acquire(timeout=5))
        if acquired[-1]:
            lock.release()

    other = threading.Thread(target=_try_acquire)
    other.start()
    other.join(timeout=10)
    assert acquired == [True], "_gen_lock 断开后必须可被其他线程获取(唔可以有泄漏)"
    assert lock.acquire(timeout=5)
    lock.release()
