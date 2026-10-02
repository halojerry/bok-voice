"""F-10(2026-09-23 生产就绪修复波):MiniMax bidi 限流族 task_failed 守卫。

实弹证据(T3 报告 F-10 Blocker):1002(RPM 限流)呼叫开局突发即触后,适配器
不关连接、不退避、不回落——死会话上继续 task_continue → 794 行
「no audio frames were pushed」,且失败重试自我维持限流(同通后续轮全灭)。

官方指引(platform.minimax.io t2a_v2_bidi):task_failed 必须关闭连接并处理错误。
守卫契约(BOK_MINIMAX_BIDI_GUARD 默认 "1"):
- 限流族 {1002,1039,2205}(2205 仅 task_failed 形态;软背压形态保留原重发路径):
  关当前 WS 弃会话 → 1002 首击直接回落 HTTP;1039/2205 指数退避重试(1s/2s),
  耗尽回落 HTTP;
- 同通连续 3 轮限流 → 熔断:剩余轮跳过 WS 文本只记账,收尾直接 HTTP;
- 非限流族维持现状记日志;guard=0 全部回旧行为。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import MiniMaxTTS  # noqa: E402


def _task_failed(status: int) -> str:
    return json.dumps({"event": "task_failed", "base_resp": {"status_code": status, "status_msg": "limit"}})


_CONNECTED = '{"event": "connected_success"}'
_STARTED = '{"event": "task_started"}'
_FLUSHED = '{"event": "task_flushed"}'
_AUDIO = '{"data": {"audio": "' + "7f" * 2000 + '"}}'


class _ConnectionClosed(Exception):
    """模拟 websockets.exceptions.ConnectionClosed:关闭后 send/recv 抛。"""


class _WS:
    """可编程 fake bidi WS:脚本消息 + 测试侧 server_push。

    close 后 send 抛 `_ConnectionClosed`(与真 websockets 同型)——C-1 复现
    依赖此语义:守卫触发后继续 ws.send 必须抛,才暴露「外层 except 吞掉
    finalize」的静默轮。
    """

    def __init__(self, script: list[str] | None = None):
        self._script = list(script or [])
        self.sent: list[dict] = []
        self.closed = False
        self._q: asyncio.Queue[str] = asyncio.Queue()
        for m in self._script:
            self._q.put_nowait(m)

    def server_push(self, raw: str) -> None:
        self._q.put_nowait(raw)

    async def recv(self):
        if self.closed and self._q.empty():
            raise _ConnectionClosed("sent on closed ws")
        return await self._q.get()

    async def send(self, payload):
        if self.closed:
            raise _ConnectionClosed("sent on closed ws")
        self.sent.append(json.loads(payload))

    async def ping(self):
        if self.closed:
            raise _ConnectionClosed("ping on closed ws")
        return

    async def close(self):
        self.closed = True


class _SlowAudioWS(_WS):
    """首包前有间隙(默认 0.8s > 旧 flush 档 recv 窗 0.5s)的连接——I-1 鉴别:
    重试后 `_flushed_evt` 不清,新连接 recv 窗恒 0.5s,间隙首包必被误判排干。"""

    def __init__(self, script: list[str] | None = None, delay_s: float = 0.8):
        super().__init__(script)
        self._delay_s = delay_s

    async def recv(self):
        raw = await super().recv()
        if '"audio"' in raw:
            await asyncio.sleep(self._delay_s)
        return raw


class _Connect:
    def __init__(self, sockets: list[_WS]):
        self._sockets = list(sockets)
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        return self._sockets.pop(0)


class _Http:
    """fake httpx.AsyncClient:返回一段 hex PCM;记录请求。"""

    calls = 0
    last_json: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, *, headers=None, json=None):
        type(self).calls += 1
        type(self).last_json = dict(json or {})

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": {"audio": "7f" * 4800}, "base_resp": {"status_code": 0}}

        return _Resp()


@pytest.fixture()
def guard_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    monkeypatch.setenv("MINIMAX_REGION", "cn")
    monkeypatch.setenv("MINIMAX_LANGUAGE_BOOST", "")
    monkeypatch.setenv("MINIMAX_PAUSE", "0")
    monkeypatch.setenv("MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S", "0")  # 看门狗让位给守卫断言
    monkeypatch.delenv("BOK_MINIMAX_BIDI_GUARD", raising=False)
    yield monkeypatch


def _make_tts() -> MiniMaxTTS:
    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse"},
        sample_rate=24000,
        api_key="test-key",
    )


def _mock_http(monkeypatch) -> type[_Http]:
    monkeypatch.setattr(lp, "httpx", types.SimpleNamespace(AsyncClient=_Http))
    _Http.calls = 0
    _Http.last_json = {}
    return _Http


async def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def _continue_texts(ws: _WS) -> list[str]:
    return [m.get("text") for m in ws.sent if m.get("event") == "task_continue"]


def test_rate_limit_1002_closes_ws_and_falls_back_to_http(guard_env, monkeypatch):
    """1002(RPM 限流)首击:关死会话连接,当轮直接 HTTP 回落出声。"""
    ws = _WS([_CONNECTED, _STARTED])
    fake_connect = _Connect([ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    fake_http = _mock_http(monkeypatch)
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        assert await _wait_for(lambda: len(_continue_texts(ws)) == 1)
        ws.server_push(_task_failed(1002))
        s.end_input()
        assert await _wait_for(lambda: fake_http.calls >= 1, timeout=5), "1002 应直接回落 HTTP"
        await _wait_for(lambda: s._task.done(), timeout=10)
        got = bytearray()
        async for a in s:
            got += bytes(a.frame.data)
        return got

    got = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert ws.closed, "限流后必须关闭当前 WS 连接(官方 task_failed 指引)"
    assert fake_connect.calls == 1, "1002 首击不重试 WS(退避撞同一线程白烧预算)"
    assert bytes(got), "HTTP 回落必须推出可播音频"
    assert fake_http.last_json.get("text"), "HTTP 载荷应携带本流已发文本"
    assert tts._bidi_session().rate_limit_streak == 1, "限流轮应计入连续熔断计数"


def test_rate_limit_mid_stream_multi_chunk_chunks_accounted(guard_env, monkeypatch, capsys):
    """C-1(评审返工):守卫触发后 LLM 仍在流式上产的后续 chunk,必须只记账,
    绝不 ws.send(关后抛 ConnectionClosed → 外层 except 吞掉守卫收尾=零音频
    零 beep 静默轮)。生产主形态:1002 开局即触,LLM 随后吐几十个 chunk。"""
    ws = _WS([_CONNECTED, _STARTED])
    fake_connect = _Connect([ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    fake_http = _mock_http(monkeypatch)
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")  # 首 chunk:正常发出(守卫未触发)
        assert await _wait_for(lambda: len(_continue_texts(ws)) == 1)
        ws.server_push(_task_failed(1002))  # 守卫触发:连接弃置、旗标全置
        assert await _wait_for(lambda: s._rate_limited, timeout=5), "守卫应已触发"
        assert ws.closed
        # LLM 继续流式:多个后续 chunk 在守卫触发态到达(生产主形态)。
        s.push_text("，我帮您查一下")
        s.push_text("这个单号。")
        s.push_text("稍等。")
        s.end_input()
        assert await _wait_for(lambda: s._task.done(), timeout=10)
        got = bytearray()
        async for a in s:
            got += bytes(a.frame.data)
        return got

    got = asyncio.run(asyncio.wait_for(run(), timeout=20))
    # 全部 chunk(含触发后的)都进了 HTTP 回落文本,唔会静默丢轮。
    assert fake_http.calls >= 1, "守卫触发后仍要走到 finalize 的 HTTP 回落"
    merged = fake_http.last_json.get("text") or ""
    assert "你好" in merged and "我帮您查一下" in merged and "稍等" in merged, (
        f"触发后 chunk 必须并入回落文本: {merged!r}")
    assert bytes(got), "回落必须出声(C-1 静默轮回归钉)"
    out = capsys.readouterr().out
    assert "MINIMAX_BIDI_RATE_LIMIT_FALLBACK_HTTP" in out
    assert "MINIMAX_TTS_BIDI_ERR" not in out, "唔应再走 send 抛异常被外层吞的旧路径"
    assert tts._bidi_session().rate_limit_streak == 1


def test_rate_limit_1039_retry_recovery_survives_slow_first_audio(guard_env, monkeypatch, capsys):
    """I-1(评审返工):WS 重试恢复全链——重试前必须清 `_flushed_evt`/
    `_canceled_evt`,否则新连接 recv 窗恒 0.5s,首包间隙 >0.5s 即被误判排干,
    重试恒失败落 HTTP(恢复名存实亡)+ flush 立即返回截尾。"""
    guard_env.setattr(lp, "_MINIMAX_BIDI_RATE_LIMIT_RETRY_DELAYS", (0.05, 0.05))
    ws1 = _WS([_CONNECTED, _STARTED])
    ws2 = _SlowAudioWS([_CONNECTED, _STARTED, _AUDIO, _FLUSHED], delay_s=0.8)
    fake_connect = _Connect([ws1, ws2])
    monkeypatch.setattr("websockets.connect", fake_connect)
    fake_http = _mock_http(monkeypatch)
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        assert await _wait_for(lambda: len(_continue_texts(ws1)) == 1)
        ws1.server_push(_task_failed(1039))
        s.end_input()
        assert await _wait_for(lambda: fake_connect.calls == 2, timeout=5)
        assert await _wait_for(lambda: s._task.done(), timeout=15)
        got = bytearray()
        async for a in s:
            got += bytes(a.frame.data)
        return got

    got = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert fake_http.calls == 0, "慢首包重试应恢复,唔应落 HTTP"
    assert "MINIMAX_BIDI_RATE_LIMIT_RECOVERED" in capsys.readouterr().out
    assert b"\x7f" in bytes(got), "恢复后的重发音频应完整出声(含慢首包)"


def test_rate_limit_1039_retries_ws_and_recovers(guard_env, monkeypatch, capsys):
    """1039(TPM 限流)task_failed:关连接 → 退避重试新会话 → 出声恢复,计数归零。

    恢复在收尾段(输入排空后)执行:限流即弃会话,LLM 文本继续记账,排空后
    合并重发到新会话。
    """
    guard_env.setattr(lp, "_MINIMAX_BIDI_RATE_LIMIT_RETRY_DELAYS", (0.05, 0.05))
    ws1 = _WS([_CONNECTED, _STARTED])
    ws2 = _WS([_CONNECTED, _STARTED, _AUDIO, _FLUSHED])
    fake_connect = _Connect([ws1, ws2])
    monkeypatch.setattr("websockets.connect", fake_connect)
    fake_http = _mock_http(monkeypatch)
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        assert await _wait_for(lambda: len(_continue_texts(ws1)) == 1)
        ws1.server_push(_task_failed(1039))
        s.end_input()
        assert await _wait_for(lambda: fake_connect.calls == 2, timeout=5), "退避后应重试新会话"
        assert await _wait_for(lambda: len(_continue_texts(ws2)) == 1, timeout=5)
        assert await _wait_for(lambda: s._task.done(), timeout=15)
        async for _a in s:
            pass

    asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert ws1.closed, "限流即弃旧连接"
    assert fake_http.calls == 0, "重试恢复后不应再走 HTTP"
    assert "MINIMAX_BIDI_RATE_LIMIT_RECOVERED" in capsys.readouterr().out, "恢复应打点"
    assert tts._bidi_session().rate_limit_streak == 0, "恢复出声=连续限流断链"


def test_rate_limit_retries_exhausted_falls_back_to_http(guard_env, monkeypatch):
    """1039 重试(两档退避)都失败 → 回落 HTTP。"""
    guard_env.setattr(lp, "_MINIMAX_BIDI_RATE_LIMIT_RETRY_DELAYS", (0.05, 0.05))
    ws1 = _WS([_CONNECTED, _STARTED])
    ws2 = _WS([_CONNECTED, _STARTED])
    ws3 = _WS([_CONNECTED, _STARTED])
    fake_connect = _Connect([ws1, ws2, ws3])
    monkeypatch.setattr("websockets.connect", fake_connect)
    fake_http = _mock_http(monkeypatch)
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        assert await _wait_for(lambda: len(_continue_texts(ws1)) == 1)
        ws1.server_push(_task_failed(1039))
        s.end_input()
        assert await _wait_for(lambda: fake_http.calls >= 1, timeout=12)
        await _wait_for(lambda: s._task.done(), timeout=15)

    asyncio.run(asyncio.wait_for(run(), timeout=40))
    assert fake_connect.calls == 3, f"应有两次 WS 重试: calls={fake_connect.calls}"
    assert all(w.closed for w in (ws1, ws2, ws3)), "每次重试失败都应弃会话"
    assert tts._bidi_session().rate_limit_streak == 1, "整轮一次限流只计一次"


def test_circuit_opens_after_three_rate_limited_turns(guard_env, monkeypatch, capsys):
    """同通连续 3 轮限流 → 熔断:第 4 轮零 WS 连接,文本只记账,直接 HTTP。"""
    # 每个预脚本连接:握手完成即吃 1002 task_failed(开局突发即触=实弹同型)。
    sockets = [_WS([_CONNECTED, _STARTED, _task_failed(1002)]) for _ in range(3)]
    fake_connect = _Connect(list(sockets))
    monkeypatch.setattr("websockets.connect", fake_connect)
    fake_http = _mock_http(monkeypatch)
    tts = _make_tts()
    session = tts._bidi_session()

    async def one_turn(text: str):
        s = tts.stream()
        s.push_text(text)
        s.end_input()
        assert await _wait_for(lambda: s._task.done(), timeout=10)
        async for _a in s:
            pass

    async def run():
        for i in range(3):
            await one_turn(f"第{i}轮")
        assert session.rate_limit_streak >= 3, f"三轮限流应达熔断阈值: {session.rate_limit_streak}"
        connect_before = fake_connect.calls
        http_before = fake_http.calls
        await one_turn("第四轮")
        assert fake_connect.calls == connect_before, "熔断轮不得再碰 WS"
        assert fake_http.calls == http_before + 1, "熔断轮应直接 HTTP(文本只记账)"

    asyncio.run(asyncio.wait_for(run(), timeout=40))
    assert "MINIMAX_BIDI_CIRCUIT_OPEN" in capsys.readouterr().out


def test_non_rate_limit_task_failed_keeps_old_behavior(guard_env, monkeypatch, capsys):
    """非限流族 task_failed(如 2038):维持现状只记日志,不关连接不回落。"""
    ws = _WS([_CONNECTED, _STARTED])
    fake_connect = _Connect([ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    fake_http = _mock_http(monkeypatch)
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        assert await _wait_for(lambda: len(_continue_texts(ws)) == 1)
        ws.server_push(_task_failed(2038))
        s.end_input()
        # 非限流族维持现状:流保持等待(唔触发守卫收尾、唔回落、唔拆连接)。
        await asyncio.sleep(0.6)
        assert not s._task.done(), "非限流族 task_failed 唔应触发守卫收尾/回落"
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert not ws.closed, "非限流族唔应拆连接(现状保留)"
    assert fake_http.calls == 0, "非限流族唔应回落 HTTP"
    assert fake_connect.calls == 1
    assert tts._bidi_session().rate_limit_streak == 0
    out = capsys.readouterr().out
    assert "MINIMAX_TTS_BIDI_STATUS 2038" in out
    assert "MINIMAX_BIDI_RATE_LIMIT" not in out


def test_soft_backpressure_2205_on_non_failed_event_keeps_resend(guard_env, monkeypatch):
    """2205 双形态:非 task_failed 携带的 2205=软背压,保留原样重发路径。"""
    monkeypatch.setenv("MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S", "0")
    ws = _WS([_CONNECTED, _STARTED])
    fake_connect = _Connect([ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    fake_http = _mock_http(monkeypatch)
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        assert await _wait_for(lambda: len(_continue_texts(ws)) == 1)
        ws.server_push(json.dumps({"event": "task_continued", "base_resp": {"status_code": 2205}}))
        assert await _wait_for(
            lambda: any(m.get("event") == "task_continue" and m.get("text") == "你好"
                        for m in ws.sent[1:]), timeout=5), "2205 软背压应原样重发"
        assert fake_connect.calls == 1, "软背压唔应重连"
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert not ws.closed
    assert tts._bidi_session().rate_limit_streak == 0
    assert fake_http.calls == 0


def test_guard_off_restores_legacy_behavior(guard_env, monkeypatch, capsys):
    """BOK_MINIMAX_BIDI_GUARD=0:回旧行为——不关连接、不重试、不回落。"""
    monkeypatch.setenv("BOK_MINIMAX_BIDI_GUARD", "0")
    ws = _WS([_CONNECTED, _STARTED])
    fake_connect = _Connect([ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    fake_http = _mock_http(monkeypatch)
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好")
        assert await _wait_for(lambda: len(_continue_texts(ws)) == 1)
        ws.server_push(_task_failed(1002))
        s.end_input()
        await asyncio.sleep(0.8)
        s._task.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert not ws.closed, "guard=0 应维持旧行为:不关连接"
    assert fake_http.calls == 0
    assert fake_connect.calls == 1
    assert tts._bidi_session().rate_limit_streak == 0
    out = capsys.readouterr().out
    assert "MINIMAX_TTS_BIDI_STATUS 1002" in out, "旧行为:只记 STATUS 日志"
    assert "MINIMAX_BIDI_RATE_LIMIT" not in out
