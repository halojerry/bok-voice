"""MiniMax bidi 首 chunk 提前切单测（2026-09-28，fake WS + 纯函数，无网络）。

覆盖：
- ``_tts_first_chunk_chars``:默认 6（2026-09-30 耳测定档，原 10）/ "0" 关 / 坏值回默认 / 负值关；
- ``_first_chunk_cut``:句内 ≥N 返回安全切点、不足 N 返回 None、句末标点让位
  None、IRON GUARD 数字串（中文数字/ASCII/混排）与拉丁词 run 不被拦腰切；
- 流级:首 continue 句内提前发、后续 continue 按句界对齐、env "0" 逐字节回旧、
  每条回复（每个流）"first" 重置；
- 源级 pin:``BOK_TTS_FIRST_CHUNK_CHARS`` 进 ``tools/bok.py`` ``_FORWARD_ENV``。

harness 镜像 tests/test_minimax_bidi.py（_FakeWS/_QueueWS/_FakeConnect）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import (  # noqa: E402
    MiniMaxTTS,
    _first_chunk_cut,
    _tts_first_chunk_chars,
)


class _FakeWS:
    """鸭型 WS：recv 按脚本回消息；send 记录；脚本耗尽后挂起等 cancel。"""

    def __init__(self, script: list[str] | None = None):
        self._script = list(script or [])
        self.sent: list[dict] = []
        self.pings = 0
        self.closed = False

    async def recv(self):
        if self._script:
            return self._script.pop(0)
        await asyncio.Event().wait()

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def ping(self):
        self.pings += 1

    async def close(self):
        self.closed = True


class _QueueWS:
    """测试侧可随时注入服务端消息的 fake WS（流级时序控制）。"""

    def __init__(self):
        self.sent: list[dict] = []
        self.pings = 0
        self.closed = False
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self.server_push(_CONNECTED)
        self.server_push(_STARTED)

    def server_push(self, raw: str) -> None:
        self._q.put_nowait(raw)

    async def recv(self):
        return await self._q.get()

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def ping(self):
        self.pings += 1

    async def close(self):
        self.closed = True


class _FakeConnect:
    """排队发 fake WS；记录 connect 次数。"""

    def __init__(self, sockets: list[_FakeWS | _QueueWS]):
        self._sockets = list(sockets)
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        return self._sockets.pop(0)


_CONNECTED = '{"event": "connected_success"}'
_STARTED = '{"event": "task_started"}'
_FLUSHED = '{"event": "task_flushed"}'
_CANCELED = '{"event": "task_canceled"}'
# 2000 字节 PCM：≥首推门槛 frame_bytes//5(=1920 @24k)，触发首包 PERF 打点。
_AUDIO = '{"data": {"audio": "' + "00" * 2000 + '"}}'


@pytest.fixture(autouse=True)
def _bidi_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    monkeypatch.delenv("MINIMAX_WS_URL", raising=False)
    monkeypatch.setenv("MINIMAX_REGION", "cn")
    monkeypatch.setenv("MINIMAX_LANGUAGE_BOOST", "")
    monkeypatch.setenv("MINIMAX_PAUSE", "0")
    # 测试腿无需看门狗/长 cancel 等待，剔除无关计时。
    monkeypatch.setenv("MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S", "0")
    monkeypatch.setenv("MINIMAX_BIDI_CANCEL_WAIT_S", "0.2")
    monkeypatch.delenv("BOK_TTS_FIRST_CHUNK_CHARS", raising=False)
    yield


def _make_tts():
    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse", "cantonese": "Cantonese_crisp_news_anchor_vv2"},
        sample_rate=24000,
        api_key="test" + "-key",
    )


async def _wait_for(pred, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def _continue_texts(ws) -> list[str]:
    return [m.get("text") for m in ws.sent if m.get("event") == "task_continue"]


# ---- 配置读取 ----

def test_first_chunk_chars_default():
    # 2026-09-30 Ethan 耳测定档（AB 三臂 MiniMax 实声）：10→6。
    assert _tts_first_chunk_chars() == 6


def test_first_chunk_chars_zero_disables(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "0")
    assert _tts_first_chunk_chars() == 0


def test_first_chunk_chars_negative_disables(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "-3")
    assert _tts_first_chunk_chars() == 0


def test_first_chunk_chars_override(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "7")
    assert _tts_first_chunk_chars() == 7


def test_first_chunk_chars_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "abc")
    assert _tts_first_chunk_chars() == 6


# ---- 纯函数 _first_chunk_cut ----

def test_cut_mid_sentence_at_or_above_n():
    text = "我而家即刻幫你查下單號先"  # 12 字，无标点
    assert _first_chunk_cut(text, 10) == 10
    assert _first_chunk_cut(text, 10) is not None


def test_cut_below_n_returns_none():
    assert _first_chunk_cut("你好", 10) is None


def test_cut_sentence_end_before_n_returns_none():
    # 句末标点在场 → 让位既有按句路径（旧行为不变），helper 返回 None。
    assert _first_chunk_cut("你好。", 10) is None


def test_cut_far_sentence_end_does_not_block_early_cut():
    # 2026-09-28 A/B 实弹勘误:句界在 58 字远处不得压制 10 字早切——早切本就
    # 为长首句而生;只有句界落在 N+6 容差内才让位换自然断点。
    text = "好我哋即刻提交資料專員會加你收截圖核實完之後就會通知你跟進。"
    assert len(text) > 20
    cut = _first_chunk_cut(text, 10)
    assert cut is not None and cut <= 12  # 早切在场,切点在 N 附近


def test_cut_sentence_end_within_tolerance_returns_none():
    # 句界落在 N..N+6 容差内 → 多等几个字换自然断点(韵律优先)。
    head = "我哋即刻幫你"  # 6 字
    text = head + "核實。跟進"  # 句界在第 9 字(<10+6),让位
    assert _first_chunk_cut(text, 10) is None


def test_cut_off_returns_none():
    assert _first_chunk_cut("我而家即刻幫你查下單號先", 0) is None
    assert _first_chunk_cut("我而家即刻幫你查下單號先", -1) is None


def test_cut_digit_guard_chinese_numeral_run():
    text = "你的單號係六四三一一三三請記低"  # 中文数字 run: 六四三一一三三
    cut = _first_chunk_cut(text, 10)
    assert cut is not None
    run = "六四三一一三三"
    assert run in text[:cut], f"数字 run 被拦腰截断: {text[:cut]!r}"
    assert not (text[cut - 1] in run and text[cut:cut + 1] in run)


def test_cut_digit_guard_ascii_run():
    text = "訂單號是 88291047 盡快"
    cut = _first_chunk_cut(text, 10)
    assert cut is not None
    assert "88291047" in text[:cut], f"ASCII 数字 run 被截断: {text[:cut]!r}"


def test_cut_digit_guard_mixed_run():
    text = "客戶C3四五六七八九號"
    cut = _first_chunk_cut(text, 5)
    assert cut is not None
    assert "C3四五六七八九" in text[:cut], f"混排 run 被截断: {text[:cut]!r}"


def test_cut_latin_guard_whatsapp():
    text = "我加你WhatsApp先啦唔該晒你"
    cut = _first_chunk_cut(text, 10)
    assert cut is not None
    assert "WhatsApp" in text[:cut], f"拉丁词被拦腰截断: {text[:cut]!r}"


def test_cut_never_returns_index_past_end():
    # run 延伸到串尾 → 切点无剩余 = 等于没切 → None（整段原样走旧路径）。
    assert _first_chunk_cut("8829104788", 10) is None


# ---- 流级：首 continue 提前切 ----

def test_stream_first_chunk_flushes_early(monkeypatch, capsys):
    """长首块（无句号）≥N 字 → 首个 task_continue 就是提前切段，未必等句界。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()
    full = "你的單號係六四三一一三三請記低"

    async def run():
        s = tts.stream()
        s.push_text(full)
        ok = await _wait_for(lambda: len(_continue_texts(ws)) >= 1)
        assert ok, f"首个 continue 没发出: {_continue_texts(ws)}"
        # 首块切点：数字 run 整段同行。
        assert _continue_texts(ws)[0] == "你的單號係六四三一一三三"
        # 喂音频触发首包 PERF 打点（读 flush reason）。
        ws.server_push(_AUDIO)
        await _wait_for(lambda: "first_chunk=early" in capsys.readouterr().out, timeout=3)
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_stream_first_chunk_disabled_is_old_behavior(monkeypatch):
    """env "0" → 首块不提前切：逐块原样透传（整段一条 continue）。"""
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "0")
    ws = _FakeWS([_CONNECTED, _STARTED, _CANCELED])
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()
    full = "你的單號係六四三一一三三請記低"

    async def run():
        s = tts.stream()
        s.push_text(full)
        ok = await _wait_for(lambda: len(_continue_texts(ws)) >= 1)
        assert ok, f"未发出 continue: {_continue_texts(ws)}"
        await asyncio.sleep(0.1)
        assert _continue_texts(ws) == [full], _continue_texts(ws)
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_stream_second_continue_waits_for_sentence(monkeypatch):
    """首块提前切后，尾块攒到句末标点才发（后续 continue 按句界对齐）。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你的單號係六四三一一三三請記低")  # 切 12 + 尾「請記低」
        ok = await _wait_for(lambda: len(_continue_texts(ws)) == 1)
        assert ok, _continue_texts(ws)
        assert _continue_texts(ws)[0] == "你的單號係六四三一一三三"
        s.push_text("，專員會盡快跟進")  # 无句界 → 仍攒住不发
        await asyncio.sleep(0.15)
        assert len(_continue_texts(ws)) == 1, f"尾块不应提前发: {_continue_texts(ws)}"
        s.push_text("。")  # 句界到 → 尾块一次发
        ok = await _wait_for(lambda: len(_continue_texts(ws)) == 2)
        assert ok, f"尾块句界未发: {_continue_texts(ws)}"
        assert _continue_texts(ws)[1] == "請記低，專員會盡快跟進。"
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_stream_first_chunk_resets_per_reply(monkeypatch):
    """同一条 bidi 会话：第二条回复（第二个流）重新享有首块提前切。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()
    full = "你的單號係六四三一一三三請記低"
    prefix = "你的單號係六四三一一三三"

    async def run():
        s1 = tts.stream()
        s1.push_text(full)
        assert await _wait_for(lambda: len(_continue_texts(ws)) >= 1), _continue_texts(ws)
        assert _continue_texts(ws)[0] == prefix
        ws.server_push(_AUDIO)  # 需先出首帧，AudioEmitter 才 start（否则 end_input 抛）
        await asyncio.sleep(0.1)
        s1.end_input()
        ws.server_push(_FLUSHED)
        assert await _wait_for(lambda: s1._task.done(), timeout=10), "流 1 未收尾"
        async for _ev in s1:
            pass

        s2 = tts.stream()
        s2.push_text(full)
        ok = await _wait_for(lambda: len(_continue_texts(ws)) >= 3)
        assert ok, f"第二条回复未重新提前切: {_continue_texts(ws)}"
        assert _continue_texts(ws)[2] == prefix, _continue_texts(ws)
        s2._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=15))


