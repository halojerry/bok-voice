"""MiniMax TTS 打断竞态崩修复(W1a) + classic WS 池并行预热(W1b) 单测。

W1a 崩形(livekit-agents 1.8.2 锁版,生产 2026-09-22 ×342 / 09-23 ×912 /
10-06 ×9):流在「首音频尚未产出」时以**零音频正常返回**(打断竞态下输入通道被
先闭/服务端哑火/连接死亡 2201)→ 框架 ``SynthesizeStream._main_task`` 收尾
无条件调 ``output_emitter.end_input()``(tts.py:601)→
RuntimeError("AudioEmitter isn't started")(tts.py:1008-1010)→ 经 ``__anext__``
上抛 → FallbackAdapter 误判主档死亡「switching to next TTS」→ backup 冷连
(实测 ws_connect_ms≈2468)= 4-7s 黑窗,主档被无谓下线。
修复=零音频正常收尾前垫 ~20ms 静音把 emitter 正常启动
(MINIMAX_TTS_ZERO_AUDIO_PAD);beep 路径同款自启(_qwen3_tts_beep 先例)。
取消(cancel)路径保持 re-raise:任务以 cancelled 收场时框架 ``__anext__`` 走
``task.cancelled()`` 分支回 StopAsyncIteration=消费者视角干净收尾,绝唔触
框架 end_input()——吞掉取消反而令 _run「正常返回」+零音频=崩形本体。

W1b:bidi 主档 ``prewarm()``(会话装配单点)并行暖 1 条 classic 池连接,主档异常
切 backup(classic 档)时 pool=hit 免冷握手段。池深仍 1、单飞幂等、双闸
(BOK_TTS_PREWARM / MINIMAX_WS_POOL)沿用。

全部 fake WS,零网络。镜像 tests/test_minimax_bidi.py 的 fake 基建。
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
from agent_runtime.providers.livekit_plugins import FakeLiveKitTTS, MiniMaxTTS  # noqa: E402


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


class _QueueWS(_FakeWS):
    """测试侧可随时注入服务端消息的 fake WS(控制「零音频收尾」时序)。"""

    def __init__(self):
        super().__init__()
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self.server_push(_CONNECTED)
        self.server_push(_STARTED)

    def server_push(self, raw: str) -> None:
        self._q.put_nowait(raw)

    async def recv(self):
        return await self._q.get()


class _RecvDieWS(_FakeWS):
    """脚本耗尽后 recv 直接抛错(模拟服务端断连)。"""

    async def recv(self):
        if self._script:
            return self._script.pop(0)
        raise ConnectionError("server gone")


class _FakeConnect:
    """排队发 fake WS;记录 connect 次数。"""

    def __init__(self, sockets: list):
        self._sockets = list(sockets)
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        return self._sockets.pop(0)


@pytest.fixture(autouse=True)
def _bidi_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    monkeypatch.delenv("MINIMAX_WS_URL", raising=False)
    monkeypatch.setenv("MINIMAX_REGION", "cn")
    monkeypatch.setenv("MINIMAX_LANGUAGE_BOOST", "")
    monkeypatch.setenv("MINIMAX_PAUSE", "0")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    # 看门狗默认 6s:零音频场景会被判僵死重连搅局,本文件统一关。
    monkeypatch.setenv("MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S", "0")
    yield


@pytest.fixture()
def _clean_pool():
    """classic 池是模块级单例态:用前清空、用后弃置,防测试间串池。"""
    lp._MINIMAX_POOL_WS = None
    lp._MINIMAX_POOL_KEY = None
    lp._MINIMAX_POOL_AT = 0.0
    lp._MINIMAX_POOL_TASK = None
    yield
    ws = lp._MINIMAX_POOL_WS
    lp._MINIMAX_POOL_WS = None
    lp._MINIMAX_POOL_KEY = None
    lp._MINIMAX_POOL_AT = 0.0
    lp._MINIMAX_POOL_TASK = None
    if ws is not None:
        lp._minimax_pool_discard(ws)


def _make_tts() -> MiniMaxTTS:
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


def _continue_texts(ws: _FakeWS) -> list[str]:
    return [m.get("text") for m in ws.sent if m.get("event") == "task_continue"]


def _events(ws: _FakeWS) -> list[str]:
    return [m.get("event") for m in ws.sent]


# ---- W1a:零音频正常收尾唔再炸 -------------------------------------------------


def test_bidi_zero_audio_finish_no_runtimeerror(monkeypatch, capsys):
    """核心复现(W1a):推文本→end_input→服务端只回 flushed 零音频 → 流正常收尾。

    旧行为:_run 零音频返回 → 框架 _main_task 收尾 end_input() 在未启动 emitter
    上抛 RuntimeError("AudioEmitter isn't started") → task.exception() 非空 →
    FallbackAdapter 误切 backup(生产 4-7s 黑窗根因)。
    新行为:收尾前垫 ~20ms 静音(MINIMAX_TTS_ZERO_AUDIO_PAD),任务无异常,
    消费者收到全零音频段。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好。")
        assert await _wait_for(lambda: len(_continue_texts(ws)) == 1), _continue_texts(ws)
        s.end_input()
        assert await _wait_for(lambda: "task_flush" in _events(ws)), _events(ws)
        ws.server_push(_FLUSHED)  # 服务端只 ack,零音频
        assert await _wait_for(lambda: s._task.done(), timeout=10)
        got = bytearray()
        async for ev in s:  # 消费者视角:正常拿到音频段,绝唔抛 RuntimeError
            got += bytes(ev.frame.data)
        return bytes(got)

    audio = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert audio, "零音频收尾应垫出静音段"
    assert set(audio) <= {0}, f"垫的应是静音: 非零字节 {len([b for b in audio if b])}"
    out = capsys.readouterr().out
    assert "MINIMAX_TTS_ZERO_AUDIO_PAD" in out, "零音频垫段应打点"


