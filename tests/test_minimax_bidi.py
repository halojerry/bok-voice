"""MiniMax bidi 持久连接(t2a_v2_bidi)单测(fake WS,无网络,镜像 test_minimax_ws_pool.py)。

MINIMAX_WS_MODE=bidi 选入(默认 classic 零变化):一条连接服务整个 call,
task_continue 逐字透传(服务端切句)、task_flush 收尾(连接保留)、打断 task_cancel
(连接保留)、60s 客户端 ping、2205 软背压重发、2201 断连自动重连。
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
from agent_runtime.providers.livekit_plugins import MiniMaxTTS  # noqa: E402


class _FakeWS:
    """鸭型 WS:recv 按脚本回消息;send/ping 记录;脚本耗尽后挂起等 cancel。"""

    def __init__(self, script: list[str] | None = None):
        self._script = list(script or [])
        self.sent: list[dict] = []
        self.pings = 0
        self.closed = False

    async def recv(self):
        if self._script:
            return self._script.pop(0)
        await asyncio.Event().wait()  # 挂起:等收尾 cancel

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def ping(self):
        self.pings += 1

    async def close(self):
        self.closed = True


_CONNECTED = '{"event": "connected_success"}'
_STARTED = '{"event": "task_started"}'
_FLUSHED = '{"event": "task_flushed"}'
_CANCELED = '{"event": "task_canceled"}'
# 2000 字节 PCM(24k 16bit):≥ 首推门槛 frame_bytes//5=1920,够 pushed_duration>0
_AUDIO = '{"data": {"audio": "' + "00" * 2000 + '"}}'


class _FakeConnect:
    """排队发 fake WS;记录 connect 次数(断连重连断言用)。"""

    def __init__(self, sockets: list[_FakeWS]):
        self._sockets = list(sockets)
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        return self._sockets.pop(0)


class _QueueWS:
    """测试侧可随时注入服务端消息的 fake WS(测打断残留音频门禁的次序控制)。"""

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


@pytest.fixture(autouse=True)
def _bidi_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    monkeypatch.delenv("MINIMAX_WS_URL", raising=False)
    monkeypatch.setenv("MINIMAX_REGION", "cn")
    monkeypatch.setenv("MINIMAX_LANGUAGE_BOOST", "")
    monkeypatch.setenv("MINIMAX_PAUSE", "0")
    yield


def _make_tts():
    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse", "cantonese": "Cantonese_crisp_news_anchor_vv2"},
        sample_rate=24000,
        api_key="test-key",
    )


async def _wait_for(pred, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def _events(ws: _FakeWS) -> list[str]:
    return [m.get("event") for m in ws.sent]


def _continue_texts(ws: _FakeWS) -> list[str]:
    return [m.get("text") for m in ws.sent if m.get("event") == "task_continue"]


def test_char_granularity_continue_passthrough(monkeypatch):
    """逐字 push → task_continue 原样透传:唔切句、唔合并、唔插停顿标签。"""
    ws = _FakeWS([_CONNECTED, _STARTED, _CANCELED])
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        for ch in "你好。":
            s.push_text(ch)
        ok = await _wait_for(lambda: len(_continue_texts(ws)) == 3)
        assert ok, f"3 次逐字 continue 未到: {_continue_texts(ws)}"
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert _continue_texts(ws) == ["你", "好", "。"]
    assert all("<#" not in t for t in _continue_texts(ws)), "bidi 模式不应插停顿标签"


def test_flush_on_end_input_and_connection_kept(monkeypatch, capsys):
    """end_input → task_flush(唔係 task_finish);task_flushed 后连接保留唔拆。"""
    ws = _FakeWS([_CONNECTED, _STARTED, _AUDIO, _FLUSHED, _AUDIO])
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        s.end_input()
        ok = await _wait_for(lambda: "task_flush" in _events(ws))
        assert ok, f"task_flush 未发出: {_events(ws)}"
        await _wait_for(lambda: s._task.done(), timeout=10)
        async for _ev in s:  # 排干音频事件,顺带确认流正常完结
            pass
        assert ws.pings >= 0

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert "task_finish" not in _events(ws), "回合收尾应 task_flush,唔係 task_finish"
    assert not ws.closed, "bidi 会话跨回合保留,唔应拆连接"
    out = capsys.readouterr().out
    assert "MINIMAX_TTS_BIDI_PERF" in out
    assert "first_continue_to_audio_ms=" in out


def test_cancel_sends_task_cancel_and_keeps_connection(monkeypatch):
    """打断 → task_cancel + 等 task_canceled;连接保留,下个流同一条连接继续。"""
    ws = _FakeWS([_CONNECTED, _STARTED, _CANCELED, _FLUSHED, _FLUSHED])
    fake_connect = _FakeConnect([ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        s1 = tts.stream()
        s1.push_text("你好。")
        await _wait_for(lambda: len(_continue_texts(ws)) >= 1)
        s1._task.cancel()  # 模拟框架 barge-in cancel
        await asyncio.sleep(0.3)
        assert "task_cancel" in _events(ws), f"打断应发 task_cancel: {_events(ws)}"
        assert not ws.closed, "打断后连接应保留"

        # 下一个流(下一轮对话):同一条连接直接 task_continue,零重连
        s2 = tts.stream()
        s2.push_text("再见")
        s2.end_input()
        ok = await _wait_for(lambda: "再见" in _continue_texts(ws))
        assert ok, "第二个流应复用连接继续 task_continue"
        await _wait_for(lambda: s2._task.done(), timeout=10)
        assert fake_connect.calls == 1, f"两个流一条连接,connect 次数应 1: {fake_connect.calls}"

    asyncio.run(asyncio.wait_for(run(), timeout=15))


def test_cancel_timeout_stale_audio_gated_for_next_stream(monkeypatch, capsys):
    """打断 cancel 超时(连接保留)→ 上一流迟到残留音频被纪元门禁丢弃,
    唔漏进下一流的 emitter;新流自己 task_continue 认领纪元后音频照常转发。"""
    monkeypatch.setenv("MINIMAX_BIDI_CANCEL_WAIT_S", "0.2")  # 等唔到 task_canceled → 超时
    ws = _QueueWS()
    fake_connect = _FakeConnect([ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()
    stale_hex = "11" * 2000  # 被打断那句的残留(服务端未停稳)
    fresh_hex = "22" * 2000  # 新一轮自己的合成音频
    got = {"audio": bytearray()}

    async def run():
        # 流 1:推文本(认领纪元)→ 打断 → cancel 等超时,连接保留。
        s1 = tts.stream()
        s1.push_text("你好。")
        assert await _wait_for(lambda: len(_continue_texts(ws)) >= 1), _continue_texts(ws)
        s1._task.cancel()
        await _wait_for(lambda: s1._task.done(), timeout=10)
        assert "task_cancel" in _events(ws), _events(ws)
        assert not ws.closed, "cancel 超时后连接应保留"

        # 流 1 收摊后服务端才吐残留音频——排队等在同一条连接上。
        ws.server_push('{"data": {"audio": "' + stale_hex + '"}}')
        ws.server_push('{"data": {"audio": "' + stale_hex + '"}}')

        # 流 2(下一轮)复用连接:此时尚未 task_continue(active_epoch 还是流 1 的)
        # → 残留音频必须被门禁丢弃。唔 push 文本,保证残留先到、continue 后到。
        s2 = tts.stream()
        await asyncio.sleep(0.2)  # 等 _run 过 ensure_ready、recv_task 挂上队列收残留

        # 新流首个 task_continue 认领纪元 → 门禁对本流放行。
        s2.push_text("再见")
        s2.end_input()
        assert await _wait_for(lambda: "再见" in _continue_texts(ws)), _continue_texts(ws)
        ws.server_push('{"data": {"audio": "' + fresh_hex + '"}}')
        ws.server_push(_FLUSHED)

        async for a in s2:
            got["audio"] += bytes(a.frame.data)
        await _wait_for(lambda: s2._task.done(), timeout=10)

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert fake_connect.calls == 1, f"cancel 超时连接保留,两流应一条连接: {fake_connect.calls}"
    out = capsys.readouterr().out
    assert "MINIMAX_BIDI_DROP_STALE" in out, "残留音频应被丢弃并打点"
    # 新一轮收到嘅音频净係得自己 continue 之后嗰段;残留(0x11)一滴都冇漏。
    audio = bytes(got["audio"])
    assert b"\x11\x11" not in audio, "被打断句的残留音频漏进了下一流(门禁失效)"
    assert b"\x22\x22" in audio, "新流认领纪元后的正常音频应照常转发"


def test_ping_timer_fires(monkeypatch):
    """客户端定期 ping(官方:服务端永不 ping,空闲 >120s 断连)。"""
    monkeypatch.setenv("MINIMAX_BIDI_PING_S", "0.05")
    ws = _FakeWS([_CONNECTED, _STARTED, _FLUSHED])
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        s.end_input()
        await _wait_for(lambda: "task_start" in _events(ws))
        await asyncio.sleep(0.25)  # 等 ping 循环走 3+ 拍
        assert ws.pings >= 2, f"ping 计时应触发: pings={ws.pings}"
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_2205_soft_backpressure_resends(monkeypatch, capsys):
    """2205 软背压 → 稍后原样重发同一条 task_continue,唔重连。"""
    ws = _FakeWS([_CONNECTED, _STARTED, '{"base_resp": {"status_code": 2205}}', _CANCELED])
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        ok = await _wait_for(lambda: _continue_texts(ws).count("你好") >= 2, timeout=5)
        assert ok, f"2205 后应原样重发: {_continue_texts(ws)}"
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert "MINIMAX_TTS_BIDI_2205_RESEND" in capsys.readouterr().out


def test_reconnect_after_2201(monkeypatch):
    """2201(空闲断连)→ 本流按既有语义失败;会话判死,下个流全新重连+task_start。"""
    ws1 = _FakeWS([_CONNECTED, _STARTED, '{"base_resp": {"status_code": 2201}}'])
    ws2 = _FakeWS([_CONNECTED, _STARTED, _AUDIO, _FLUSHED, _AUDIO])
    fake_connect = _FakeConnect([ws1, ws2])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        s1 = tts.stream()
        s1.push_text("你好。")
        s1.end_input()
        await _wait_for(lambda: s1._task.done(), timeout=10)
        # 2201 后本流失败(零音频 → 框架 AudioEmitter 未启动,同 classic 语义)
        assert s1._task.done() and s1._task.exception() is not None

        # 下一个流(下一轮对话):自动全新重连,task_start + 完整重放输入
        s2 = tts.stream()
        s2.push_text("你好。")
        s2.end_input()
        await _wait_for(lambda: s2._task.done(), timeout=10)
        async for _ev in s2:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert fake_connect.calls == 2, f"2201 后应全新重连: {fake_connect.calls}"
    assert ws1.closed, "2201 旧连接应弃置关闭"
    assert _events(ws2)[:2] == ["task_start", "task_continue"]
    assert "task_flush" in _events(ws2)
    assert _continue_texts(ws2) == ["你好。"]


def test_param_change_rebuilds_session(monkeypatch):
    """换声(参数指纹变)→ 旧连接 task_finish 干净收掉,全新连接重 task_start。"""
    ws1 = _FakeWS([_CONNECTED, _STARTED, _AUDIO, _FLUSHED, _AUDIO])
    ws2 = _FakeWS([_CONNECTED, _STARTED, _AUDIO, _FLUSHED, _AUDIO])
    fake_connect = _FakeConnect([ws1, ws2])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        s1 = tts.stream()
        s1.push_text("你好")
        s1.end_input()
        await _wait_for(lambda: s1._task.done(), timeout=10)
        async for _ev in s1:
            pass

        tts._voice = {"zh": "other-voice"}  # 中途换声
        s2 = tts.stream()
        s2.push_text("你好")
        s2.end_input()
        await _wait_for(lambda: s2._task.done(), timeout=10)
        async for _ev in s2:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert fake_connect.calls == 2, "参数指纹变应重建连接"
    assert "task_finish" in _events(ws1), "旧会话应 task_finish 干净收掉"
    assert _events(ws2)[:1] == ["task_start"], "新连接重新 task_start"


def test_mode_env_default_classic(monkeypatch):
    """MINIMAX_WS_MODE 缺省 = classic:流类型/池路径零变化;bidi 只选入生效。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    tts = _make_tts()
    assert tts._ws_mode() == "classic"
    assert tts._endpoint_ws() == MiniMaxTTS._ENDPOINT_WS_CN

    monkeypatch.delenv("MINIMAX_WS_MODE", raising=False)
    tts2 = _make_tts()
    assert tts2._ws_mode() == "classic", "缺省必须 classic(未验证前零行为变化)"

    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    tts3 = _make_tts()
    assert tts3._endpoint_ws_bidi() == MiniMaxTTS._ENDPOINT_WS_BIDI_CN

    monkeypatch.setenv("MINIMAX_WS_URL", "wss://proxy.example/ws/v1/t2a_v2")
    assert tts3._endpoint_ws_bidi() == "wss://proxy.example/ws/v1/t2a_v2_bidi", "覆盖端点补 _bidi 后缀"

    # stream() 类型断言(SynthesizeStream.__init__ 建 task,要在 loop 内调)
    async def pick():
        monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
        assert isinstance(_make_tts().stream(), lp._MiniMaxSynthesizeStream)
        monkeypatch.delenv("MINIMAX_WS_MODE", raising=False)
        assert isinstance(_make_tts().stream(), lp._MiniMaxSynthesizeStream)
        monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
        assert isinstance(_make_tts().stream(), lp._MiniMaxBidiStream)

    asyncio.run(asyncio.wait_for(pick(), timeout=5))
