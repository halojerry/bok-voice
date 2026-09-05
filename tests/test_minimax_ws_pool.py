"""P1.5 FIX 3: MiniMax 热连接池(keep-warm) + 首包分解 instrument 单测（fake WS，无网络）。

官方 t2a_v2 WS 一连接一任务（task_finish 后服务端关连接）→ 连接复用做唔到；
池放「处女连接」（只 connect、唔 task_start），合成取用免握手，任何异常回退
流内自连（旧行为）。MINIMAX_WS_POOL=0 关闭。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "agent"))

from agent_runtime.providers import livekit_plugins as lp  # noqa: E402
from agent_runtime.providers.livekit_plugins import MiniMaxTTS  # noqa: E402


class _FakeWS:
    """鸭型 WS：recv 按脚本回消息；send 记录；可配「死连接」模式。"""

    def __init__(self, script: list[str] | None = None, dead: bool = False):
        self._script = list(script or [])
        self._dead = dead
        self.sent: list[dict] = []
        self.closed = False

    async def recv(self):
        if self._dead:
            raise ConnectionResetError("fake dead ws")
        if self._script:
            return self._script.pop(0)
        await asyncio.Event().wait()  # 挂起：等 _task.cancel 收尾

    async def send(self, payload):
        if self._dead:
            raise ConnectionResetError("fake dead ws")
        import json

        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


_CONNECTED = '{"event": "connected_success"}'
_STARTED = '{"event": "task_started"}'
# 2000 字节 PCM（24k 16bit）：≥ 首推门槛 frame_bytes//5=1920，触到 PERF 打点
_AUDIO = '{"data": {"audio": "' + "00" * 2000 + '"}}'


class _FakeConnect:
    def __init__(self, ws: _FakeWS):
        self.ws = ws
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        return self.ws


@pytest.fixture(autouse=True)
def _reset_pool(monkeypatch):
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", None)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", None)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", 0.0)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_TASK", None)
    monkeypatch.setenv("MINIMAX_WS_POOL", "1")
    yield


def _make_tts():
    return MiniMaxTTS(
        voice={"zh": "male-qn-qingse", "cantonese": "Cantonese_crisp_news_anchor_vv2"},
        sample_rate=24000,
        api_key="test-key",
    )


def test_pool_hit_skips_connect_and_replenishes(monkeypatch):
    """池有热连接 → 合成不再 websockets.connect；收尾后台补池成功。"""
    hot = _FakeWS([_CONNECTED, _STARTED])
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", hot)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", (lp.MiniMaxTTS._ENDPOINT_WS_CN, "test-key"))
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", __import__("time").monotonic())
    fake_connect = _FakeConnect(_FakeWS())
    monkeypatch.setattr("websockets.connect", fake_connect)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = tts.stream()
        s.push_text("你好。")
        s.end_input()
        for _ in range(100):
            if [m for m in hot.sent if m.get("event") == "task_continue"]:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)  # 让 recv_loop/收尾跑到可断言的状态
        s._task.cancel()
        await asyncio.sleep(0.2)  # 让取消传播：_run finally 补池
        # 收尾 finally 已 schedule 补池 → 等 replenish 完成
        for _ in range(100):
            if lp._MINIMAX_POOL_TASK is None:
                break
            await asyncio.sleep(0.05)
        return fake_connect.calls

    calls = asyncio.run(asyncio.wait_for(run(), timeout=10))
    # 池命中：合成路径零 connect；唯一一次 connect 来自收尾后的补池
    assert calls == 1, f"合成应复用热连接(0 connect),补池 1 次: {calls}"
    assert [m["event"] for m in hot.sent][:2] == ["task_start", "task_continue"]
    assert lp._MINIMAX_POOL_WS is not None, "收尾后池应有一条新热连接"
    assert lp._MINIMAX_POOL_KEY == (lp.MiniMaxTTS._ENDPOINT_WS_CN, "test-key")


def test_pool_stale_falls_back_to_fresh(monkeypatch, capsys):
    """池连接已被服务端静默关闭 → 弃池重连一次，合成照常出（failure-safe）。"""
    stale = _FakeWS(dead=True)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", stale)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", (lp.MiniMaxTTS._ENDPOINT_WS_CN, "test-key"))
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", __import__("time").monotonic())
    fresh = _FakeWS([_CONNECTED, _STARTED])
    fake_connect = _FakeConnect(fresh)
    monkeypatch.setattr("websockets.connect", fake_connect)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = tts.stream()
        s.push_text("你好。")
        s.end_input()
        for _ in range(100):
            if [m for m in fresh.sent if m.get("event") == "task_continue"]:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        s._task.cancel()
        await asyncio.sleep(0.2)
        for _ in range(100):
            if lp._MINIMAX_POOL_TASK is None:
                break
            await asyncio.sleep(0.05)

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    out = capsys.readouterr().out
    assert "MINIMAX_TTS_WS_POOL_STALE" in out
    # 1 次弃池回退全新连接 + 收尾补池 1 次 = 2
    assert fake_connect.calls == 2, f"弃池回退 1 次 + 补池 1 次: {fake_connect.calls}"
    assert [m["event"] for m in fresh.sent if m.get("event") == "task_continue"]
    assert stale.closed, "死池连接应收尾关闭"


def test_pool_disabled_env_always_fresh(monkeypatch):
    """MINIMAX_WS_POOL=0 → 恒走流内自连（旧行为），池里有毒连接也唔会取。"""
    monkeypatch.setenv("MINIMAX_WS_POOL", "0")
    poison = _FakeWS(dead=True)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", poison)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", (lp.MiniMaxTTS._ENDPOINT_WS_CN, "test-key"))
    fresh = _FakeWS([_CONNECTED, _STARTED])
    fake_connect = _FakeConnect(fresh)
    monkeypatch.setattr("websockets.connect", fake_connect)

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = tts.stream()
        s.push_text("你好。")
        s.end_input()
        for _ in range(100):
            if [m for m in fresh.sent if m.get("event") == "task_continue"]:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        s._task.cancel()
        await asyncio.sleep(0.2)
        for _ in range(20):
            if lp._MINIMAX_POOL_TASK is None:
                break
            await asyncio.sleep(0.05)

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert fake_connect.calls == 1, "开关关 → 直接流内自连"
    assert lp._MINIMAX_POOL_WS is poison, "开关关唔补池、唔动池"


def test_pool_pop_rejects_key_mismatch_and_ttl(monkeypatch):
    """key/endpoint 变更或超龄（TTL 240s）→ 弃用，返回 None。"""
    import time

    ws = _FakeWS()
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", ws)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", ("wss://old", "key-a"))
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", time.monotonic())
    assert lp._minimax_pool_pop("wss://new", "key-a") is None  # endpoint 变
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", ws)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", ("wss://new", "key-a"))
    assert lp._minimax_pool_pop("wss://new", "key-b") is None  # key 变
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", ws)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", ("wss://new", "key-a"))
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", time.monotonic() - 241)
    assert lp._minimax_pool_pop("wss://new", "key-a") is None  # 超龄
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", ws)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", ("wss://new", "key-a"))
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", time.monotonic())
    assert lp._minimax_pool_pop("wss://new", "key-a") is ws  # 正常命中


def test_perf_breakdown_marker(monkeypatch, capsys):
    """首包分解打点：TTS_FIRST_AUDIO_MS + MINIMAX_TTS_WS_PERF(connect/首音频/全程)。"""
    ws = _FakeWS([_CONNECTED, _STARTED, _AUDIO])
    monkeypatch.setattr(lp, "_MINIMAX_POOL_WS", ws)
    monkeypatch.setattr(lp, "_MINIMAX_POOL_KEY", (lp.MiniMaxTTS._ENDPOINT_WS_CN, "test-key"))
    monkeypatch.setattr(lp, "_MINIMAX_POOL_AT", __import__("time").monotonic())
    monkeypatch.setattr("websockets.connect", _FakeConnect(_FakeWS()))

    tts = _make_tts()

    async def run():
        from livekit.agents import APIConnectOptions

        s = tts.stream()
        s.push_text("你好。")
        s.end_input()
        for _ in range(100):
            if [m for m in ws.sent if m.get("event") == "task_continue"]:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)  # 让 recv_loop 消费音频、打出 PERF 行
        s._task.cancel()
        await asyncio.sleep(0.2)
        for _ in range(20):
            if lp._MINIMAX_POOL_TASK is None:
                break
            await asyncio.sleep(0.05)

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    out = capsys.readouterr().out
    assert "TTS_FIRST_AUDIO_MS" in out
    line = [l for l in out.splitlines() if "MINIMAX_TTS_WS_PERF" in l]
    assert line, "应打出首包分解 PERF 行"
    assert "pool=hit" in line[0] and "ws_connect_ms=0" in line[0]
    assert "task_start_to_audio_ms=" in line[0] and "total_ms=" in line[0]