# ---- 源级 pin ----

def test_forward_env_registers_first_chunk(tmp_path, monkeypatch):
    """D14 纪律：BOK_TTS_FIRST_CHUNK_CHARS 必须进 _FORWARD_ENV（prod 可达）。"""
    monkeypatch.delenv("BOK_TTS_FIRST_CHUNK_CHARS", raising=False)
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    import tools.bok as bok  # noqa: E402

    assert "BOK_TTS_FIRST_CHUNK_CHARS" in bok._FORWARD_ENV


# ---- _RepeatSelfGuardStream 首片段早放（LLM→TTS 句缓冲层的同一早发） ----


class _GuardPlugin(lp.llm.LLM):
    """guard stream 构造用的最小 plugin 替身。"""

    def chat(self, *, chat_ctx, **kw):  # pragma: no cover - 不经此路
        raise AssertionError("not driven via chat")


class _GuardInner(lp.llm.LLMStream):
    def __init__(self, plugin):
        from livekit.agents.types import APIConnectOptions

        super().__init__(
            llm=plugin, chat_ctx=lp.llm.ChatContext(), tools=[],
            conn_options=APIConnectOptions(),
        )

    async def _metrics_monitor_task(self, event_aiter) -> None:  # pragma: no cover
        async for _ in event_aiter:
            pass

    async def _run(self):  # pragma: no cover - guard 测试不经内芯
        return None


