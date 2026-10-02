"""流末 flush 的换头复读纵深(P3,2026-10-02)。

病灶:`_RepeatSelfGuardStream._flush_at_end` 只把流末余段(rest)单独比对复读
语料——已早放/被 D1 强制放行的片段(`_released_head`)不拼进来。D1 有界持有
(2026-09-30)放开「repeat-head 冻结攒到 22 字强制早放」后,头部几字不同、余段
相似度不够 0.9 的换头复读在**无句界收流**时整段漏剥(有句界时 `_feed` 的
「片段+句」拼合单元已拦住)。

修复=流末判定复用 `_feed` 同款组合单元:check = `_released_head + rest`。
本文件姿势镜像 tests/test_repeat_guard_head_hold.py(构造与收尾同一事件循环)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from livekit.agents import llm  # noqa: E402

from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    _RepeatSelfGuardStream,
    _is_parrot_sentence,
)

# 上一条回复(归一化后作为复读语料);head 是它的前缀,余段带一个插入字「呢」。
_REPLY = "您这单是三天前寄出的，尾号七八九零，我帮您核对一下物流进度。"
_HEAD_CHUNK = "您这单是三天前寄出的"  # 无句界,恰命中 repeat-head 冻结
_MID_CHUNK = "呢，尾号七八九零"
_TAIL_CHUNK = "，我帮您核对一下物流进度"


class _EndlessInner(llm.LLMStream):
    """只作 _inner 占位(_feed/_flush 直测不需要真事件流)。"""

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


def _run_with_stream(fn, *, last_reply: str = _REPLY, ledger: list[str] | None = None):
    """同一循环内构造→喂料→断言→aclose(LLMStream 任务跨 loop 会炸)。"""

    async def _main():
        inner = _FakeLLM()
        stream = inner.chat(chat_ctx=llm.ChatContext())
        g = _RepeatSelfGuardStream(inner, stream, last_reply, ledger=ledger or [])
        try:
            return fn(g)
        finally:
            await asyncio.wait_for(g.aclose(), timeout=5.0)

    return asyncio.run(_main())


def test_flush_head_swap_rest_alone_would_leak(monkeypatch):
    """先钉病灶形状:余段单独比对**不**命中(旧 flush 会漏),组合单元命中。"""
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "12")

    def _case(g):
        head = g._feed(_HEAD_CHUNK)  # repeat-head 冻结(前缀命中)
        assert head == ""
        mid = g._feed(_MID_CHUNK)  # 攒够 12 字 → D1 强制放行前 6 字
        assert mid != ""
        assert g._released_head == mid
        g._feed(_TAIL_CHUNK)  # 余段无句界,留在 _buf
        rest = g._buf
        # 病灶形状:rest 单独比对不够 0.9(漏剥的根);组合单元够。
        assert not _is_parrot_sentence(rest, _REPLY), "余段单判不中=旧 flush 漏剥现场"
        assert _is_parrot_sentence(g._released_head + rest, _REPLY)

    assert _run_with_stream(_case) is None  # 断言全在 _case 内


def test_flush_head_swap_tail_suppressed(monkeypatch, capsys):
    """流末换头残段必须被组合单元剥除(音频不出声、账本无残留)。"""
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "12")

    def _case(g):
        assert g._feed(_HEAD_CHUNK) == ""
        head = g._feed(_MID_CHUNK)
        assert head != ""
        assert g._feed(_TAIL_CHUNK) == ""  # 无句界,全部扣在缓冲
        tail = g._flush_at_end()
        assert tail == "", "流末换头残段漏剥=复读主体再次出声"
        assert g._released_head == "", "命中后片段账本应清"
        return g._cross_suppressed

    suppressed = _run_with_stream(_case)
    assert suppressed == 0
    assert "REPEAT_SELF_SUPPRESSED" in capsys.readouterr().out


def test_flush_without_head_zero_drift(monkeypatch):
    """无已放片段时流末行为不变:非复读余段原样输出(旧路径零漂移)。"""
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")

    def _case(g):
        g._feed("今日天氣唔錯我哋傾下包裹")  # 无句界、非复读 → 早放 6 字
        g._feed("安排同跟進時間")
        tail = g._flush_at_end()
        assert tail == "我哋傾下包裹安排同跟進時間", "无片段在账=不拼合,照旧放行"
        assert tail.endswith("安排同跟進時間")

    _run_with_stream(_case)


def test_flush_with_head_but_new_tail_passes(monkeypatch):
    """有已放片段但余段是新内容:不得误剥(拼合单元只防复读,不当连坐)。"""
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "12")

    def _case(g):
        assert g._feed(_HEAD_CHUNK) == ""
        assert g._feed(_MID_CHUNK) != ""  # D1 早放片段
        g._feed("，我哋安排專員上門同你核對")  # 新内容余段(无句界)
        tail = g._flush_at_end()
        assert "安排專員上門" in tail, "新内容不得被拼合单元误剥"
        assert g._released_head == ""

    _run_with_stream(_case)


def test_flush_cross_turn_head_swap_suppressed(monkeypatch, capsys):
    """跨轮账本路径同样吃组合单元(last_reply 空,ledger 命中)。"""
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")
    monkeypatch.setenv("BOK_REPEAT_HEAD_MAX_HOLD", "12")

    def _case(g):
        assert g._feed(_HEAD_CHUNK) == ""
        assert g._feed(_MID_CHUNK) != ""
        g._feed(_TAIL_CHUNK)
        tail = g._flush_at_end()
        assert tail == "", "跨轮换头残段同样剥"
        return g._cross_suppressed

    suppressed = _run_with_stream(_case, last_reply="", ledger=[_REPLY])
    assert suppressed >= 1
    assert "REPEAT_CROSS_TURN_SUPPRESSED" in capsys.readouterr().out


def test_flush_reuses_combined_unit_source_pin():
    """源级 pin:flush 判定必须与 _feed(1966)同款组合单元(删掉即红)。"""
    src = (
        ROOT / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
    ).read_text(encoding="utf-8")
    i_cls = src.index("class _RepeatSelfGuardStream")
    i_flush = src.index("def _flush_at_end", i_cls)
    body = src[i_flush : i_flush + 1400]
    assert 'check_unit = (self._released_head or "") + rest' in body
    assert "_is_parrot_sentence(check_unit, self._last_reply)" in body
    assert "self._is_cross_turn(check_unit)" in body
    # 旧形状(单独判 rest)必须绝迹
    assert "_is_parrot_sentence(rest, self._last_reply)" not in body
