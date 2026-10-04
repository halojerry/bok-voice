"""豆包（火山引擎）流式语音识别 —— LiveKit STT provider（2026-10-03 云 ASR 装线波）。

协议：openspeech V3 族 SAUC 双向流式（``wss://.../api/v3/sauc/bigmodel``，二进制帧
+ gzip；帧格式与 scripts/probes/probe_cloud_asr.py 同源——探针为取证台架，本文件为生产
实现，协议语义改动两处同步）。

设计定案（全部有 2026-10-03 实弹证据，见 probe 报告 reports/cloud-asr/）：

- **一段 VAD 语音 = 一个 WS 会话**：本地 VAD（min_silence 0.28-0.45s）驱动分段，
  START_OF_SPEECH 开会话（连接时延藏在用户说话期间）、说话中 200ms 分包喂入、
  END_OF_SPEECH 发末包（负 seq）强制服务端立即定稿——服务端自身 800ms
  end_window 判停在我们的分段节奏里结构性用不上（我们 0.28s 静音先到）。
- **result.text 全程单调累积**（两句话夹 1s 静音实弹：不重置、definite 只在末包
  出现）——FINAL 直接取「最长已见文本」，无需已提交坐标/拼接账本。
- **interim 高频**（~200ms/次、有重复）——按「文本变化」去重后发 INTERIM_TRANSCRIPT。
- **重试**：整段 PCM 全程留缓冲（上限 60s），live 路连接失败/超时/服务端错且无
  文本时，整段单发重试一次（探针同姿势）；重试仍败 → 返回空（EOS 已发，空转写由
  下游 EMPTY_TURN_DROPPED 兜底，绝不悬挂轮次）。
- **方言**：``enable_lid`` 常开（官方「启用中英文及方言识别」，含粤语）；语言不传
  （auto 三语实测全通，zh 0.0 / 粤 0.084 / en 0.073）。
- **热词**：``request.corpus.context`` 直传热词（100 token 上限，取前 40 词）。
- 收线/告别直念窗（F4）：``set_closing_say(True)`` 期间整段丢弃（不发 EOS/FINAL）。

总闸 ``BOK_DOUBAO_ASR``（缺省开，=0 时装配点回退本地 Qwen3-ASR；装配点读法见
agent.py / interpret.py 的 provider 分支）。凭据/端点由设置面（asr 段）经装配点
下发，本模块零 env 凭据、零模块级可变全局（worker 并发多通不串线）。
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import os
import struct
import time
import uuid
from urllib.parse import urlsplit

from livekit.agents import APIConnectOptions, stt, utils, vad

# ---- 常量 ----
DOUBAO_WS_DEFAULT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
# 官方推荐 2.0（seedasr）；1.0（bigasr）为历史版本——两者四臂矩阵实测同输出，
# 装线按 2.0，设置面可覆盖。
DOUBAO_RESOURCE_DEFAULT = "volc.seedasr.sauc.duration"

_PACKET_BYTES = 16000 * 2 * 200 // 1000  # 200ms@16k mono PCM16
_SEG_CAP_BYTES = 16000 * 2 * 60  # 重试缓冲上限 60s
_CONNECT_TIMEOUT_S = 8.0
_FINAL_TIMEOUT_S = 6.0

# 火山 SAUC 二进制帧（V3 协议族；官方 demo protocol.py 语义）
MSG_FULL_CLIENT_REQ = 0b0001
MSG_AUDIO_ONLY_REQ = 0b0010
MSG_FULL_SERVER_RESP = 0b1001
MSG_SERVER_ACK = 0b1011
MSG_ERROR = 0b1111

FLAG_NO_SEQ = 0b0000
FLAG_POS_SEQ = 0b0001
FLAG_LAST_NO_SEQ = 0b0010
FLAG_NEG_SEQ = 0b0011


def doubao_asr_enabled() -> bool:
    """总闸（缺省开）：=0 时装配点强制回退本地 Qwen3-ASR。"""
    return os.environ.get("BOK_DOUBAO_ASR", "1") != "0"


def _header(msg_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes([
        (1 << 4) | 1,                      # version=1, header_size=1 → 4 字节头
        (msg_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def frame_full_client_request(payload: dict) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    buf = _header(MSG_FULL_CLIENT_REQ, FLAG_POS_SEQ, serialization=1, compression=1)
    buf += struct.pack(">i", 1)            # seq=1
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
    """宽容解析（官方 demo 语义）：flags&1=带 seq、&2=末包、&4=带 event。"""
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
                except Exception:  # noqa: BLE001 - 解析失败留原始
                    pass
            out["payload"] = payload
    return out


def _ws_host_ok(url: str) -> bool:
    """仅放行 wss/https 公网端点（SSRF 护栏：拒环回/私有/保留地址）。

    域名不在此处做 DNS 解析（解析发生在 websockets 连接层）；IP 字面量按
    ``ipaddress.is_global`` 判定——私有/环回/保留/链路本地一律拒绝。
    """
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
        return True  # 域名：放行（连接层再做解析）
    return bool(ip.is_global)


class DoubaoSTT(stt.STT):
    """豆包 SAUC 流式 ASR（LiveKit STT；A/B 线共用）。

    capabilities.streaming=True——本类自带 VAD 骨架流（无离线包装层）；
    interim_results=True（服务端增量文本去重后发 INTERIM_TRANSCRIPT）。
    """

    model = "doubao-asr"
    provider = "doubao"

    def __init__(
        self,
        *,
        api_key: str = "",
        resource_id: str = DOUBAO_RESOURCE_DEFAULT,
        ws_url: str = DOUBAO_WS_DEFAULT,
        app_id: str = "",
        access_token: str = "",
        language_state=None,
        hotword_terms: list[str] | None = None,
        vad_=None,
        connect_timeout: float = _CONNECT_TIMEOUT_S,
        final_timeout: float = _FINAL_TIMEOUT_S,
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                diarization=False,
                aligned_transcript=False,
                offline_recognize=True,
                keyterms=False,
                chat_context=False,
            )
        )
        self._api_key = str(api_key or "").strip()
        self._resource_id = str(resource_id or "").strip() or DOUBAO_RESOURCE_DEFAULT
        self._ws_url = str(ws_url or "").strip() or DOUBAO_WS_DEFAULT
        self._app_id = str(app_id or "").strip()
        self._access_token = str(access_token or "").strip()
        self._language_state = language_state
        # 热词（装配点已按当通 effective 词表解析好；100 token 上限内取前 40 词，
        # 保序去重防预算被重复词吃掉）。
        self._hotword_terms = list(
            dict.fromkeys(str(t).strip() for t in (hotword_terms or []) if str(t).strip())
        )
        self._vad = vad_
        self._connect_timeout = float(connect_timeout)
        self._final_timeout = float(final_timeout)
        # 与 Qwen3ASRLiveSTT 同款公开面（agent 侧 duck 访问）：partial 档旋钮、
        # 回复在途旗、收线窗旗、本轮 partial 末稿。云档语义见各方法 docstring。
        self._partial_ms_override: int | None = None
        self._reply_busy = False
        self._closing_say = False
        self._turn_partial_text: str = ""
        # 无 token 置信度（云档无此物）——轮处理器的 CSC 门读它，None=门不触发。
        self.last_confidence: dict | None = None

    # ---- 凭据/请求构造（每次连接现取；零模块级状态）----
    def _headers(self) -> dict:
        headers = {
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
            # 官方文档列为必选（任务ID，随机 UUID）；社区协议只有 Connect-Id，双发无害。
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        else:
            # 旧版控制台鉴权（APP ID + Access Token）——设置面两套字段并存，
            # 新版单 Key 优先。
            headers["X-Api-App-Key"] = self._app_id
            headers["X-Api-Access-Key"] = self._access_token
        return headers

    def _config(self) -> dict:
        request: dict = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
            "result_type": "full",
            # 方言识别开关（官方「启用中英文及方言识别」，含粤语）。缺省 false 时
            # 粤语被按普通话音系硬转（2026-10-03 实测），故常开。
            "enable_lid": True,
        }
        terms = self._hotword_terms[:40]
        if terms:
            # 直传热词（官方 corpus.context，JSON 字符串；与热词表合计 100 token 上限）。
            request["corpus"] = {
                "context": json.dumps(
                    {"hotwords": [{"word": t} for t in terms]}, ensure_ascii=False
                )
            }
        return {
            "user": {"uid": "bok-agent"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
            "request": request,
        }

    def _lang(self) -> str:
        return str(getattr(self._language_state, "lang", "") or "")

    # ---- LiveKit STT 接口 ----
    def stream(self, *, language=None, conn_options=None):
        return _DoubaoLiveStream(self, conn_options=conn_options or APIConnectOptions())

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        pcm = bytes(getattr(buffer, "data", b""))
        text = await self._transcribe_once(pcm) if pcm else ""
        alternatives = (
            [stt.SpeechData(language=self._lang(), text=text)] if text else []
        )
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT, request_id="", alternatives=alternatives
        )

    # ---- 整段单发（重试路 + offline recognize 共用）----
    async def _transcribe_once(self, pcm: bytes, timeout: float | None = None) -> str:
        if not pcm:
            return ""
        import websockets

        timeout = float(timeout if timeout is not None else self._final_timeout + 4.0)
        try:
            ws = await websockets.connect(
                self._ws_url,
                additional_headers=self._headers(),
                open_timeout=self._connect_timeout,
                max_size=20_000_000,
            )
        except Exception as exc:  # noqa: BLE001
            print("DOUBAO_ASR_CONNECT_ERROR", repr(exc), flush=True)
            return ""
        try:
            await ws.send(frame_full_client_request(self._config()))
            packets = [pcm[i:i + _PACKET_BYTES] for i in range(0, len(pcm), _PACKET_BYTES)]
            for i, packet in enumerate(packets):
                await ws.send(frame_audio(i + 2, packet, last=(i == len(packets) - 1)))
            text = ""
            deadline = time.monotonic() + timeout
            try:
                while True:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.5, deadline - time.monotonic())
                    )
                    if isinstance(raw, str):
                        continue
                    fr = parse_server_frame(bytes(raw))
                    if fr["type"] == MSG_ERROR:
                        print("DOUBAO_ASR_SERVER_ERROR code=", fr.get("code"), flush=True)
                        break
                    if fr["payload"]:
                        try:
                            j = json.loads(fr["payload"])
                        except Exception:  # noqa: BLE001
                            continue
                        t = str(((j.get("result") or {}).get("text")) or "")
                        if len(t) >= len(text):
                            text = t
                    if fr["is_last"]:
                        break
            except asyncio.TimeoutError:
                print("DOUBAO_ASR_ONESHOT_TIMEOUT", flush=True)
            return text
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print("DOUBAO_ASR_STREAM_ERROR", repr(exc), flush=True)
            return ""
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- 与 Qwen3ASRLiveSTT 同款公开面（agent 侧 duck 访问，语义见各自 docstring）----
    def set_partial_ms(self, ms: int | None) -> None:
        """会话级 partial 解码档（本地侧 GPU 竞态旋钮）——云档无对应物，只记不用。"""
        self._partial_ms_override = ms

    def set_reply_busy(self, busy: bool) -> None:
        """回复在途旗（本地侧迟到 FINAL 护栏读它）——云档终稿来自服务端无重解，
        v1 只记不用（保留字段供后续云侧守卫扩展）。"""
        self._reply_busy = bool(busy)

    def set_closing_say(self, on: bool) -> None:
        """收线/告别直念窗：True 期间流整段丢弃（不发 EOS/FINAL，把告别说完）。"""
        self._closing_say = bool(on)

    def last_partial_text(self) -> str:
        """本轮 ASR partial 末稿（E2 热词泄漏清洗的 fallback_text 取口）。

        契约与 Qwen3 版同款：只反映本轮（新语音段开场即清；该轮无 FINAL 发出时
        为空）；读取恒安全（纯属性读）。云档写点=interim 文本更新 + FINAL 重贴。
        """
        return str(self._turn_partial_text or "")


class _DoubaoLiveStream(stt.RecognizeStream):
    """VAD 骨架 + 段内 WS 会话的豆包流式转写。

    生命周期：START_OF_SPEECH 开 WS（配置帧 seq=1）→ 说话中 200ms 分包喂音频
    → END_OF_SPEECH 发末包（负 seq）→ 等 is_last 收全文 → FINAL_TRANSCRIPT。
    收线窗（_closing_say）整段丢弃；live 路失败且无文本时整段单发重试一次。
    """

    def __init__(self, stt_: DoubaoSTT, *, conn_options):
        super().__init__(stt=stt_, conn_options=conn_options, sample_rate=16000)
        self._stt_ = stt_
        self._vad = stt_._vad
        # 段状态（_reset_segment 清）
        self._seg_pcm = bytearray()      # 整段 PCM（重试缓冲，上限 60s）
        self._last_server_text = ""      # 段内最长已见文本（result.text 单调）
        self._last_interim_emitted = ""  # INTERIM 去重
        self._session_error = ""
        self._finishing = False
        self._saw_last = False           # 本段是否收到服务端末包（正常定稿）
        # 会话状态
        self._ws = None
        self._send_q: asyncio.Queue | None = None
        self._sender_task: asyncio.Task | None = None
        self._receiver_task: asyncio.Task | None = None
        self._final_evt: asyncio.Event | None = None
        self._session_alive = False

    # ---- 会话管理 ----
    async def _open_session(self) -> None:
        if not _ws_host_ok(self._stt_._ws_url):
            self._session_error = "ws_url_rejected"
            print(f"DOUBAO_ASR_URL_REJECTED {self._stt_._ws_url!r}", flush=True)
            return
        import websockets

        try:
            ws = await websockets.connect(
                self._stt_._ws_url,
                additional_headers=self._stt_._headers(),
                open_timeout=self._stt_._connect_timeout,
                max_size=20_000_000,
            )
        except Exception as exc:  # noqa: BLE001
            self._session_error = f"connect:{exc!r}"
            print("DOUBAO_ASR_CONNECT_ERROR", repr(exc), flush=True)
            return
        try:
            await ws.send(frame_full_client_request(self._stt_._config()))
        except Exception as exc:  # noqa: BLE001
            self._session_error = f"config:{exc!r}"
            print("DOUBAO_ASR_CONFIG_ERROR", repr(exc), flush=True)
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            return
        self._ws = ws
        self._send_q = asyncio.Queue()
        self._final_evt = asyncio.Event()
        self._session_alive = True
        # 持引用任务（禁裸 create_task：异常回收 + 关闭时 cancel 需要句柄）。
        self._sender_task = asyncio.create_task(self._sender(ws))
        self._receiver_task = asyncio.create_task(self._receiver(ws))
        print("DOUBAO_ASR_CONNECT ok", flush=True)

    async def _close_session(self) -> None:
        self._session_alive = False
        tasks = [t for t in (self._sender_task, self._receiver_task) if t is not None]
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sender_task = None
        self._receiver_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None

    async def _sender(self, ws) -> None:
        """音频泵：队列收 PCM → 200ms 分包 → 正 seq 帧；None 哨兵 = 末包（负 seq）。"""
        seq = 2
        buf = bytearray()
        while True:
            item = await self._send_q.get()
            if item is None:
                # 末包：余量（可能为空）强制服务端立即定稿。
                await ws.send(frame_audio(seq, bytes(buf), last=True))
                return
            buf.extend(item)
            while len(buf) >= _PACKET_BYTES:
                packet = bytes(buf[:_PACKET_BYTES])
                del buf[:_PACKET_BYTES]
                await ws.send(frame_audio(seq, packet, last=False))
                seq += 1

    async def _receiver(self, ws) -> None:
        try:
            while True:
                raw = await ws.recv()
                if isinstance(raw, str):
                    continue
                fr = parse_server_frame(bytes(raw))
                if fr["type"] == MSG_ERROR:
                    self._session_error = f"server:{fr.get('code')}"
                    print(f"DOUBAO_ASR_SERVER_ERROR code={fr.get('code')}", flush=True)
                    break
                if fr["payload"]:
                    try:
                        j = json.loads(fr["payload"])
                    except Exception:  # noqa: BLE001
                        j = None
                    if isinstance(j, dict):
                        self._on_payload(j)
                if fr["is_last"]:
                    self._saw_last = True
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._session_error = self._session_error or f"recv:{exc!r}"
        finally:
            if self._final_evt is not None:
                self._final_evt.set()

    def _on_payload(self, j: dict) -> None:
        res = j.get("result") or {}
        text = str(res.get("text") or "")
        if text and len(text) >= len(self._last_server_text):
            # 单调累积（实弹取证）；防御性取最长，防服务端变体重置。
            self._last_server_text = text
            self._maybe_interim(text)

    def _maybe_interim(self, text: str) -> None:
        if not text or text == self._last_interim_emitted:
            return
        self._last_interim_emitted = text
        try:
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language=self._stt_._lang(), text=text)],
                )
            )
        except Exception:  # noqa: BLE001 - 流已关：迟到 interim 丢弃
            pass

    def _feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._seg_pcm.extend(pcm)
        if len(self._seg_pcm) > _SEG_CAP_BYTES:
            del self._seg_pcm[: len(self._seg_pcm) - _SEG_CAP_BYTES]
        if self._session_alive and self._send_q is not None:
            self._send_q.put_nowait(pcm)

    async def _finish_segment(self) -> str:
        """末包 → 等定稿 → （未见末包时）整段单发重试。返回全文（可能空串）。

        重试判据=``not _saw_last``（连接失败/中途死/超时/服务端错——此时手里的
        文本可能是半截稿）；正常末包（含合法空转写）不重试，绝不重复烧钱。
        """
        text = ""
        if self._session_alive and self._send_q is not None and self._final_evt is not None:
            self._send_q.put_nowait(None)
            try:
                await asyncio.wait_for(self._final_evt.wait(), timeout=self._stt_._final_timeout)
            except asyncio.TimeoutError:
                self._session_error = self._session_error or "final_timeout"
                print("DOUBAO_ASR_FINAL_TIMEOUT", flush=True)
            text = self._last_server_text
        await self._close_session()
        if self._seg_pcm and not self._saw_last:
            print(
                f"DOUBAO_ASR_RETRY cause={(self._session_error or 'no_last')[:80]} "
                f"seg_ms={len(self._seg_pcm) // 32}",
                flush=True,
            )
            retry_text = await self._stt_._transcribe_once(bytes(self._seg_pcm))
            if len(retry_text) >= len(text):
                text = retry_text
        return text

    def _reset_segment(self) -> None:
        self._seg_pcm.clear()
        self._last_server_text = ""
        self._last_interim_emitted = ""
        self._session_error = ""
        self._saw_last = False
        # 暴露位随段清零；FINAL 发出点按 pre-reset 快照重贴（与 Qwen3 版契约一致）。
        self._stt_._turn_partial_text = ""

    # ---- 主循环（VAD 双任务骨架，与 _Qwen3ASRLiveStream 同构）----
    async def _run(self) -> None:
        vad_stream = self._vad.stream()

        async def _forward_input() -> None:
            async for input in self._input_ch:
                if isinstance(input, self._FlushSentinel):
                    vad_stream.flush()
                    continue
                vad_stream.push_frame(input)
            vad_stream.end_input()

        async def _recognize() -> None:
            started = False
            async for event in vad_stream:
                if event.type == vad.VADEventType.START_OF_SPEECH:
                    started = True
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(stt.SpeechEventType.START_OF_SPEECH)
                    )
                    await self._open_session()
                    if event.frames:
                        # 前导喂会话（silero prefix padding + min_speech 确认窗帧）
                        try:
                            self._feed(bytes(utils.merge_frames(event.frames).data))
                        except Exception:  # noqa: BLE001 - 合帧失败不致命
                            pass
                elif event.type == vad.VADEventType.INFERENCE_DONE:
                    if not started or self._finishing:
                        continue
                    try:
                        self._feed(bytes(utils.merge_frames(event.frames).data))
                    except Exception:  # noqa: BLE001
                        continue
                elif event.type == vad.VADEventType.END_OF_SPEECH:
                    if not started:
                        continue
                    # 收线/告别直念窗：整段丢弃（不发 EOS/FINAL，把告别说完）。
                    if bool(getattr(self._stt_, "_closing_say", False)):
                        started = False
                        self._finishing = False
                        self._reset_segment()
                        print("DOUBAO_ASR_CLOSING_SAY_SUPPRESS src=segment_eos", flush=True)
                        continue
                    self._finishing = True
                    speech_end_time = (
                        time.time() - event.silence_duration - event.inference_duration
                    )
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.END_OF_SPEECH,
                            speech_end_time=speech_end_time,
                        )
                    )
                    t0 = time.monotonic()
                    partial_snapshot = self._last_server_text
                    text = await self._finish_segment()
                    if text:
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[
                                    stt.SpeechData(language=self._stt_._lang(), text=text)
                                ],
                            )
                        )
                    print(
                        f"DOUBAO_ASR_TEXT {text[:120]!r} {self._stt_._lang()} "
                        f"ASR_MS={(time.monotonic() - t0) * 1000:.0f}(cloud)",
                        flush=True,
                    )
                    started = False
                    self._finishing = False
                    self._reset_segment()
                    if text and partial_snapshot:
                        self._stt_._turn_partial_text = partial_snapshot

        try:
            await asyncio.gather(_forward_input(), _recognize())
        finally:
            await self._close_session()