def _guard(last_reply: str, *, ledger=None):
    return lp._RepeatSelfGuardStream(
        _GuardPlugin(), _GuardInner(_GuardPlugin()), last_reply,
        ledger=ledger,
    )


def _guard_io(build_and_feed):
    """LLMStream 构造需要活 loop:每个用例的构造+喂料在 asyncio.run 内跑。"""
    async def _inner():
        return build_and_feed()
    return asyncio.run(_inner())


def test_guard_first_fragment_released_early(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "10")

    def _io():
        g = _guard("完全无关的上一条回复内容。")
        out = g._feed("我而家即刻幫你查下單號先慢慢嚟")
        return out, g._buf, g._first_sent

    out, buf, first = _guard_io(_io)
    assert out == "我而家即刻幫你查下單號先"[:10]  # 首 10 字早放
    assert buf == "號先慢慢嚟"
    assert first is True


def test_guard_fragment_prefix_of_repeat_held(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "10")
    last = "我而家即刻幫你查下單號先慢慢嚟。跟進要兩日。"

    def _io():
        g = _guard(last)
        return g._feed("我而家即刻幫你查下單號先慢慢嚟。"), g._first_sent

    out, first = _guard_io(_io)
    # 片段与 last_reply 前缀命中 → 扣住等整句判定(复读句头 10 字不漏播)。
    assert out == ""
    assert first is False


