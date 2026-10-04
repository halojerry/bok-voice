"""W-TTS: MiniMaxTTS.prewarm() 冻结契约（async -> bool）+ 握手前移池单测。

契约（会话装配单点调用，签名冻结）::

    async def prewarm(self) -> bool
    预热一次 WS 会话入池。返回 True=池里现在有暖会话;False=不可预热
    (HTTP 车道/未配置/失败)。绝不 raise。

合成路径与握手映射（本文件钉死）：
- classic WS（MINIMAX_WS_MODE=classic）: 一连接一任务，**每轮合成一次**
  connect+task_start 握手 → 池容量 1 把 connect 前移（prewarm 入池、合成 pop
  零握手、pop 后立刻后台补池 → 第 N+1 轮同样热）;
- bidi WS（默认）: 整通一条持久会话，握手一次（task_cancel/task_flush 后连接
  保留）→ prewarm 预连，首段合成零握手段;
- HTTP 整段（MINIMAX_WS=0）: 无 WS 握手可摊销 → prewarm()=False、零行为变化。

kill-switch BOK_TTS_PREWARM=0: prewarm() 立即 False、池逻辑全旁路（合成恒流内
自连，旧行为）。池是纯快路径——陈旧/失败一律回退内联握手，合成永不因池失败。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import MiniMaxTTS  # noqa: E402

_CONNECTED = '{"event": "connected_success"}'
_STARTED = '{"event": "task_started"}'


class _FakeWS:
    """鸭型 WS（镜像 test_minimax_ws_pool.py）：recv 按脚本回消息；send 记录。"""

    def __init__(self, script: list[str] | None = None, dead: bool = False):
        self._script = list(script or [])
        self._dead = dead
        self.sent: list[dict] = []
        self.closed = False
        from websockets.protocol import State

        self.state = State.OPEN

    async def recv(self):
        if self._dead:
            raise ConnectionResetError("fake dead ws")
        if self._script:
            return self._script.pop(0)
        await asyncio.Event().wait()  # 挂起：等 _task.cancel 收尾

    async def send(self, payload):
        if self._dead:
            raise ConnectionResetError("fake dead ws")
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


class _FakeConnect:
    """排队发 fake WS；记录 connect 次数（握手计数断言用）。"""

    def __init__(self, sockets: list[_FakeWS] | None = None):
        self._sockets = list(sockets or [])
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        if self._sockets:
            return self._sockets.pop(0)
        return _FakeWS([_CONNECTED, _STARTED])  # 未预置兜底：计数断言仍会抓多余握手


@pytest.fixture(autouse=True)
def _prewarm_env(monkeypatch):
    """池全局 + env 全复位（模块级池跨测试隔离，仿 test_minimax_ws_pool）。"""
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", None)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", None)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", 0.0)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_TASK", None)
    monkeypatch.setenv("BOK_TTS_PREWARM", "1")
    monkeypatch.setenv("MINIMAX_WS_POOL", "1")
    monkeypatch.setenv("MINIMAX_REGION", "cn")
    monkeypatch.setenv("MINIMAX_PAUSE", "0")
    monkeypatch.delenv("MINIMAX_WS", raising=False)
    monkeypatch.delenv("MINIMAX_WS_URL", raising=False)
    monkeypatch.delenv("MINIMAX_WS_MODE", raising=False)
    monkeypatch.delenv("MINIMAX_LANGUAGE_BOOST", raising=False)
    yield


def _make_tts(api_key: str = "test-key") -> MiniMaxTTS:
    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse", "cantonese": "Cantonese_crisp_news_anchor_vv2"},
        sample_rate=24000,
        api_key=api_key,
    )


async def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def _run_one_round(tts: MiniMaxTTS, ws: _FakeWS):
    """跑一轮 classic 合成，等目标连接收到 task_continue 即返回（不取消）。"""
    s = tts.stream()
    s.push_text("你好。")
    s.end_input()
    for _ in range(100):
        if [m for m in ws.sent if m.get("event") == "task_continue"]:
            break
        await asyncio.sleep(0.02)
    return s


async def _drain_pool_task():
    """等在飞补池任务收尾（防跨测试泄漏后台任务）。"""
    for _ in range(100):
        if lp._MINIMAX_POOL_TASK is None:
            return
        await asyncio.sleep(0.02)


def test_prewarm_pool_hit_every_round_skips_handshake(monkeypatch, capsys):
    """prewarm()=True → 合成吃池连接零握手；pop 后立刻补池 → 第 2 轮同样热。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    sockets = [_FakeWS([_CONNECTED, _STARTED]) for _ in range(3)]
    fake_connect = _FakeConnect(sockets)
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        assert await tts.prewarm() is True
        assert fake_connect.calls == 1, "prewarm 恰一次握手"
        assert lp._MINIMAX_POOL_WS is sockets[0], "暖会话应泊在池里"

        # 第 1 轮：pop 池连接 → 本轮零新握手；pop 后立刻后台补池（不等收尾）
        s1 = await _run_one_round(tts, sockets[0])
        assert await _wait_for(lambda: fake_connect.calls == 2), "pop 后应立即后台补池"
        assert lp._MINIMAX_POOL_WS is sockets[1], "补的应是 pools[1]"
        s1._task.cancel()
        await asyncio.sleep(0.05)

        # 第 2 轮：吃补好的池，同样零新握手 + 再补
        s2 = await _run_one_round(tts, sockets[1])
        assert await _wait_for(lambda: fake_connect.calls == 3)
        assert lp._MINIMAX_POOL_WS is sockets[2]
        s2._task.cancel()
        await asyncio.sleep(0.05)
        await _drain_pool_task()

    asyncio.run(asyncio.wait_for(run(), timeout=15))

    assert [m["event"] for m in sockets[0].sent][:2] == ["task_start", "task_continue"]
    assert [m["event"] for m in sockets[1].sent][:2] == ["task_start", "task_continue"]
    assert [m["event"] for m in sockets[2].sent] == [], "补池连接只 connect，不 task_start"
    out = capsys.readouterr().out
    assert out.count("MINIMAX_PREWARM hit=1") == 2, "每轮池命中各打一行"


