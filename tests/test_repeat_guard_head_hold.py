"""D1 终修·guard repeat-head 有界持有（2026-09-30）。

病理（call-3b776663 + 25 天 24 例零 push）：冻结命中的首段在无句界长首句下
可无限期扣住——官方链路 ``_produce_segments`` 只在非空 chunk 才起 TTS 段
（agent_activity.py:3576-3587），空文本=零 push_text+零 TTS 任务+框架干净
完稿，死寂直到 watchdog 6-8s 强断。

修复契约：冻结攒到 ``BOK_REPEAT_HEAD_MAX_HOLD``（默认 22 字）强制经同一
``_first_chunk_cut`` 放行（数字/拉丁 run 铁闸复用）；换头复读主体仍由
「片段+句」拼合纵深在下一句界剥除；env=0 无界旧行为逐字节回退。

测试形态：guard 构造与收尾必须同一事件循环（LLMStream.__init__ 起
pump/metrics 任务，跨 loop aclose 会炸）——每用例单 ``asyncio.run`` 内
完成「构造→喂料→断言数据采集→aclose」，断言在同步面做。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

import asyncio  # noqa: E402

from livekit.agents import llm  # noqa: E402

from _bok_src import bok_source  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    _RepeatSelfGuardStream,
    _repeat_head_max_hold,
)

PLUGINS_SRC = (
    ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
).read_text(encoding="utf-8")
BOK_SRC = bok_source()


class _EndlessInner(llm.LLMStream):
    """只作 _inner 占位（_feed 直测不需要真事件流）。"""

    def __init__(self, plugin):
        from livekit.agents.types import APIConnectOptions

        super().__init__(
            llm=plugin, chat_ctx=llm.ChatContext(), tools=[], conn_options=APIConnectOptions()
        )

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:
            pass

    async def _run(self):
        await asyncio.Event().wait()


class _FakeLLM(llm.LLM):
    provider = "fake-provider"

    def chat(self, *, chat_ctx, **kw):
        return _EndlessInner(self)


# 冻结语料：片段与上一条回复同头（前缀命中 → repeat-head 冻结）。
_REPLY_HEAD = "唔係我係新龍快遞嘅客服專員"
_REPLY = _REPLY_HEAD + "你個包裹已經到咗倉"  # 恰 22 字


def test_repeat_head_max_hold_default_and_env(monkeypatch):
    """默认 22；env 可调；配错回 22；0=显式无界。"""
    monkeypatch.delenv("BOK_REPEAT_HEAD_MAX_HOLD", raising=False)
    assert _repeat_head_max_hold() == 22
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "30")
    assert _repeat_head_max_hold() == 30
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "0")
    assert _repeat_head_max_hold() == 0
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "garbage")
    assert _repeat_head_max_hold() == 22
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "-5")
    assert _repeat_head_max_hold() == 22


def test_freeze_below_threshold_holds(monkeypatch):
    """阈值内照冻：repeat-head 命中 + 无句界 → 不放行（原行为）。"""

    async def _main() -> tuple[str, bool]:
        inner = _FakeLLM()
        stream = inner.chat(chat_ctx=llm.ChatContext())
        g = _RepeatSelfGuardStream(inner, stream, _REPLY)
        try:
            return g._feed(_REPLY_HEAD[:10]), g._first_sent
        finally:
            await asyncio.wait_for(g.aclose(), timeout=5.0)

    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "22")
    out, first_sent = asyncio.run(_main())
    assert out == ""
    assert first_sent is False


def test_freeze_force_releases_at_threshold(monkeypatch, capsys):
    """攒到 22 字强制放行首切点：出声优先于完美防复读（零句死 > 6 字险）。"""

    async def _main() -> tuple[str, bool, str]:
        inner = _FakeLLM()
        stream = inner.chat(chat_ctx=llm.ChatContext())
        g = _RepeatSelfGuardStream(inner, stream, _REPLY)
        try:
            # 无句界长首句：repeat-head 命中 + buf >= 22 → 强制经 _first_chunk_cut 放行
            text = _REPLY_HEAD + "而家幫你跟進緊單據狀態"  # 24 字,无句界
            out = g._feed(text)
            return out, g._first_sent, g._released_head
        finally:
            await asyncio.wait_for(g.aclose(), timeout=5.0)

    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "22")
    out, first_sent, released = asyncio.run(_main())
    assert out != "", "达阈值必须放行——这是零句死的修复面"
    assert first_sent is True
    assert released == out
    assert "REPEAT_GUARD_HEAD_FORCE_RELEASE chars=" in capsys.readouterr().out


def test_killswitch_zero_restores_unbounded(monkeypatch, capsys):
    """env=0：无界旧行为逐字节回退（多长都冻，等句界/流尾）。"""

    async def _main() -> tuple[str, bool]:
        inner = _FakeLLM()
        stream = inner.chat(chat_ctx=llm.ChatContext())
        g = _RepeatSelfGuardStream(inner, stream, _REPLY)
        try:
            text = _REPLY_HEAD + "而家幫你跟進緊單據狀態仲有兩日就到"
            return g._feed(text), g._first_sent
        finally:
            await asyncio.wait_for(g.aclose(), timeout=5.0)

    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "0")
    out, first_sent = asyncio.run(_main())
    assert out == ""
    assert first_sent is False
    assert "REPEAT_GUARD_HEAD_FORCE_RELEASE" not in capsys.readouterr().out


def test_forced_head_keeps_combined_depth_defense(monkeypatch, capsys):
    """强制放行后拼合纵深仍在：下一句界以「片段+句」判复读，主体剥除。"""

    async def _main() -> tuple[str, str]:
        inner = _FakeLLM()
        stream = inner.chat(chat_ctx=llm.ChatContext())
        g = _RepeatSelfGuardStream(inner, stream, _REPLY)
        try:
            # 1) 整条回复开头 22 字（无句界）→ 冻结达阈值强制放行前 6 字
            head = g._feed(_REPLY)
            # 2) 余段到句界：片段+句 = 上一条回复原文 → 拼合单元判复读，整句剥除
            out2 = g._feed("。")
            return head, out2
        finally:
            await asyncio.wait_for(g.aclose(), timeout=5.0)

    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "22")
    head, out2 = asyncio.run(_main())
    assert head != ""
    assert head == _REPLY[:6]
    assert out2 == "", "拼合单元命中复读必须剥余段"
    captured = capsys.readouterr().out
    assert "REPEAT_GUARD_HEAD_FORCE_RELEASE" in captured
    assert "REPEAT_SELF_SUPPRESSED" in captured


def test_normal_head_release_untouched(monkeypatch):
    """非复读头：正常早切路径零变化（回归面）。"""

    async def _main() -> tuple[str, bool]:
        inner = _FakeLLM()
        stream = inner.chat(chat_ctx=llm.ChatContext())
        g = _RepeatSelfGuardStream(inner, stream, "完全无关的上一条回复内容")
        try:
            return g._feed("今日天氣真好我哋傾下包裹安排"), g._first_sent
        finally:
            await asyncio.wait_for(g.aclose(), timeout=5.0)

    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")
    out, first_sent = asyncio.run(_main())
    assert out != ""
    assert first_sent is True


def test_wiring_source_pins():
    """接线源级 pin：有界持有 + 观测行 + env 立法。"""
    assert "BOK_REPEAT_HEAD_MAX_HOLD" in PLUGINS_SRC
    assert "REPEAT_GUARD_HEAD_FORCE_RELEASE chars=" in PLUGINS_SRC
    assert "REPEAT_GUARD_CANCEL_DROP chars={len(self._buf)} first_sent=" in PLUGINS_SRC
    assert '"BOK_REPEAT_HEAD_MAX_HOLD"' in BOK_SRC