def test_guard_first_sent_via_boundary_no_second_early(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "10")

    def _io():
        g = _guard("完全无关的上一条回复内容。")
        a = g._feed("好。")
        b = g._feed("跟住我哋慢慢睇下後續處理進度好唔好")
        return a, b

    a, b = _guard_io(_io)
    assert a == "好。"
    # 首段已放(句界路径);后续长无标点段不再早放(只动首段)。
    assert b == ""


def test_guard_remainder_parrot_still_caught(monkeypatch):
    """换头复读两道防线:①片段尾交叠命中→扣住;②真早放后拼合判定剥余段。"""
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "10")
    last = "重點內容係呢一句要複述。"

    # ①流式形态(句界未到先攒字):片段尾「重點內容係呢」6 字窗交叠命中语料
    # 前缀 → 扣住不放(换头复读主体不早播)。
    def _io_hold():
        g = _guard(last)
        a = g._feed("而家馬上講重點內容係呢一句要複")  # 无句界,攒到 10 字
        return a, g._first_sent

    out, first = _guard_io(_io_hold)
    assert out == "" and first is False

    # ②纵深防御(拼合判定):早放片段与语料零交叠(①接不住的分布形态)时,
    # 句界轮以「片段+句」拼合单元判定;命中→只剥余段,已放片段不回收。
    # 直接驱动 _released_head 状态隔离测拼合机制(①会先扣住高相似复读,
    # 端到端到不了这条——纵深层独立钉)。语料 45 字:拼合(9+45 vs 45)
    # 相似度 0.909 才过 0.9 阈值(分母係双侧长度,短料上不了线)。
    _pool = "子丑寅卯辰巳午未申酉戌亥金木水火土山河湖海日月星雲雷電風雪霜露橋船車馬牛羊雞犬豬鼠兔虎蛇猴"
    _body = _pool[:45]
    assert len(_body) == 45

    def _io_prepend():
        g = _guard(_body + "。")
        g._released_head = "而家講真呢句好重要"  # 模拟已早放的无关片段
        return g._feed(_body + "。")

    assert _guard_io(_io_prepend) == ""  # 拼合=复读 → 余段剥除(REPEAT_SELF_SUPPRESSED)


def test_guard_early_off_env_zero(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "0")

    def _io():
        g = _guard("完全无关的上一条回复内容。")
        return g._feed("我而家即刻幫你查下單號先慢慢嚟")

    assert _guard_io(_io) == ""  # 旧行为:攒句界


def test_guard_turn1_bypass_passthrough(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "10")

    def _io():
        g = _guard("")  # turn-1 无 last_reply → bypass
        return g._feed("我而家即刻幫你查下")

    assert _guard_io(_io) == "我而家即刻幫你查下"


def test_guard_digit_run_not_split_in_fragment(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "6")

    def _io():
        g = _guard("完全无关的上一条回复内容。")
        return g._feed("你的單號係六四三一一三三請記低")

    assert _guard_io(_io) == "你的單號係六四三一一三三"  # 数字 run 整段同行,切点延伸过 run
