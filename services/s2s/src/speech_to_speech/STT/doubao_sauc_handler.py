"""Bok patch: 豆包（火山引擎）SAUC 流式 ASR handler（官方 V3 二进制帧协议）。

协议实现从我们生产 livekit 插件（apps/agent/agent_runtime/providers/doubao_asr.py，
2026-10-03 实弹验证）原样搬入本宿主；语义保持：

- 一段 VAD 语音 = 一个 WS 会话（progressive 首包开会话，连接时延藏在说话期间）
- result.text 单调累积：FINAL 取「最长已见文本」
- interim 按文本变化去重后发 PartialTranscription
- 末包负 seq 强制定稿；连接失败且无文本 → 整段单发重试一次
- enable_lid 常开（粤语）；热词走 request.corpus.context（前 40 词）
- SSRF 护栏：仅 wss/https + 公网端点
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import logging
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

import numpy as np

from speech_to_speech.pipeline.handler_types import STTIn, STTOut
from speech_to_speech.pipeline.messages import PartialTranscription, Transcription
from speech_to_speech.STT.base_stt_handler import BaseSTTHandler

logger = logging.getLogger(__name__)

DOUBAO_WS_DEFAULT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
DOUBAO_RESOURCE_DEFAULT = "volc.seedasr.sauc.duration"

_PACKET_MS = 200
_SAMPLE_RATE = 16000
_SEG_CAP_BYTES = _SAMPLE_RATE * 2 * 60
_CONNECT_TIMEOUT_S = 8.0
_FINAL_TIMEOUT_S = 6.0

MSG_FULL_CLIENT_REQ = 0b0001
MSG_AUDIO_ONLY_REQ = 0b0010
MSG_FULL_SERVER_RESP = 0b1001
MSG_SERVER_ACK = 0b1011
MSG_ERROR = 0b1111

FLAG_NO_SEQ = 0b0000
FLAG_POS_SEQ = 0b0001
FLAG_LAST_NO_SEQ = 0b0010
FLAG_NEG_SEQ = 0b0011


def _header(msg_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes([
        (1 << 4) | 1,
        (msg_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def frame_full_client_request(payload: dict) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    buf = _header(MSG_FULL_CLIENT_REQ, FLAG_POS_SEQ, serialization=1, compression=1)
    buf += struct.pack(">i", 1)
    buf += struct.pack(">I", len(body)) + body
    return buf


def frame_audio(seq: int, chunk: bytes, *, last: bool) -> bytes:
    body = gzip.compress(chunk)
    if last:
        buf = _header(MSG_AUDIO_ONLY_REQ, FLAG_NEG_SEQ, serialization=0, compression=1)
        buf += struct.pack(">i", -seq)
    else:
        buf = _header(MSG_AUDIO_ONLY_REQ, FLAG_POS_SEQ, serialization=0, compression=1)
        buf += struct.pack(">i", seq)
    buf += struct.pack(">I", len(body)) + body
    return buf


def parse_server_frame(data: bytes) -> dict:
    b0, b1, b2 = data[0], data[1], data[2]
    msg_type = b1 >> 4
    flags = b1 & 0x0F
    compression = b2 & 0x0F
    pos = 4 * (b0 & 0x0F)
    out: dict = {"type": msg_type, "flags": flags, "is_last": bool(flags & 0x02),
                 "seq": None, "event": None, "payload": b"", "code": None}
    if flags & 0x01:
        out["seq"] = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4
    if flags & 0x04:
        out["event"] = struct.unpack(">i", data[pos:pos + 4])[0]
        pos += 4
    if msg_type == MSG_ERROR:
        out["code"] = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
    if msg_type in (MSG_FULL_SERVER_RESP, MSG_SERVER_ACK, MSG_ERROR):
        if pos + 4 <= len(data):
            size = struct.unpack(">I", data[pos:pos + 4])[0]
            pos += 4
            payload = data[pos:pos + size]
            if compression == 1 and payload:
                try:
                    payload = gzip.decompress(payload)
                except Exception:  # noqa: BLE001
                    pass
            out["payload"] = payload
    return out


def _ws_host_ok(url: str) -> bool:
    """SSRF 护栏：仅 wss/https 公网端点（拒环回/私有/保留地址）。"""
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


class _LoopThread:
    """私有事件循环线程：同步 handler 线程内跑 asyncio websockets。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
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

        self._thread = threading.Thread(target=_run, daemon=True, name="bok-doubao-stt-loop")
        self._thread.start()
        self._ready.wait(5.0)

    def call(self, coro, timeout: float):
        assert self._loop is not None, "loop thread not started"
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None


