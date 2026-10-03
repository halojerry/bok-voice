"""Bok patch: MiniMax t2a_v2_bidi 流式 TTS handler（官方 WS 协议）。

协议实现从我们生产 livekit 插件（apps/agent/agent_runtime/providers/
livekit_plugins.py MiniMaxTTS/_MiniMaxBidiSession）搬入本宿主。spike 版为
协议核心（连接/task_start/task_continue/task_flush/hex-PCM 解码/task_flushed
收尾）；生产版的加固件（60s 自管 ping/纪元门禁/限流熔断/合成级预热）未随迁,
生产化时按 livekit_plugins.py 同源补齐。

官方协议要点（生产实弹验证过的语义）：
- ``wss://api.minimax.cn|chat/ws/v1/t2a_v2_bidi``，Bearer 鉴权，
  ``ping_interval=None``（客户端自管 ping——spike 版短会话暂不需要）
- 连接后收 ``connected_success``（丢帧不致命）→ ``task_start``（voice_setting
  /audio_setting/stream_options.exclude_aggregated_audio）→ ``task_started``
- 文本块：``{"event":"task_continue","text":...}``（顶层 text 字段）
- 应答收尾：``{"event":"task_flush"}`` 催产；收 ``task_flushed`` 止
- 音频：事件 ``data.audio`` 为 hex PCM，按 audio_setting.sample_rate 出
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

import numpy as np

from speech_to_speech.pipeline.handler_types import TTSIn, TTSOut
from speech_to_speech.pipeline.messages import EndOfResponse, TTSInput
from speech_to_speech.baseHandler import BaseHandler as _Base

logger = logging.getLogger(__name__)

_ENDPOINT_CN = "wss://api.minimax.cn/ws/v1/t2a_v2_bidi"
_ENDPOINT_INTL = "wss://api.minimax.chat/ws/v1/t2a_v2_bidi"
_PIPELINE_SR = 16000
_OPEN_TIMEOUT_S = 10.0
_START_TIMEOUT_S = 15.0
_FLUSH_TIMEOUT_S = 10.0


def _ws_host_ok(url: str) -> bool:
    """SSRF 护栏：仅 wss/https 公网端点。"""
    try:
        parsed = urlsplit(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("wss", "https"):
        return False
    host = (parsed.hostname or "").lower().strip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith((".local", ".internal", ".lan", ".localhost")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return bool(ip.is_global)


def _pcm_to_pipeline(pcm: bytes, sr: int) -> np.ndarray:
    arr = np.frombuffer(pcm, dtype=np.int16)
    if sr == _PIPELINE_SR:
        return arr
    gcd = np.gcd(_PIPELINE_SR, sr)
    up, down = _PIPELINE_SR // gcd, sr // gcd
    f = arr.astype(np.float32) / 32768.0
    from scipy.signal import resample_poly

    r = resample_poly(f, up, down)
    return np.clip(r * 32768, -32768, 32767).astype(np.int16)


class _LoopThread:
    """私有事件循环线程（同步 handler 契约内跑 asyncio websockets）。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._loop is not None:
            return

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._ready.set()
            loop.run_forever()

        threading.Thread(target=_run, daemon=True, name="bok-minimax-tts-loop").start()
        self._ready.wait(5.0)

    def call(self, coro, timeout: float):
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None