def test_bidi_cancel_before_first_audio_clean(monkeypatch, capsys):
    """打断(cancel)发生在首音频前:task_cancel 照发、连接保留、消费者干净收尾
    (StopAsyncIteration 等价),绝唔向外传播 RuntimeError。"""
    monkeypatch.setenv("MINIMAX_BIDI_CANCEL_WAIT_S", "0.2")  # 快速超时,测试提速
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好。")
        assert await _wait_for(lambda: len(_continue_texts(ws)) >= 1), _continue_texts(ws)
        s._task.cancel()  # 模拟框架 barge-in cancel(首音频未产出)
        await _wait_for(lambda: s._task.done(), timeout=10)
        got = bytearray()
        async for ev in s:  # cancelled 任务 → __anext__ 走 StopAsyncIteration 分支
            got += bytes(ev.frame.data)
        return s, bytes(got)

    s, audio = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert s._task.cancelled(), "取消路径任务应以 cancelled 收场(干净收尾契约)"
    assert s._task.exception() is None if not s._task.cancelled() else True
    assert "task_cancel" in _events(ws), f"打断应发 task_cancel: {_events(ws)}"
    assert not ws.closed, "打断后连接应保留给下一轮"
    assert "AudioEmitter" not in capsys.readouterr().out


def test_bidi_zero_audio_via_fallback_adapter_no_switch(monkeypatch):
    """端到端崩形验证:零音频收尾经真 FallbackAdapter 唔触发「switching to next
    TTS」、主档 availability 保持 True、消费者拿到音频。旧行为在呢一步炸出
    RuntimeError → 主档被下线 → backup 冷连=4-7s 黑窗。"""
    ws = _QueueWS()
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    primary = _make_tts()
    adapter = lp.tts.FallbackAdapter(tts=[primary, FakeLiveKitTTS(sample_rate=16000)])

    async def run():
        s = adapter.stream()
        s.push_text("你好。")
        s.end_input()
        assert await _wait_for(lambda: "task_flush" in _events(ws)), _events(ws)
        ws.server_push(_FLUSHED)
        assert await _wait_for(lambda: primary._bidi_session() and _continue_texts(ws))
        got = bytearray()
        async for ev in s:
            got += bytes(ev.frame.data)
        await _wait_for(lambda: s._task.done(), timeout=10)
        return bytes(got)

    audio = asyncio.run(asyncio.wait_for(run(), timeout=20))
    assert audio, "FallbackAdapter 应收到主档垫出的静音段"
    assert adapter._status[0].available, "零音频干净收尾后主档唔应被 FallbackAdapter 下线"


def test_bidi_missing_credentials_beep_boots_emitter(monkeypatch, capsys):
    """缺凭据→beep:旧行为 beep push 喺未启动 emitter 上抛并被子句吞掉(beep
    从未播出)+框架收尾再炸;新行为 beep 自启 emitter(_qwen3_tts_beep 先例),
    任务干净完结、消费者收到 beep 音频。"""
    tts = MiniMaxTTS(voice="male-qn-qingse", sample_rate=24000, api_key="")

    async def run():
        s = tts.stream()
        s.push_text("你好。")
        s.end_input()
        await _wait_for(lambda: s._task.done(), timeout=10)
        got = bytearray()
        async for ev in s:
            got += bytes(ev.frame.data)
        return bytes(got)

    audio = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert "MINIMAX_TTS_MISSING_CREDENTIALS" in capsys.readouterr().out
    assert audio, "beep 应回退出声(emitter 已被 beep 自行启动)"
    assert any(b != 0 for b in audio), "beep 应係 440Hz 非静音"


