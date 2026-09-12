"""STT 源头热词幻听闸单测(2026-09-09):词表顺串喺 STT 出口丢弃,字幕/轮次/脑全链路不污染。

实机两连回归(call-feaf914c/dd40727c):开场白期间客户没说话,词表整串被解成
FINAL/INTERIM,hook 守卫只能拦脑拦唔到字幕广播——源头闸喺 STT 出口直接丢弃。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from livekit.agents.utils.aio.channel import ChanEmpty  # noqa: E402

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    LanguageState,
    Qwen3ASRSTT,
    _Qwen3ASRLiveStream,
    _is_hotword_vocab_echo,
)

_CTX = "Vocabulary: 單號, 運單, 賠償, 運費, 專員, 集運, 時效, 上門, 追蹤, 核實, WhatsApp, 微信, 顺丰速递"


def _make_stream(hotword_context: str = _CTX) -> _Qwen3ASRLiveStream:
    inner = Qwen3ASRSTT(base_url="http://127.0.0.1:8787", language_state=LanguageState(lang="cantonese"))
    inner._hotword_context = hotword_context
    return _Qwen3ASRLiveStream(inner, vad=object(), conn_options=lp.APIConnectOptions())


def _drain(stream) -> list[str]:
    got = []
    while True:
        try:
            ev = stream._event_ch.recv_nowait()
            got.append(ev.type.name)
        except ChanEmpty:
            return got
        except Exception:
            return got


def test_pure_fn_matches_agent_guard_semantics():
    assert _is_hotword_vocab_echo("單號，運單，賠償，運費，專員，集運，時效，上門，追蹤，核實，WhatsApp，微信，顺丰速递。", _CTX)
    assert _is_hotword_vocab_echo("單號，運單，賠償，運費，專員，集運，時效，上門，追蹤，核實，WhatsApp，微信，顺丰速递", _CTX)
    assert not _is_hotword_vocab_echo("我個單號係三七七八九零", _CTX)
    assert not _is_hotword_vocab_echo("好的", _CTX)


def test_stream_vocab_echo_detector():
    async def scenario():
        stream = _make_stream()
        try:
            # 剥尾保头版统一闸:纯回声→空串,真话原样
            assert stream._echo_filter("單號運單賠償", "stop-mouth") == ""
            assert stream._echo_filter("我個單號係三七七八九零", "stop-mouth") == "我個單號係三七七八九零"
        finally:
            stream._event_ch.close()

    asyncio.run(scenario())


def test_sentence_commit_drops_echo_without_bookkeeping(monkeypatch):
    """幻听句被 _emit_sentence_commit 丢弃:零事件、零记账(_committed_text 不动)。"""
    monkeypatch.delenv("QWEN3_HOTWORD_ECHO_GUARD", raising=False)

    async def scenario():
        stream = _make_stream()
        try:
            stream._emit_sentence_commit(
                "單號，運單，賠償，運費，專員，集運。", 14, "cantonese", 0.0, source="partial-punct"
            )
            assert stream._committed_text == "", "幻听句绝不能进已提交记账"
            assert _drain(stream) == [], "幻听句不能发出任何事件(FINAL/EOS)"
            # 真句子照常提交
            stream._last_sentence_commit_at = -99.0  # 过限速
            stream._emit_sentence_commit("而家到咗邊度呀。", 8, "cantonese", 0.0, source="partial-punct")
            assert stream._committed_text == "而家到咗邊度呀。"
            assert _drain(stream) == ["FINAL_TRANSCRIPT"]
        finally:
            stream._event_ch.close()

    asyncio.run(scenario())


def test_kill_switch_disables_detector(monkeypatch):
    monkeypatch.setenv("QWEN3_HOTWORD_ECHO_GUARD", "0")

    async def scenario():
        stream = _make_stream()
        try:
            assert stream._echo_filter("單號運單賠償", "stop-mouth") == "單號運單賠償", "开关关闭=旧行为(不过滤)"
        finally:
            stream._event_ch.close()

    asyncio.run(scenario())
