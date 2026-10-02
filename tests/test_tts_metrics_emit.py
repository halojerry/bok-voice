"""TTS provider 流的 tts_metrics emit 契约（2026-10-02 DR 栈 TTS 行灰根因修复）。

**根因**（livekit 1.8.2 `.venv312/.../livekit/agents/tts/tts.py` 源码实读）：
``SynthesizeStream._metrics_monitor_task`` 的收尾 emit（``_emit_metrics``）首闸
就是 ``if not self._started_time: return``——而 ``_started_time`` 只由子类在
「首段文本交给 provider」时调 ``_mark_started()`` 设置。官方两处范本
（``tts/stream_adapter.py`` 首个 token、``inference/tts.py`` 首包发送前）都是
这么做的；``FallbackSynthesizeStream`` 甚至只把**子流**的 ``_started_time``
透传上来（``_capture_started_time``），即子流自身负责标。

我们三个 provider 流（``_MiniMaxBidiStream`` / ``_MiniMaxSynthesizeStream`` /
``_Qwen3SynthesizeStream``）此前从不调用 ``_mark_started()``，因此：
- 自身的 tts_metrics 结构性为零（监视器任务在跑、事件在流，但 emit 被闸掉）；
- MiniMax 链只是**碰巧**被 ``tts_cache._RelaySynthesizeStream`` 兜了一层
  （锚点=首帧转发时刻，非首送；被填充垫话 hold 拉长，打断轮整条不发）；
- 本地 Qwen3 车道装配为**裸 provider**（agent.py 只在 ``_tts_primary is not
  None``=MiniMax 时才包 Relay/_FirstAudioTTS）→ 整通零 tts_metrics →
  CP Provider 卡 TTS 行恒灰（``tts_first_audio`` 样本 n=0）。

本文件：①用真 provider 流 + 假 WS/假 POST 跑完整轮，断言 metrics_collected
真在 provider 对象上 emit 且字段口径对；②ttfb 锚点=「首段文本发出」（音频延迟
0.3s → ttfb≥0.25，旧的 Relay 锚点做不到）；③源级 pin 三个首送点；
④基座契约 pin（防上游升级把闸去掉后我们误以为还在生效）。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path

import pytest

import agent_runtime.providers.livekit_plugins as lp
from agent_runtime.providers.livekit_plugins import (
    MiniMaxTTS,
    Qwen3TTSTTS,
)

LP_PATH = Path(lp.__file__)


def _pcm(seconds: float, sample_rate: int = 24000) -> bytes:
    """s16le mono 正弦 PCM（非静音：classic 首包前导静音修剪_real_ 会裁静音）。"""
    import math

    n = int(sample_rate * seconds)
    out = bytearray()
    for i in range(n):
        value = int(6000 * math.sin(2 * math.pi * 440 * i / sample_rate))
        out += value.to_bytes(2, "little", signed=True)
    return bytes(out)


class _FakeBidiWS:
    """bidi 流假 WS：握手应答 + task_continue→回音频 + task_flush→task_flushed。

    ``recv`` 无消息时挂起轮询（真实 WS 语义）；``send`` 内部零 await（不切
    任务，保证 ``_send_text`` 里「先 send 后认领纪元」的次序与真连接一致）。
    """

    def __init__(self, *, pcm: bytes, audio_delay_s: float = 0.0) -> None:
        self.sent: list[dict] = []
        self._pcm = pcm
        self._audio_delay_s = audio_delay_s
        self._pending: list[str] = [json.dumps({"event": "connected_success"})]
        self.closed = False

    async def recv(self):
        while not self._pending:
            await asyncio.sleep(0.01)
        return self._pending.pop(0)

    async def send(self, payload):
        msg = json.loads(payload)
        self.sent.append(msg)
        event = msg.get("event")
        if event == "task_start":
            self._pending.append(json.dumps({"event": "task_started"}))
        elif event == "task_continue":
            audio = json.dumps({"data": {"audio": self._pcm.hex()}, "is_final": True})
            if self._audio_delay_s > 0:
                asyncio.get_running_loop().call_later(
                    self._audio_delay_s, self._pending.append, audio
                )
            else:
                self._pending.append(audio)
        elif event == "task_flush":
            self._pending.append(json.dumps({"event": "task_flushed"}))

    async def close(self):
        self.closed = True

    async def ping(self):
        return None


def _install_fake_connect(monkeypatch, ws):
    async def fake_connect(*_a, **_kw):
        return ws

    monkeypatch.setattr("websockets.connect", fake_connect)


def _bidi_env(monkeypatch):
    # 显式钉 bidi（防开发/CI 环境 MINIMAX_WS_MODE 污染导致测到 classic 车道）。
    monkeypatch.setenv("MINIMAX_WS_MODE", "bidi")
    # 首 chunk 提前切关（走「句界整发」主路径，与 _tts_first_chunk 测试解耦）。
    monkeypatch.setenv("BOK_TTS_FIRST_CHUNK_CHARS", "0")
    # 关首包看门狗（本测试喂音频很快，不需要重连路径参与）。
    monkeypatch.setenv("MINIMAX_BIDI_FIRST_AUDIO_TIMEOUT_S", "0")
    # 关注册 ping（60s 默认会把 loop 挂住到测试结束）。
    monkeypatch.setenv("MINIMAX_BIDI_PING_S", "0")


def _run_bidi(pcm: bytes, audio_delay_s: float = 0.0):
    """跑真 _MiniMaxBidiStream 一轮，返回 (metrics, 推过的文本)。"""
    text = "你好呀。"
    ws = _FakeBidiWS(pcm=pcm, audio_delay_s=audio_delay_s)
    tts = MiniMaxTTS(voice={"zh": "male-qn-qingse"}, sample_rate=24000, api_key="test-key")
    metrics: list = []
    tts.on("metrics_collected", metrics.append)
    return ws, tts, metrics, text


def _drive_bidi(ws, tts, text) -> None:
    async def _run():
        stream = tts.stream()
        assert type(stream).__name__ == "_MiniMaxBidiStream", "bidi 车道未生效"
        stream.push_text(text)
        stream.end_input()
        async for _ev in stream:
            pass
        await stream.aclose()  # aclose 内部 await _metrics_task → emit 落地后才返回

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# ① 真 bidi 流 + 假 WS：完整轮必须 emit 一条 tts_metrics，字段口径对
# ---------------------------------------------------------------------------


def test_bidi_stream_emits_provider_tts_metrics(monkeypatch):
    _bidi_env(monkeypatch)
    ws, tts, metrics, text = _run_bidi(pcm=_pcm(0.6))
    _install_fake_connect(monkeypatch, ws)
    _drive_bidi(ws, tts, text)

    assert len(metrics) == 1, f"bidi 流应 emit 恰好 1 条 TTSMetrics，实际 {len(metrics)}"
    m = metrics[0]
    assert m.streamed is True
    assert m.cancelled is False
    assert "MiniMaxTTS" in m.label
    assert m.characters_count == len(text)
    # audio_duration ≈ 推入 PCM 时长（0.6s；无损音量=尾部 10ms hold 会在 is_final 一起发）
    assert abs(m.audio_duration - 0.6) < 0.05, f"audio_duration={m.audio_duration}"
    # ttfb = 首送→首帧（真合成档几百 ms 级）；此假 WS 秒回 → 近 0 但非负。
    assert 0.0 <= m.ttfb < 0.5, f"ttfb={m.ttfb}"
    # 首送发的是 task_continue（不是 task_start/flush）
    assert any(msg.get("event") == "task_continue" for msg in ws.sent)


def test_bidi_ttfb_anchored_at_first_provider_send(monkeypatch):
    """ttfb 锚点=首段文本发出，不是首帧转发。

    假 WS 延迟 0.3s 才回音频：若锚点错挂在「首帧到达」（旧 Relay 层口径），
    ttfb 会≈0；正确锚点（_mark_started 在首条 task_continue）ttfb≥0.25。
    """
    _bidi_env(monkeypatch)
    ws, tts, metrics, text = _run_bidi(pcm=_pcm(0.4), audio_delay_s=0.3)
    _install_fake_connect(monkeypatch, ws)
    _drive_bidi(ws, tts, text)

    assert len(metrics) == 1
    assert metrics[0].ttfb >= 0.25, f"ttfb={metrics[0].ttfb} 未把首送→首帧等待计入"


def test_bidi_stream_multiple_chunks_single_metric(monkeypatch):
    """多块输入（多次 task_continue）仍只 emit 一条：整轮=一个 segment 一个样本。"""
    _bidi_env(monkeypatch)
    ws, tts, metrics, text = _run_bidi(pcm=_pcm(0.4))
    _install_fake_connect(monkeypatch, ws)

    async def _run():
        stream = tts.stream()
        stream.push_text(text[:2])
        stream.push_text(text[2:])
        stream.end_input()
        async for _ev in stream:
            pass
        await stream.aclose()

    asyncio.run(_run())
    assert len(metrics) == 1, f"整轮只应 1 条 TTSMetrics，实际 {len(metrics)}"
    assert metrics[0].characters_count == len(text)
    # 两块都发出（不是丢块）
    continues = [m for m in ws.sent if m.get("event") == "task_continue"]
    assert len(continues) >= 1


# ---------------------------------------------------------------------------
# ② 本地 Qwen3 流（裸 provider 车道，无 Relay 兜底）：必须自己 emit
# ---------------------------------------------------------------------------


def test_qwen3_stream_emits_provider_tts_metrics(monkeypatch):
    monkeypatch.setenv("BOK_TTS_FIRST_CLAUSE", "0")
    posted: list[str] = []

    async def fake_post(tts_, text, output_emitter, state, *, end_segment=True):
        posted.append(text)
        if not state.get("started"):
            output_emitter.initialize(
                request_id="r",
                sample_rate=24000,
                num_channels=1,
                mime_type="audio/pcm",
                stream=True,
            )
            output_emitter.start_segment(segment_id="s")
            state["started"] = True
        output_emitter.push(_pcm(0.3))
        output_emitter.flush()
        return True

    monkeypatch.setattr(lp, "_qwen3_tts_post_frames", fake_post)

    tts = Qwen3TTSTTS(base_url="http://127.0.0.1:8788", voice="vivian", sample_rate=24000)
    metrics: list = []
    tts.on("metrics_collected", metrics.append)
    text = "你好呀。"

    async def _run():
        stream = tts.stream()
        assert type(stream).__name__ == "_Qwen3SynthesizeStream", "Qwen3 车道未生效"
        stream.push_text(text)
        stream.end_input()
        async for _ev in stream:
            pass
        await stream.aclose()

    asyncio.run(_run())

    assert posted == [text]
    assert len(metrics) == 1, f"Qwen3 流应 emit 恰好 1 条 TTSMetrics，实际 {len(metrics)}"
    m = metrics[0]
    assert m.streamed is True
    assert m.cancelled is False
    assert "Qwen3TTSTTS" in m.label
    assert m.characters_count == len(text)
    assert abs(m.audio_duration - 0.3) < 0.05
    assert 0.0 <= m.ttfb < 0.5


# ---------------------------------------------------------------------------
# ③ classic MiniMax 流（MINIMAX_WS_MODE=classic）：同契约
# ---------------------------------------------------------------------------


class _FakeClassicWS:
    """classic 流假 WS：握手两 recv + task_continue 回音频 + task_finish 后断开。"""

    def __init__(self, *, pcm: bytes) -> None:
        self.sent: list[dict] = []
        self._pcm = pcm
        self._recv_n = 0
        self._audio_sent = False

    async def recv(self):
        self._recv_n += 1
        if self._recv_n == 1:
            return json.dumps({"event": "connected_success"})
        if self._recv_n == 2:
            return json.dumps({"event": "task_started"})
        if not self._audio_sent:
            self._audio_sent = True
            return json.dumps({"data": {"audio": self._pcm.hex()}, "is_final": True})
        raise Exception("fake ws closed")

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


def test_classic_stream_emits_provider_tts_metrics(monkeypatch):
    monkeypatch.setenv("MINIMAX_WS_MODE", "classic")  # 真走 _MiniMaxSynthesizeStream
    monkeypatch.setenv("MINIMAX_WS_POOL", "0")
    monkeypatch.setenv("MINIMAX_FIRST_AUDIO_TIMEOUT_S", "30")
    monkeypatch.setenv("BOK_TTS_FIRST_CLAUSE", "0")
    ws = _FakeClassicWS(pcm=_pcm(0.4))
    _install_fake_connect(monkeypatch, ws)

    tts = MiniMaxTTS(voice={"zh": "male-qn-qingse"}, sample_rate=24000, api_key="test-key")
    metrics: list = []
    tts.on("metrics_collected", metrics.append)
    text = "你好呀。"

    async def _run():
        stream = tts.stream()
        assert type(stream).__name__ == "_MiniMaxSynthesizeStream", "classic 车道未生效"
        stream.push_text(text)
        stream.end_input()
        async for _ev in stream:
            pass
        await stream.aclose()

    asyncio.run(_run())

    assert len(metrics) == 1, f"classic 流应 emit 恰好 1 条 TTSMetrics，实际 {len(metrics)}"
    m = metrics[0]
    assert m.streamed is True
    assert m.characters_count == len(text)
    assert abs(m.audio_duration - 0.4) < 0.05
    assert any(msg.get("event") == "task_continue" for msg in ws.sent)


# ---------------------------------------------------------------------------
# ④ 源级 pin：三个 provider 流在「首送」点必须调 self._mark_started()
# ---------------------------------------------------------------------------


def _class_method_has_mark_started(cls_name: str, method_name: str) -> bool:
    tree = ast.parse(LP_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            # 方法常是 _run 内的闭包（_send_text/_note_first_send/_mark_first_send）
            # → 在类体子树内按名找 FunctionDef（含嵌套）。
            for item in ast.walk(node):
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    for sub in ast.walk(item):
                        if (
                            isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "_mark_started"
                            and isinstance(sub.func.value, ast.Name)
                            and sub.func.value.id == "self"
                        ):
                            return True
    return False


@pytest.mark.parametrize(
    ("cls_name", "method_name"),
    [
        ("_MiniMaxBidiStream", "_send_text"),
        ("_MiniMaxSynthesizeStream", "_note_first_send"),
        ("_Qwen3SynthesizeStream", "_run"),
    ],
)
def test_provider_stream_marks_started_at_first_send(cls_name, method_name):
    assert _class_method_has_mark_started(cls_name, method_name), (
        f"{cls_name}.{method_name} 缺 self._mark_started()——基座 _emit_metrics 的 "
        "_started_time 闸会把本流 tts_metrics 整条闸掉（CP Provider 卡 TTS 行灰）"
    )


# ---------------------------------------------------------------------------
# ⑤ 基座契约 pin：闸在基座、标在子类（上游升级若改变，本 pin 先红）
# ---------------------------------------------------------------------------


def test_base_synthesize_stream_metrics_gate_contract():
    from livekit.agents.tts import SynthesizeStream

    monitor_src = inspect.getsource(SynthesizeStream._metrics_monitor_task)
    assert "self._started_time" in monitor_src, (
        "livekit 基座契约变了：_metrics_monitor_task 不再以 _started_time 为闸——"
        "复核 provider 流的 _mark_started 接线是否仍需要/是否已可回退"
    )
    push_src = inspect.getsource(SynthesizeStream.push_text)
    assert "_mark_started" not in push_src, (
        "livekit 基座已在 push_text 里自动标 started——我们的子类调用变冗余（可删，"
        "但删前先复核官方语义是否等价于「首段交 provider」）"
    )