def test_empty_pool_inline_handshake_payload_byte_identical(monkeypatch):
    """空池 → 流内自连（旧行为），task_start 载荷与 bidi 同源逐字节一致。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    ws = _FakeWS([_CONNECTED, _STARTED])
    fake_connect = _FakeConnect([ws, _FakeWS([_CONNECTED, _STARTED])])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        s = await _run_one_round(tts, ws)
        inline_calls = fake_connect.calls  # 取消前的握手计数（收尾补池不计）
        s._task.cancel()
        await asyncio.sleep(0.05)
        await _drain_pool_task()
        return inline_calls

    inline_calls = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert inline_calls == 1, "空池 → 恰一次流内握手"

    start = [m for m in ws.sent if m.get("event") == "task_start"]
    assert start, "应发出 task_start"
    expected = tts._task_start_payload(tts._resolve_voice(), tts.sample_rate)
    assert start[0] == expected, "classic 握手载荷必须与 _task_start_payload 同源"
    assert json.dumps(start[0], ensure_ascii=False) == json.dumps(
        expected, ensure_ascii=False
    ), "载荷逐字节一致（键序/值全同）"


def test_stale_pooled_session_discarded_inline_handshake(monkeypatch, capsys):
    """池会话已被服务端关闭 → 弃池 + 流内自连，合成照常出（池永不拖垮合成）。"""
    from websockets.protocol import State

    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    stale = _FakeWS([_CONNECTED, _STARTED])
    stale.state = State.CLOSED
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", stale)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", (MiniMaxTTS._ENDPOINT_WS_CN, "test-key"))
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", time.monotonic())
    fresh = _FakeWS([_CONNECTED, _STARTED])
    fake_connect = _FakeConnect([fresh, _FakeWS([_CONNECTED, _STARTED])])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        s = await _run_one_round(tts, fresh)
        s._task.cancel()
        await asyncio.sleep(0.05)
        await _drain_pool_task()

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    out = capsys.readouterr().out
    assert "MINIMAX_PREWARM stale=1" in out, "陈旧弃置应有归因打点"
    assert [m["event"] for m in fresh.sent][:2] == ["task_start", "task_continue"]
    assert stale.closed, "陈旧池连接应被收尾关闭"
    assert fake_connect.calls == 2, "弃陈旧 1 次流内自连 + 收尾补池 1 次"


def test_kill_switch_zero_bypasses_pool_and_prewarm(monkeypatch):
    """BOK_TTS_PREWARM=0 → prewarm False；池里毒连接不取不补，握手计数=旧行为。"""
    monkeypatch.setenv("BOK_TTS_PREWARM", "0")
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    poison = _FakeWS(dead=True)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", poison)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", (MiniMaxTTS._ENDPOINT_WS_CN, "test-key"))
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", time.monotonic())
    fresh = _FakeWS([_CONNECTED, _STARTED])
    fake_connect = _FakeConnect([fresh])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        assert await tts.prewarm() is False, "kill-switch:立即 False"
        assert fake_connect.calls == 0, "kill-switch:prewarm 零握手"
        assert lp._MINIMAX_POOL_WS is poison, "kill-switch:池泊位零触碰"
        s = await _run_one_round(tts, fresh)
        inline_calls = fake_connect.calls
        s._task.cancel()
        await asyncio.sleep(0.1)
        return inline_calls

    inline_calls = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert inline_calls == 1, "池旁路:每轮恒 1 次流内握手（旧行为）"
    assert fake_connect.calls == 1, "池逻辑全旁路:收尾也不补池"
    assert lp._MINIMAX_POOL_WS is poison, "毒连接照旧未被取用"
    assert lp._MINIMAX_POOL_TASK is None, "旁路档不排补池任务"


def test_concurrent_prewarm_single_handshake(monkeypatch):
    """并发 prewarm 单飞：三次调用共享同一次握手（inflight 去重）。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    ws = _FakeWS([_CONNECTED, _STARTED])
    fake_connect = _FakeConnect([ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        return await asyncio.gather(tts.prewarm(), tts.prewarm(), tts.prewarm())

    results = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert results == [True, True, True]
    assert fake_connect.calls == 1, "在飞单飞:并发 prewarm 只握一次手"
    assert lp._MINIMAX_POOL_WS is ws
    # 幂等：池里已有暖会话时再 prewarm 不再握手
    assert asyncio.run(asyncio.wait_for(tts.prewarm(), timeout=5)) is True
    assert fake_connect.calls == 1


def test_bidi_prewarm_first_synthesis_reuses_session(monkeypatch):
    """默认 bidi：prewarm 预连+task_start；首段合成复用同一连接（0 新握手）。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    ws = _FakeWS([_CONNECTED, _STARTED])
    fake_connect = _FakeConnect([ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        assert await tts.prewarm() is True
        assert fake_connect.calls == 1, "bidi prewarm 恰一次 connect+task_start"
        s = tts.stream()
        s.push_text("你好。")
        s.end_input()
        for _ in range(100):
            if [m for m in ws.sent if m.get("event") == "task_continue"]:
                break
            await asyncio.sleep(0.02)
        reused_calls = fake_connect.calls
        s._task.cancel()
        await asyncio.sleep(0.05)
        return reused_calls

    reused_calls = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert reused_calls == 1, "首段合成吃预热会话,零新握手"
    events = [m.get("event") for m in ws.sent]
    assert events[0] == "task_start"
    assert "task_continue" in events


def test_http_lane_prewarm_false_no_connect(monkeypatch):
    """MINIMAX_WS=0（HTTP 整段车道）→ prewarm()=False、零 connect、零行为变化。"""
    monkeypatch.setenv("MINIMAX_WS", "0")
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    fake_connect = _FakeConnect()
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()
    assert asyncio.run(asyncio.wait_for(tts.prewarm(), timeout=5)) is False
    assert fake_connect.calls == 0


def test_prewarm_false_when_unconfigured(monkeypatch):
    """未配置（无 key / 空音色）→ False，零握手（绝不 raise）。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    fake_connect = _FakeConnect()
    monkeypatch.setattr("websockets.connect", fake_connect)

    no_key = MiniMaxTTS(voice="male-qn-qingse", sample_rate=24000, api_key="")
    no_voice = _make_tts()
    no_voice._voice = ""
    assert asyncio.run(asyncio.wait_for(no_key.prewarm(), timeout=5)) is False
    assert asyncio.run(asyncio.wait_for(no_voice.prewarm(), timeout=5)) is False
    assert fake_connect.calls == 0


def test_source_pins_signature_and_forward_env():
    """签名冻结 pin + _FORWARD_ENV 登记 pin（prod launchd 封闭 env 面）。"""
    provider = (
        Path(__file__).resolve().parents[1]
        / "apps" / "agent" / "agent_runtime" / "providers" / "livekit_plugins.py"
    )
    src = provider.read_text(encoding="utf-8")
    assert "async def prewarm(self) -> bool" in src, "冻结契约签名不得改"
    assert 'os.environ.get("BOK_TTS_PREWARM", "1") == "1"' in src, "kill-switch 默认档 pin"

    import bok  # noqa: E402 - tools/ 已在 sys.path

    assert "BOK_TTS_PREWARM" in bok.env._FORWARD_ENV, "新 env 开关必须进 _FORWARD_ENV"