def test_classic_zero_audio_finish_no_runtimeerror(monkeypatch, capsys):
    """classic 档(备档 speech-2.6-turbo 走的路)同款零音频收尾:服务端断连 →
    垫静音干净完结,任务无异常(旧行为同炸 RuntimeError)。"""
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")
    ws = _RecvDieWS([_CONNECTED, _STARTED])
    monkeypatch.setattr("websockets.connect", _FakeConnect([ws]))
    tts = _make_tts()

    async def run():
        s = tts.stream()
        s.push_text("你好。")
        s.end_input()
        assert await _wait_for(lambda: s._task.done(), timeout=10)
        got = bytearray()
        async for ev in s:
            got += bytes(ev.frame.data)
        return bytes(got)

    audio = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert audio, "classic 零音频收尾应垫出静音段"
    assert set(audio) <= {0}, f"垫的应是静音: 非零字节 {len([b for b in audio if b])}"
    out = capsys.readouterr().out
    assert "MINIMAX_TTS_ZERO_AUDIO_PAD" in out


def test_chunked_missing_credentials_beep_boots_emitter(monkeypatch, capsys):
    """synthesize() 整段路径(缺凭据):beep 自启 emitter(stream=False 口径),
    ChunkedStream._main_task 收尾 end_input 不炸,collect() 干净返回。"""
    tts = MiniMaxTTS(voice="male-qn-qingse", sample_rate=24000, api_key="")

    async def run():
        s = tts.synthesize("你好")
        return await s.collect()

    frame = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert "MINIMAX_TTS_MISSING_CREDENTIALS" in capsys.readouterr().out
    assert frame is not None and frame.data, "beep 应回退出声"


# ---- W1b:bidi 主档装配时并行暖 classic 池 --------------------------------------


def test_bidi_prewarm_also_warms_classic_pool(monkeypatch, _clean_pool, capsys):
    """W1b:bidi 主档 prewarm()(会话装配单点)后,classic 池内应有 1 条预热线
    (并行、唔阻塞 bidi 预热本体);两连接各自 fake WS 服务。

    池 socket 排前:pool replenish 任务先 create(先入 ready 队列先跑)先 pop;
    两个 fake 都给全脚本,连 pop 顺序有变都唔会令 bidi 握手挂在空脚本上。"""
    del capsys
    bidi_ws = _FakeWS([_CONNECTED, _STARTED])
    pool_ws = _FakeWS([_CONNECTED, _STARTED])
    fake_connect = _FakeConnect([pool_ws, bidi_ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        ok = await tts.prewarm()
        assert ok, "bidi 主档预热应成功"
        assert tts._bidi_session()._alive(), "bidi 持久会话应已连上"
        assert await _wait_for(
            lambda: lp._minimax_pool_ready(tts._endpoint_ws(), tts._api_key())
        ), "装配后 classic 池内应有 1 条预热线(并行预热)"
        # 池深=1:预热线在池,未取用
        assert lp._MINIMAX_POOL_WS is pool_ws

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert fake_connect.calls == 2, f"bidi 会话 + 池预热线 = 两次 connect: {fake_connect.calls}"


def test_bidi_prewarm_pool_respects_total_gate(monkeypatch, _clean_pool):
    """BOK_TTS_PREWARM=0 总闸:prewarm() 立即 False,classic 池零动作(旧行为)。"""
    monkeypatch.setenv("BOK_TTS_PREWARM", "0")
    fake_connect = _FakeConnect([_FakeWS([_CONNECTED, _STARTED]), _FakeWS()])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        ok = await tts.prewarm()
        assert ok is False, "总闸关=prewarm 立即 False"
        await asyncio.sleep(0.1)
        assert not lp._minimax_pool_ready(tts._endpoint_ws(), tts._api_key())
        assert fake_connect.calls == 0, "总闸关唔应发起任何连接"

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_bidi_prewarm_pool_respects_pool_gate(monkeypatch, _clean_pool):
    """MINIMAX_WS_POOL=0:bidi 会话照常预热(True),classic 池零动作。"""
    monkeypatch.setenv("MINIMAX_WS_POOL", "0")
    bidi_ws = _FakeWS([_CONNECTED, _STARTED])
    fake_connect = _FakeConnect([bidi_ws])
    monkeypatch.setattr("websockets.connect", fake_connect)
    tts = _make_tts()

    async def run():
        ok = await tts.prewarm()
        assert ok is True, "bidi 主档预热唔受池开关影响"
        assert tts._bidi_session()._alive()
        await asyncio.sleep(0.1)
        assert not lp._minimax_pool_ready(tts._endpoint_ws(), tts._api_key())
        assert fake_connect.calls == 1, "池开关关唔应为池发起连接"

    asyncio.run(asyncio.wait_for(run(), timeout=10))
