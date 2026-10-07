"""MiniMax 云 ASR（asr-1.0）—— LiveKit offline STT provider（2026-10-07 四语车道）。

协议：``POST https://api.minimax.cn/v1/speech_to_text``（multipart：wav 文件 +
``model=asr-1.0`` + ``stream=true``；Bearer key + ``language`` 头），SSE ``data:``
行逐条 JSON、``delta`` 串接=全文、``finish=true`` 停读——请求/响应形状与
scripts/probes/probe_cloud_asr.py ``minimax_once`` 同源（探针为取证台架，本文件
为生产实现；协议语义改动两处同步）。

为什么有这个 provider（2026-10-06 W2c 四语实测，reports/cloud-asr/）：de/fr/ja/pt
源语豆包 SAUC 24/24 幻听（不可用），MiniMax asr-1.0 24/24 CER≤0.08 数字 4/4。
但 MiniMax「流式」只是 SSE 结果流——输入侧官方自述不支持推流（VAD 切段伪流式，
2026-10-03 定案表）。接线姿势=官方 ``stt.StreamAdapter(vad=...)`` 包本 offline
provider（interpret.py B 线装配点，A 线不吃本车道）。

- **热词**：MiniMax ASR 无热词表（定案表）——装配点不喂，术语表只帮 MT 侧。
- **失败语义**：单段失败返回空转写（框架 EOS 已发，空转写不悬挂轮次，
  DoubaoSTT._transcribe_once 同款）；异常单段重试一次（同豆包整段单发重试姿势）。
- **SSRF**：``_https_url_ok`` 镜像 doubao_asr._ws_host_ok（本 provider 只发 https）。
- **凭据**：装配点下发（env ``MINIMAX_API_KEY`` 优先，缺省回设置面 tts.api_key——
  MiniMax 控制台同一把 key，probe 回读同一 DB 面）。本模块零 env 凭据、零模块级
  可变全局（worker 并发多通不串线）。凭据绝不打印/落日志。
- 总闸 ``BOK_MINIMAX_ASR``（缺省开，=0 装配点回旧装配链逐字节）。
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import os
import time
import wave
from urllib.parse import urlsplit

import httpx
from livekit.agents import stt

MINIMAX_STT_URL_DEFAULT = "https://api.minimax.cn/v1/speech_to_text"
MINIMAX_ASR_MODEL = "asr-1.0"
_TIMEOUT_S = 20.0
_ATTEMPTS = 2  # 异常单段重试一次（豆包整段单发重试同姿势；正常响应不重试）

# 四语车道源语集（interpret.py 装配点判定同源；zh/cantonese/en 不在此集=既有
# 装配链逐字节）。标签只进 MiniMax HTTP 头（厂商 BCP-47 短标签=外部接口字面量；
# 探针 _VENDOR_LANG.minimax 同源子集，术语门禁边界映射口径不变）。
MINIMAX_ASR_4LANG = ("de", "fr", "ja", "pt")
LANG_TAGS = {"de": "de", "fr": "fr", "ja": "ja", "pt": "pt"}


def minimax_asr_enabled() -> bool:
    """总闸（缺省开）：=0 时装配点回旧装配链（逐字节）。"""
    return os.environ.get("BOK_MINIMAX_ASR", "1") != "0"


def _https_url_ok(url: str) -> bool:
    """仅放行 https 公网端点（SSRF 护栏，镜像 doubao_asr._ws_host_ok）。

    域名不在此处做 DNS 解析（解析发生在 httpx 连接层）；IP 字面量按
    ``ipaddress.is_global`` 判定——私有/环回/保留/链路本地一律拒绝。
    """
    try:
        parsed = urlsplit(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme != "https":
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


def _wrap_wav(pcm: bytes, sample_rate: int, num_channels: int) -> bytes:
    """裸 PCM16 包成内存 wav（multipart file 件）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(num_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _pcm_to_wav(pcm: bytes, sample_rate: int = 16000, num_channels: int = 1) -> bytes:
    """段 PCM → 内存 wav。

    快路=16k mono 直包（探针定案表请求形状）；非 16k mono 输入（房间轨常见 48k）
    经官方 ``rtc.AudioResampler`` 重采样到 16k mono 再包——24/24 定案分数只在
    16k wav 形状上成立，直接包非 16k wav=请求形状漂移。重采样失败按原样打包
    （合成失败好过整段丢转写）。
    """
    sample_rate = int(sample_rate or 16000)
    num_channels = int(num_channels or 1)
    if sample_rate != 16000 or num_channels != 1:
        try:
            from livekit import rtc

            frame = rtc.AudioFrame(
                data=pcm,
                sample_rate=sample_rate,
                num_channels=num_channels,
                samples_per_channel=max(1, len(pcm) // (2 * num_channels)),
            )
            resampler = rtc.AudioResampler(
                input_rate=sample_rate, output_rate=16000, num_channels=1
            )
            out = bytearray()
            for chunk in [*resampler.push(frame), *resampler.flush()]:
                out.extend(bytes(chunk.data))
            if out:
                return _wrap_wav(bytes(out), 16000, 1)
        except Exception as exc:  # noqa: BLE001 - 重采样失败降级原样打包
            print("MINIMAX_ASR_RESAMPLE_FALLBACK", repr(exc), flush=True)
    return _wrap_wav(pcm, sample_rate, num_channels)


class MiniMaxSTT(stt.STT):
    """MiniMax asr-1.0 一次性转写（offline；配合官方 StreamAdapter 伪流式）。

    capabilities.streaming=False——流式请求由装配点的 ``stt.StreamAdapter``
    （VAD 分段→逐段 recognize）承担；``stream()`` 走基类 NotImplementedError
    （官方 offline STT 同语义，直连流式=装配错误）。``offline_recognize=True``。
    """

    model = "minimax-asr"
    provider = "minimax"

    def __init__(
        self,
        *,
        api_key: str = "",
        url: str = MINIMAX_STT_URL_DEFAULT,
        language_state=None,
        lang_tag: str = "",
        model: str = MINIMAX_ASR_MODEL,
        timeout: float = _TIMEOUT_S,
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                diarization=False,
                aligned_transcript=False,
                offline_recognize=True,
                keyterms=False,
                chat_context=False,
            )
        )
        self._api_key = str(api_key or "").strip()
        self._url = str(url or "").strip() or MINIMAX_STT_URL_DEFAULT
        self._language_state = language_state
        self._lang_tag = str(lang_tag or "").strip()
        self._model = str(model or "").strip() or MINIMAX_ASR_MODEL
        self._timeout = float(timeout)

    def _lang(self) -> str:
        return str(getattr(self._language_state, "lang", "") or "")

    # ---- LiveKit STT 接口 ----
    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        pcm = bytes(getattr(buffer, "data", b""))
        sample_rate = int(getattr(buffer, "sample_rate", 16000) or 16000)
        num_channels = int(getattr(buffer, "num_channels", 1) or 1)
        text = (
            await self._transcribe_once(pcm, sample_rate, num_channels) if pcm else ""
        )
        alternatives = (
            [stt.SpeechData(language=self._lang(), text=text)] if text else []
        )
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT, request_id="", alternatives=alternatives
        )

    # ---- 整段单发（multipart POST + SSE 读干；探针 minimax_once 同源）----
    async def _transcribe_once(
        self, pcm: bytes, sample_rate: int = 16000, num_channels: int = 1
    ) -> str:
        """整段一次性转写，返回全文（可能空串）。失败重试一次后返回空串——
        绝不悬挂轮次（下游 EMPTY_TURN_DROPPED 兜底）。凭据只在请求头，零打印。"""
        if not pcm:
            return ""
        if not _https_url_ok(self._url):
            print(f"MINIMAX_ASR_URL_REJECTED {self._url!r}", flush=True)
            return ""
        wav_bytes = _pcm_to_wav(pcm, sample_rate, num_channels)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "language": self._lang_tag,
        }
        data = {"model": self._model, "stream": "true"}
        files = {"file": ("segment.wav", wav_bytes, "audio/wav")}
        last_exc: Exception | None = None
        for attempt in range(_ATTEMPTS):
            t0 = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    async with client.stream(
                        "POST", self._url, headers=headers, data=data, files=files
                    ) as resp:
                        resp.raise_for_status()
                        parts: list[str] = []
                        finished = False
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            try:
                                ev = json.loads(line[len("data:"):].strip())
                            except json.JSONDecodeError:
                                continue
                            delta = str(ev.get("delta") or "")
                            if delta:
                                parts.append(delta)
                            if ev.get("finish"):
                                finished = True
                                break
                    text = "".join(parts)
                    if not finished:
                        # 流早断：手里文本可能是半截稿——按豆包「未见末包重试」
                        # 同判据，还有重试预算就再试一次。
                        print("MINIMAX_ASR_STREAM_ENDED_EARLY", flush=True)
                        if attempt + 1 < _ATTEMPTS:
                            last_exc = RuntimeError("stream ended early")
                            continue
                    print(
                        f"MINIMAX_ASR_TEXT {text[:120]!r} {self._lang()} "
                        f"ASR_MS={(time.monotonic() - t0) * 1000:.0f}(cloud)",
                        flush=True,
                    )
                    return text
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络/服务端错：重试一次
                last_exc = exc
                print("MINIMAX_ASR_RETRY", attempt + 1, repr(exc), flush=True)
                await asyncio.sleep(0.5 * (attempt + 1))
        print("MINIMAX_ASR_ERROR", repr(last_exc), flush=True)
        return ""