class MiniMaxBidiTTSHandler(_Base):
    """MiniMax bidi 流式 TTS（Bok 自有云 TTS；连接整会话复用）。"""

    def setup(
        self,
        api_key: str = "",
        voice: str = "Cantonese_crisp_news_anchor_vv2",
        model: str = "speech-2.8-hd",
        region: str = "cn",
        endpoint: str = "",
        sample_rate: int = 16000,
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
        **_kwargs: Any,
    ) -> None:
        import os

        self.api_key = api_key or os.environ.get("MINIMAX_TTS_API_KEY", "")
        self.voice = voice
        self.model = model or "speech-2.8-hd"
        self.endpoint = endpoint or (_ENDPOINT_CN if region != "intl" else _ENDPOINT_INTL)
        if not _ws_host_ok(self.endpoint):
            raise ValueError(f"minimax_tts_endpoint 非公网 wss/https 端点: {self.endpoint}")
        if not self.api_key:
            raise ValueError("minimax TTS 需要 api_key（或 env MINIMAX_TTS_API_KEY）")
        self.out_sr = int(sample_rate) if int(sample_rate) in (8000, 16000, 24000) else 16000
        self.voice_setting = {"voice_id": self.voice, "speed": float(speed),
                              "vol": float(vol), "pitch": int(pitch)}
        self._loop_thread = _LoopThread()
        self._loop_thread.start()
        self._ws = None
        logger.info("MiniMaxBidiTTSHandler ready (voice=%s model=%s sr=%d)",
                    self.voice, self.model, self.out_sr)

    # ---- 连接生命周期 ----

    def _task_start_payload(self) -> dict:
        return {
            "event": "task_start",
            "model": self.model,
            "voice_setting": self.voice_setting,
            "audio_setting": {"sample_rate": self.out_sr, "format": "pcm", "channel": 1},
            "stream_options": {"exclude_aggregated_audio": True},
        }

    async def _connect(self):
        import websockets

        ws = await websockets.connect(
            self.endpoint,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            open_timeout=_OPEN_TIMEOUT_S,
            max_size=20_000_000,
            ping_interval=None,
        )
        try:
            await asyncio.wait_for(ws.recv(), timeout=_OPEN_TIMEOUT_S)  # connected_success
        except Exception:  # noqa: BLE001
            pass
        await ws.send(json.dumps(self._task_start_payload()))
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=_START_TIMEOUT_S))
        if resp.get("event") != "task_started":
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"minimax bidi task_start failed: {str(resp)[:200]}")
        return ws

    def _ensure_open(self):
        if self._ws is None:
            self._ws = self._loop_thread.call(self._connect(), timeout=_START_TIMEOUT_S + 5.0)
        return self._ws

    async def _drain(self, ws, *, flush_wait: bool) -> list[np.ndarray]:
        """收音频；flush_wait=True 时等到 task_flushed（阻塞收尾），否则尽力收。"""
        out: list[np.ndarray] = []
        deadline = time.monotonic() + _FLUSH_TIMEOUT_S
        while True:
            remain = deadline - time.monotonic()
            if flush_wait:
                if remain <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remain)
                except asyncio.TimeoutError:
                    break
            else:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
                except asyncio.TimeoutError:
                    break
            try:
                ev = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            et = ev.get("event", "")
            if et == "error":
                logger.warning("minimax bidi error event: %s", str(ev)[:200])
                continue
            data = ev.get("data") or {}
            hex_audio = data.get("audio") or ""
            if hex_audio:
                out.append(_pcm_to_pipeline(bytes.fromhex(hex_audio), self.out_sr))
            if et == "task_flushed":
                break
        return out

    # ---- handler 契约 ----

    def process(self, tts_input: TTSIn) -> Iterator[TTSOut]:
        ws = None
        try:
            ws = self._ensure_open()
        except Exception as exc:  # noqa: BLE001 - 连接失败：本响应放弃出声（下轮重连）
            logger.warning("minimax bidi connect failed: %r", exc)
            self._ws = None
            return

        if isinstance(tts_input, EndOfResponse):
            try:
                self._loop_thread.call(_send_flush(ws), timeout=3.0)
                for chunk in self._loop_thread.call(self._drain(ws, flush_wait=True), timeout=_FLUSH_TIMEOUT_S + 3.0):
                    yield chunk
            except Exception as exc:  # noqa: BLE001
                logger.warning("minimax bidi flush failed: %r", exc)
            return

        if not isinstance(tts_input, TTSInput):
            return
        text = str(tts_input.text or "").strip()
        if not text:
            return
        try:
            self._loop_thread.call(_send_continue(ws, text), timeout=3.0)
            for chunk in self._loop_thread.call(self._drain(ws, flush_wait=False), timeout=1.0):
                yield chunk
        except Exception as exc:  # noqa: BLE001 - 喂包失败：本响应放弃（下轮重连）
            logger.warning("minimax bidi continue failed: %r", exc)
            try:
                self._loop_thread.call(_close(self._ws), timeout=3.0)
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    def on_session_end(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                self._loop_thread.call(_close(ws), timeout=3.0)
            except Exception:  # noqa: BLE001
                pass

    def cleanup(self) -> None:
        self.on_session_end()
        self._loop_thread.stop()


async def _send_continue(ws, text: str) -> None:
    await ws.send(json.dumps({"event": "task_continue", "text": text}))


async def _send_flush(ws) -> None:
    await ws.send(json.dumps({"event": "task_flush"}))


async def _close(ws) -> None:
    if ws is not None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
