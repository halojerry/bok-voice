"""bidi 中途死亡可见性单测(orch2-C,2026-10-02,fake WS,无网络)。

编排审计第二波定案:`_recv_loop` 的 socket 死亡分支静默
``invalidate(reprewarm=True)+置旗 return``——音频已推过的中途死亡=客户听到
截断回复,日志/账本零痕;收尾 finalize 因 ``_flushed_evt`` 已被死亡分支置位
直接滑过,PERF 行照报「正常收线」。本文件钉死三处:

- 中途死亡打 ``MINIMAX_TTS_BIDI_DIED_MID_REPLY first_audio=1`` 标记;
- 收尾 PERF 行带 ``truncated_mid_reply=1``(首包前死亡不带该字段);
- 2201/2206 状态分支可中途到达,同打标记+置旗;
- 死连接语义零漂移:``_flushed_evt``/``_canceled_evt`` 照旧置位。

harness 镜像 tests/test_bidi_head_flush.py。
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
    """测试侧可随时注入服务端消息/注入 recv 异常的 fake WS(流级时序控制)。"""

    def __init__(self):
        self.sent: list[dict] = []
        self.pings = 0
        self.closed = False
        self._q: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        self.server_push(_CONNECTED)
        self.server_push(_STARTED)

    def server_push(self, raw: str) -> None:
        self._q.put_nowait(("msg", raw))

    def server_raise(self, exc: BaseException) -> None:
        """下一次 recv 抛该异常(模拟连接中途死亡)。"""
        self._q.put_nowait(("raise", exc))

    async def recv(self):
        kind, payload = await self._q.get()
        if kind == "raise":
            raise payload  # type: ignore[misc]
        return payload

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
# 2000 字节 PCM(24k 16bit):≥ 首推门槛 frame_bytes//5=1920,过 stale 门后必出首包
_AUDIO = '{"data": {"audio": "' + "00" * 2000 + '"}}'


@pytest.fixture(autouse=True)
def _bidi_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    monkeypatch.delenv("MINIMAX_WS_URL", raising=False)
    monkeypatch.setenv("MINIMAX_REGION", "cn")
    monkeypatch.setenv("MINIMAX_LANGUAGE_BOOST", "")
    monkeypatch.setenv("MINIMAX_PAUSE", "0")
    # 首包看门狗与死亡判别无关,关掉防测试内重连干扰
    monkeypatch.setenv("MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S", "0")
    # 假 WS 池无新连接可用:死亡分支的自动重预热没有测试语义,关掉免噪声
    monkeypatch.setenv("MINIMAX_BIDI_AUTO_REWARM", "0")
    # 假 key(随机值,非凭据):插件凭据门要求非空,fake WS 不校验真伪
    monkeypatch.setenv("BOK_TEST_TTS_KEY", uuid.uuid4().hex)
    yield


def _make_tts():
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


def _continue_texts(ws) -> list[str]:
    return [m.get("text") for m in ws.sent if m.get("event") == "task_continue"]


def _perf_lines(out: str) -> list[str]:
    return [l for l in out.splitlines() if "MINIMAX_BIDI_PERF sentences=" in l]


def _mid_reply_until_death(ws, tts, die):
    """公共时序:发文本→等 continue→推音频→等首包→触发死亡→等流收尾。"""

    async def run():
        s = tts.stream()
        s.push_text("你好。")
        assert await _wait_for(lambda: len(_continue_texts(ws)) >= 1), _continue_texts(ws)
        ws.server_push(_AUDIO)
        assert await _wait_for(lambda: s._first_audio_evt.is_set()), "首包未推"
        die(ws)
        s.end_input()  # 输入循环收口,_run 才会走到 finalize/PERF
        assert await _wait_for(lambda: s._task.done(), timeout=8), "死亡后流未收尾"
        # 死连接语义不变:两个旗标照旧由死亡分支置位
        assert s._flushed_evt.is_set(), "_flushed_evt 应被死亡分支置位"
        assert s._canceled_evt.is_set(), "_canceled_evt 应被死亡分支置位"
        return s

    return run


def test_socket_death_mid_reply_marked_and_perf_truncated(monkeypatch, capsys):
    """首包已推后 socket 死亡:打 DIED_MID_REPLY(first_audio=1)+ PERF 带截断旗。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()
    run = _mid_reply_until_death(ws, tts, lambda w: w.server_raise(RuntimeError("boom mid-reply")))

    asyncio.run(asyncio.wait_for(run(), timeout=15))

    out = capsys.readouterr().out
    assert "MINIMAX_TTS_BIDI_DIED_MID_REPLY first_audio=1" in out, out
    assert "reason=recv-exc" in out, out
    assert "boom mid-reply" in out, out
    perf = _perf_lines(out)
    assert perf and "truncated_mid_reply=1" in perf[-1], perf


def test_status_2201_mid_reply_marked_and_perf_truncated(monkeypatch, capsys):
    """2201 状态行可中途到达:同打标记(带 status)+ 置截断旗;原 _2201 日志保留。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()
    run = _mid_reply_until_death(
        ws, tts, lambda w: w.server_push('{"base_resp": {"status_code": 2201}}')
    )

    asyncio.run(asyncio.wait_for(run(), timeout=15))

    out = capsys.readouterr().out
    assert "MINIMAX_TTS_BIDI_2201" in out, out
    assert "MINIMAX_TTS_BIDI_DIED_MID_REPLY first_audio=1" in out, out
    assert "reason=status-2201" in out, out
    perf = _perf_lines(out)
    assert perf and "truncated_mid_reply=1" in perf[-1], perf


def test_socket_death_before_first_audio_not_truncated(monkeypatch, capsys):
    """首包前死亡:标记照打(first_audio=0)但 PERF 不带截断旗(未截断任何音频)。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好。")
        assert await _wait_for(lambda: len(_continue_texts(ws)) >= 1), _continue_texts(ws)
        ws.server_raise(RuntimeError("boom pre-audio"))
        s.end_input()  # 输入循环收口,_run 才会走到 finalize/PERF
        assert await _wait_for(lambda: s._task.done(), timeout=8), "死亡后流未收尾"
        # W1a(2026-10-06 demo-quality wave)后:零音频正常收尾经 _minimax_zero_audio_pad
        # 垫静音把 emitter 正常启动——框架 601 行 end_input 不再炸(旧崩形=
        # RuntimeError("AudioEmitter isn't started") 上抛 FallbackAdapter 误切
        # backup 4-7s 黑窗;同翻转见 test_reconnect_after_2201)。
        assert s._task.exception() is None
        return s

    asyncio.run(asyncio.wait_for(run(), timeout=15))

    out = capsys.readouterr().out
    assert "MINIMAX_TTS_BIDI_DIED_MID_REPLY first_audio=0" in out, out
    assert "truncated_mid_reply" not in out, out