class DoubaoSaucSTTHandler(BaseSTTHandler):
    """豆包 SAUC 双向流式 STT（Bok 自有云 ASR；A 线粤语生产档）。"""

    def setup(
        self,
        api_key: str = "",
        app_id: str = "",
        access_token: str = "",
        resource_id: str = DOUBAO_RESOURCE_DEFAULT,
        ws_url: str = DOUBAO_WS_DEFAULT,
        hotwords: str = "",
        connect_timeout_s: float = _CONNECT_TIMEOUT_S,
        final_timeout_s: float = _FINAL_TIMEOUT_S,
        packet_ms: int = _PACKET_MS,
        **_kwargs: Any,
    ) -> None:
        import os

        self.api_key = api_key or os.environ.get("DOUBAO_STT_API_KEY", "")
        self.app_id = app_id or os.environ.get("DOUBAO_STT_APP_ID", "")
        self.access_token = access_token or os.environ.get("DOUBAO_STT_ACCESS_TOKEN", "")
        self.resource_id = resource_id or DOUBAO_RESOURCE_DEFAULT
        self.ws_url = ws_url or DOUBAO_WS_DEFAULT
        if not _ws_host_ok(self.ws_url):
            raise ValueError(f"doubao_stt_ws_url 非公网 wss/https 端点: {self.ws_url}")
        if not (self.api_key or (self.app_id and self.access_token)):
            raise ValueError("doubao STT 需要 api_key（新版）或 app_id+access_token（旧版）")
        self.hotwords = [w for w in str(hotwords or "").replace(",", " ").split() if w][:40]
        self.connect_timeout_s = float(connect_timeout_s)
        self.final_timeout_s = float(final_timeout_s)
        self.packet_bytes = _SAMPLE_RATE * 2 * max(50, int(packet_ms)) // 1000

        self._loop_thread = _LoopThread()
        self._loop_thread.start()
        self._seg: dict[str, Any] | None = None
        logger.info("DoubaoSaucSTTHandler ready (resource=%s hotwords=%d)", self.resource_id, len(self.hotwords))

    # ---- 协议件 ----

    def _headers(self) -> dict:
        headers = {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        else:
            headers["X-Api-App-Key"] = self.app_id
            headers["X-Api-Access-Key"] = self.access_token
        return headers

    def _config(self) -> dict:
        request: dict = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
            "result_type": "full",
            "enable_lid": True,
        }
        if self.hotwords:
            request["corpus"] = {
                "context": json.dumps(
                    {"hotwords": [{"word": t} for t in self.hotwords]}, ensure_ascii=False
                )
            }
        return {
            "user": {"uid": "bok-s2s"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
            "request": request,
        }

    # ---- 会话生命周期（跑在私有 loop 上） ----

    async def _open_ws(self):
        import websockets

        return await websockets.connect(self.ws_url, additional_headers=self._headers(),
                                         open_timeout=self.connect_timeout_s)

    async def _drain(self, ws, *, wait_s: float) -> str:
        """收帧累积最长文本；wait_s<=0 时非阻塞尽力收。"""
        best = ""
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            remain = deadline - time.monotonic()
            try:
                data = await ws.recv() if remain > 0 else (await asyncio.wait_for(ws.recv(), timeout=0.05))
            except asyncio.TimeoutError:
                break
            except Exception:  # noqa: BLE001 - 连接收尾/关闭都按当前累积结算
                break
            frame = parse_server_frame(data)
            if frame["type"] == MSG_ERROR:
                logger.warning("doubao sauc error frame code=%s payload=%s",
                               frame["code"], frame["payload"][:120])
                break
            if frame["type"] != MSG_FULL_SERVER_RESP:
                continue
            try:
                result = json.loads(frame["payload"] or b"{}")
            except Exception:  # noqa: BLE001
                continue
            text = str((result.get("result") or {}).get("text") or "")
            if len(text) > len(best):
                best = text
            if frame["is_last"]:
                break
        return best

    async def _send_audio(self, ws, seq_start: int, pcm: bytes, *, last: bool) -> int:
        seq = seq_start
        for i in range(0, len(pcm), self.packet_bytes):
            chunk = pcm[i:i + self.packet_bytes]
            is_last = last and i + self.packet_bytes >= len(pcm)
            await ws.send(frame_audio(seq, chunk, last=is_last))
            seq += 1
        if last and len(pcm) == 0:
            await ws.send(frame_audio(seq, b"", last=True))
            seq += 1
        return seq

    async def _transcribe_whole(self, pcm: bytes) -> str:
        """整段单发（首连失败/无文本重试路径）。"""
        ws = await self._open_ws()
        try:
            await ws.send(frame_full_client_request(self._config()))
            await _send_noop_drain(ws)
            await self._send_audio(ws, 2, pcm, last=True)
            return await self._drain(ws, wait_s=self.final_timeout_s)
        finally:
            await ws.close()

    async def _feed_session(self, seg: dict, prefix_pcm: bytes, *, last: bool) -> str:
        """增量喂入：prefix_pcm 是累积前缀，按 fed 游标取新增尾巴发送。"""
        fed = int(seg.get("fed", 0))
        if fed > len(prefix_pcm):
            # 前缀回缩（理论不发生）：重开会话由上层处理，这里只防御。
            fed = 0
            seg["seq"] = 1
        delta = prefix_pcm[fed:]
        seg["fed"] = len(prefix_pcm)
        seg["pcm"] = (seg.get("pcm") or bytearray()) + delta
        del seg["pcm"][:-_SEG_CAP_BYTES]
        seg["seq"] = await self._send_audio(seg["ws"], seg["seq"], delta, last=last)
        return await self._drain(seg["ws"], wait_s=self.final_timeout_s if last else 0.0)

    # ---- handler 契约 ----

    def process(self, vad_audio: STTIn) -> Iterator[STTOut]:
        progressive = vad_audio.mode == "progressive"
        audio = np.asarray(vad_audio.audio, dtype=np.float32)
        pcm = np.clip(audio * 32768, -32768, 32767).astype(np.int16).tobytes()

        if progressive:
            seg = self._seg
            if seg is None or seg["turn"] != vad_audio.turn_id or seg.get("closed"):
                # 新段：整段重开会话（旧段未正常收尾则弃——其 final 已错过）
                if seg is not None and not seg.get("closed"):
                    self._loop_thread.call(self._close_ws(seg), timeout=3.0)
                seg = {"turn": vad_audio.turn_id, "seq": 2, "text": "", "pcm": bytearray(),
                       "fed": 0, "ws": None, "closed": False}
                self._seg = seg
                seg["ws"] = self._loop_thread.call(self._open_ws(), timeout=self.connect_timeout_s + 2.0)
                self._loop_thread.call(
                    self._send_config(seg["ws"]), timeout=5.0
                )
            # 注：STTIn.audio 是累积前缀（progressive 全量-so-far / final 全段），
            # 喂入按 fed 游标只发新增尾巴，绝不重复。
            try:
                text = self._loop_thread.call(
                    self._feed_session(seg, pcm, last=False), timeout=self.connect_timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - 喂包失败：留缓冲，final 路整段重试
                logger.warning("doubao progressive feed failed: %r", exc)
                text = ""
            if text and len(text) > len(seg["text"]):
                seg["text"] = text
                yield PartialTranscription(
                    text=text,
                    turn_id=vad_audio.turn_id,
                    turn_revision=vad_audio.turn_revision,
                )
            return

        # ---- final ----
        seg = self._seg
        text = ""
        if seg is not None and seg["turn"] == vad_audio.turn_id and not seg.get("closed"):
            try:
                text = self._loop_thread.call(
                    self._feed_session(seg, pcm, last=True),
                    timeout=self.final_timeout_s + 3.0,
                )
                if len(text) < len(seg["text"]):
                    text = seg["text"]
            except Exception as exc:  # noqa: BLE001
                logger.warning("doubao final failed (%r) — whole-segment retry", exc)
                text = ""
            finally:
                try:
                    self._loop_thread.call(self._close_ws(seg), timeout=3.0)
                except Exception:  # noqa: BLE001
                    pass
                seg["closed"] = True
        if not text and len(seg["pcm"] if seg else b"") > 0 and seg is not None:
            try:
                text = self._loop_thread.call(
                    self._transcribe_whole(bytes(seg["pcm"])), timeout=self.final_timeout_s * 2
                )
            except Exception as exc:  # noqa: BLE001 - 重试仍败 → 空转写，下游兜底
                logger.warning("doubao whole-segment retry failed: %r", exc)
                text = ""
        yield Transcription(
            text=text,
            language_code="auto",
            turn_id=vad_audio.turn_id,
            turn_revision=vad_audio.turn_revision,
            speech_stopped_at_s=vad_audio.speech_end_at_s,
        )

    async def _send_config(self, ws) -> None:
        await ws.send(frame_full_client_request(self._config()))

    async def _close_ws(self, seg: dict) -> None:
        ws = seg.get("ws")
        seg["closed"] = True
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            seg["ws"] = None

    def on_session_end(self) -> None:
        seg = self._seg
        if seg is not None and not seg.get("closed"):
            try:
                self._loop_thread.call(self._close_ws(seg), timeout=3.0)
            except Exception:  # noqa: BLE001
                pass
        self._seg = None

    def cleanup(self) -> None:
        self.on_session_end()
        self._loop_thread.stop()


async def _send_noop_drain(ws) -> None:
    """config ack 尽力收掉（不等待）。"""
    try:
        await asyncio.wait_for(ws.recv(), timeout=0.2)
    except Exception:  # noqa: BLE001
        pass
