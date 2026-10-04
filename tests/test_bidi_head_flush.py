"""bidi 头段催产 task_flush 单测（2026-09-29，fake WS，无网络）。

官方文档+直连台架（scripts/bench/bench_minimax_bidi.py）定案：服务端对无句末标点的
缓冲不起合成（兜底窗 ~2.4s），早切头段必须 task_flush 催一声才出声——台架
首声 918-962→210-343ms。本文件钉死：

- 早切头段后立刻发 task_flush（默认开；``MINIMAX_BIDI_HEAD_FLUSH=0`` 回退=
  只早发不催产=第八波原行为）；
- 头段催产的 ack（task_flushed）**不是收尾信号**：不置 _flushed_evt、recv
  不进 0.5s 收摊窗，余句照常合成；
- 流已尽（头段=整条回复）时，头段 ack 兼任收尾 ack（不悬挂 15s）；
- env 关 / 无早切（整句）时零 task_flush；
- 源级 pin:``MINIMAX_BIDI_HEAD_FLUSH`` 进 ``tools/bok.py`` ``_FORWARD_ENV``。

harness 镜像 tests/test_tts_first_chunk.py。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers.livekit_plugins import MiniMaxTTS  # noqa: E402


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
    def __init__(self, sockets):
        self._sockets = list(sockets)
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        return self._sockets.pop(0)


_CONNECTED = '{"event": "connected_success"}'
_STARTED = '{"event": "task_started"}'
_FLUSHED = '{"event": "task_flushed"}'
_AUDIO = '{"data": {"audio": "' + "00" * 2000 + '"}}'

# 默认 N=6(2026-09-30 耳测定档):「你的單號係六四三一一三三」数字 run 整段=12 字
# 早切头段,「請記低」留尾块。
_HEAD = "你的單號係六四三一一三三"


@pytest.fixture(autouse=True)
def _bidi_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    monkeypatch.delenv("MINIMAX_WS_URL", raising=False)
    monkeypatch.setenv("MINIMAX_REGION", "cn")
    monkeypatch.setenv("MINIMAX_LANGUAGE_BOOST", "")
    monkeypatch.setenv("MINIMAX_PAUSE", "0")
    monkeypatch.setenv("MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S", "0")
    monkeypatch.setenv("MINIMAX_BIDI_CANCEL_WAIT_S", "0.2")
    monkeypatch.delenv("BOK_TTS_FIRST_CHUNK_CHARS", raising=False)
    monkeypatch.delenv("MINIMAX_BIDI_HEAD_FLUSH", raising=False)
    # 假 key(随机值,非凭据):插件凭据门要求非空,fake WS 不校验真伪
    monkeypatch.setenv("BOK_TEST_TTS_KEY", uuid.uuid4().hex)
    yield


def _make_tts():
    # 凭据从环境读(fake WS 不校验 key,测试无需真值)
    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse", "cantonese": "Cantonese_crisp_news_anchor_vv2"},
        sample_rate=24000,
        api_key=os.environ.get("BOK_TEST_TTS_KEY", ""),
    )


async def _wait_for(pred, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def _events(ws, name: str) -> list[dict]:
    return [m for m in ws.sent if m.get("event") == name]


def _continue_texts(ws) -> list[str]:
    return [m.get("text") for m in ws.sent if m.get("event") == "task_continue"]


def test_head_flush_sent_right_after_early_cut(monkeypatch):
    """早切头段 → 紧跟一条 task_flush(顺序:continue 头段 → flush)。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text(_HEAD + "請記低")
        ok = await _wait_for(lambda: len(_events(ws, "task_flush")) >= 1)
        assert ok, f"头段催产 flush 未发: {ws.sent}"
        idx_continue = next(
            i for i, m in enumerate(ws.sent) if m.get("event") == "task_continue"
        )
        idx_flush = next(i for i, m in enumerate(ws.sent) if m.get("event") == "task_flush")
        assert idx_flush == idx_continue + 1, "flush 必须紧贴头段 continue"
        assert _continue_texts(ws)[0] == _HEAD
        ws.server_push(_AUDIO)
        await asyncio.sleep(0.1)
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_head_flush_disabled_by_env(monkeypatch):
    """MINIMAX_BIDI_HEAD_FLUSH=0 → 只早发不催产(第八波原行为,零 task_flush)。"""
    monkeypatch.setenv("MINIMAX_BIDI_HEAD_FLUSH", "0")
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text(_HEAD + "請記低")
        ok = await _wait_for(lambda: len(_continue_texts(ws)) >= 1)
        assert ok, _continue_texts(ws)
        await asyncio.sleep(0.15)
        assert _events(ws, "task_flush") == [], "关闸后头段不得催产"
        ws.server_push(_AUDIO)
        await asyncio.sleep(0.1)
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_head_flush_ack_does_not_end_stream(monkeypatch):
    """头段催产 ack(空 buffer flush)不是收尾:不置 _flushed_evt,余句照常发。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text(_HEAD + "請記低")
        assert await _wait_for(lambda: len(_events(ws, "task_flush")) >= 1)
        ws.server_push(_FLUSHED)  # 头段 ack
        ws.server_push(_AUDIO)  # 头段音频(首帧)
        await asyncio.sleep(0.1)
        assert not s._flushed_evt.is_set(), "头段 ack 不得收摊"
        # 余句到句界 → 第二条 continue 照发(recv 未收摊、输入循环活着)
        s.push_text("，專員會盡快跟進。")
        ok = await _wait_for(lambda: len(_continue_texts(ws)) >= 2)
        assert ok, f"余句未发(流被头段 ack 误杀?): {_continue_texts(ws)}"
        assert _continue_texts(ws)[1] == "請記低，專員會盡快跟進。"
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_head_only_reply_head_ack_completes_turn(monkeypatch):
    """头段=整条回复:流尽后的头段 ack 兼任收尾 ack,不悬挂等第二发。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        # 早切头段+无句界尾块,随即收线:尾块在收尾补发,服务端只回一发 ack
        s.push_text(_HEAD + "請記低")
        assert await _wait_for(lambda: len(_events(ws, "task_flush")) >= 1)
        ws.server_push(_AUDIO)
        await asyncio.sleep(0.05)
        s.end_input()
        ws.server_push(_FLUSHED)  # 只回一发 ack(无论先兑头段还是收尾,都要能收线)
        ok = await _wait_for(lambda: s._task.done(), timeout=8)
        assert ok, "单发 ack 未完成收尾(15s 悬挂)"
        async for _ev in s:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=12))


def test_no_early_cut_no_head_flush(monkeypatch):
    """整句首块(句末标点让位/无早切)不催产——服务端见句号自然起合成。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("好嘅。")  # 短句,句末让位 → 无早切
        ok = await _wait_for(lambda: len(_continue_texts(ws)) >= 1)
        assert ok, _continue_texts(ws)
        await asyncio.sleep(0.15)
        assert _events(ws, "task_flush") == [], "无早切不得催产"
        ws.server_push(_AUDIO)
        await asyncio.sleep(0.1)
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_forward_env_registers_head_flush():
    """源级 pin:MINIMAX_BIDI_HEAD_FLUSH 必须在 bok _FORWARD_ENV 表里(prod 可达)。"""
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    import tools.bok as bok  # noqa: E402

    assert "MINIMAX_BIDI_HEAD_FLUSH" in bok._FORWARD_ENV
