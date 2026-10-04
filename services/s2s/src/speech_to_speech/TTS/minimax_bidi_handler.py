"""Bok patch: MiniMax t2a_v2_bidi 流式 TTS handler（官方 WS 协议，生产加固版）。

协议实现从我们生产 livekit 插件（apps/agent/agent_runtime/providers/
livekit_plugins.py MiniMaxTTS/_MiniMaxBidiSession）搬入本宿主。本版=协议核心
（连接/task_start/task_continue/task_flush/hex-PCM 解码/task_flushed 收尾）
+ 生产加固件随迁（与 livekit_plugins._MiniMaxBidiSession 同源语义）：

- 60s 自管 ping（官方：服务端永不 ping，空闲 >120s 断连；``ping_interval=None``
  + 本类自管 loop，ping 连失 >=2 强断重连；``MINIMAX_BIDI_PING_S`` /
  ``MINIMAX_BIDI_PING_MAX_MISS``）；
- 连接/握手失败静默重试 1 次（官方 #6969 姿势，1s 后重试；宿主无装配期预热钩子，
  故落在连接建立单点；``MINIMAX_BIDI_CONNECT_RETRY=0`` 关）；
- RPM 限流熔断：本会话连续限流（事件 error code 1002/1039 或 payload 含
  rate limit）>=2 轮 → 后续每个 process 打一行 WARNING 并放弃出声（宿主无 HTTP
  回退车道，故不做回落只做熔断；``MINIMAX_TTS_BIDI_GUARD=0`` 整闸回旧行为）；
- 陈旧音频纪元门禁：连接整会话复用、流按 response 分 epoch，每个 response 的
  首个 task_continue 认领；认领前收到的音频一律丢弃（``MINIMAX_BIDI_DROP_STALE=0`` 关）。
- 未随迁：合成级预热（宿主无装配期钩子）、classic 连接池（无 classic 车道）。

官方协议要点（生产实弹验证过的语义）：
- ``wss://api.minimax.cn|chat/ws/v1/t2a_v2_bidi``，Bearer 鉴权，
  ``ping_interval=None``（客户端自管 ping）
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
import os
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

# ---- 生产加固件常量（随迁自 livekit_plugins._MiniMaxBidiSession）------------
_PING_TIMEOUT_S = 10.0  # 单发 ping 的等待上限（发出即算，不等 pong 到天荒地老）
_CONNECT_RETRY_S = 1.0  # 连接/握手失败静默重试间隔（官方 #6969 姿势）
# 本会话连续限流 >= 该轮数 → 熔断（宿主无 HTTP 回退，熔断=后续轮放弃出声）。
_RATE_LIMIT_MAX_STREAK = 2
# 限流族状态码（生产 1002=RPM / 1039=TPM；宿主只做熔断，不重试不回落）。
_RATE_LIMIT_CODES = frozenset({1002, 1039})
# 认领纪元前的残留清除窗（宿主 recv 是拉取式：清一遍缓冲即断残留）。
_PURGE_WINDOW_S = 0.05


def _env_switch(name: str, default_on: bool = True) -> bool:
    """布尔 env：缺省/空=默认；"0/false/no/off"=关；其余=开。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default_on
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except Exception:  # noqa: BLE001 - 配错回默认
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except Exception:  # noqa: BLE001 - 配错回默认
        return default


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
        # ---- 生产加固件状态（livekit_plugins._MiniMaxBidiSession 同源）----
        self._ping_task: asyncio.Task | None = None
        self._ping_misses = 0  # pong/连接连失计数（达上限强断重连）
        # 纪元门禁：连接整会话复用（会话=本 handler 实例），流按 response 分 epoch。
        # 首个 task_continue 才认领 _claimed_epoch；认领前收到的音频=上一流残留 → 丢。
        self._epoch_seq = 0
        self._cur_epoch = 0
        self._claimed_epoch = -1  # -1=尚无认领（epoch 从 1 起编）
        self._response_open = False
        # 限流熔断：本会话连续限流轮数（一轮最多计一次；WS 成功出声归零）。
        self._rate_limit_streak = 0
        self._rate_limited_response = False
        self._rl_counted = False
        self._response_pushed = False
        self._stale_msgs = 0
        self._stale_bytes = 0
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
            # 官方：服务端永不 ping（空闲 >120s 断连）→ 关掉库自带 keepalive，
            # 用本类 60s 自管 ping（_ping_loop）保活。
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
        # 先落引用再起 ping：ping loop 以 `self._ws is ws` 为存活判据，顺序反了
        # 首圈检查会看到 None 直接退出（handler 线程赋值晚于 loop 线程起 task）。
        self._ws = ws
        self._start_ping(ws)
        return ws

    def _ensure_open(self):
        if self._ws is None:
            self._connect_with_retry()  # 成功时 _connect 内部落 self._ws
        return self._ws

    def _connect_with_retry(self):
        """连接/握手失败静默重试 1 次（官方 #6969 姿势；仍失败上抛由 process 兜）。

        宿主无装配期预热钩子（生产把这条重试放在 prewarm 里），故落在连接建立单点。
        """
        attempts = 2 if _env_switch("MINIMAX_BIDI_CONNECT_RETRY", True) else 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._loop_thread.call(self._connect(), timeout=_START_TIMEOUT_S + 5.0)
            except Exception as exc:  # noqa: BLE001 - 重试或上抛
                last_exc = exc
                if attempt < attempts:
                    logger.warning(
                        "minimax bidi connect failed (attempt %d/%d), retry in %.1fs: %r",
                        attempt, attempts, _CONNECT_RETRY_S, exc,
                    )
                    print(f"MINIMAX_BIDI_CONNECT_RETRY attempt={attempt} err={exc!r}"[:200], flush=True)
                    time.sleep(_CONNECT_RETRY_S)
        raise last_exc

    # ---- 60s 自管 ping（生产同源：连失 >=2 强断重连）----

    @staticmethod
    def _ping_interval() -> float:
        v = _env_float("MINIMAX_BIDI_PING_S", 60.0)
        return v if v > 0 else 0.0  # <=0 关闭自管 ping

    @staticmethod
    def _ping_max_miss() -> int:
        v = _env_int("MINIMAX_BIDI_PING_MAX_MISS", 2)
        return v if v > 0 else 2  # 非正数=配错回默认（0 会逢超时即断）

    async def _ping_loop(self, ws) -> None:
        """官方：服务端永不 ping，空闲 >120s 断连 → 客户端 60s 一发 WS ping。

        ping 帧发出即算（不 await pong 到天荒地老）；pong 静默/连接已死由接收侧
        （recv 异常）或连失计数判死。连失达上限 → 就地弃连接（下个 process 重连）。
        """
        try:
            while self._ws is ws:
                interval = self._ping_interval()
                if interval <= 0:
                    return
                await asyncio.sleep(interval)
                try:
                    await asyncio.wait_for(ws.ping(), timeout=_PING_TIMEOUT_S)
                except asyncio.TimeoutError:
                    self._ping_misses += 1
                    print(f"MINIMAX_TTS_BIDI_PING_TIMEOUT miss={self._ping_misses}", flush=True)
                    if self._ping_misses >= self._ping_max_miss():
                        print("MINIMAX_TTS_BIDI_DEAD ping连失 — 强断重连", flush=True)
                        # 唔可以在本 task 内 await _invalidate：它会 _stop_ping() 取消
                        # 当前 ping task，收尾被 CancelledError 掀掉。就地弃连接，
                        # 引用清空（下个 process _ensure_open 全新重连），再关旧连。
                        self._ws = None
                        self._claimed_epoch = -1
                        self._ping_misses = 0
                        try:
                            await ws.close()
                        except Exception:  # noqa: BLE001
                            pass
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 连接已死，接收侧会弃连接
                    return
                else:
                    self._ping_misses = 0
        except asyncio.CancelledError:
            raise

    def _start_ping(self, ws) -> None:
        self._stop_ping()
        self._ping_misses = 0
        if self._ping_interval() > 0:
            self._ping_task = asyncio.get_running_loop().create_task(self._ping_loop(ws))
            print(
                f"MINIMAX_TTS_BIDI_PING_START interval_s={self._ping_interval():g}",
                flush=True,
            )

    def _stop_ping(self) -> None:
        task = self._ping_task
        self._ping_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _invalidate(self) -> None:
        """弃置当前连接（限流/发送失败/收尾）：下个 process 自动全新重连。"""
        ws, self._ws = self._ws, None
        self._claimed_epoch = -1
        self._ping_misses = 0
        self._stop_ping()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _shutdown_ws(self, ws) -> None:
        self._stop_ping()
        await _close(ws)

    # ---- 限流熔断（宿主无 HTTP 回退：熔断=放弃出声；GUARD=0 整闸）----

    @staticmethod
    def _guard_enabled() -> bool:
        return _env_switch("MINIMAX_TTS_BIDI_GUARD", True)

    def _circuit_open(self) -> bool:
        return self._guard_enabled() and self._rate_limit_streak >= _RATE_LIMIT_MAX_STREAK

    @staticmethod
    def _status_code_of(ev: dict) -> int:
        for src in (ev, ev.get("data") or {}, ev.get("base_resp") or {}):
            if not isinstance(src, dict):
                continue
            for key in ("status_code", "code"):
                try:
                    code = int(src.get(key) or 0)
                except Exception:  # noqa: BLE001
                    continue
                if code:
                    return code
        return 0

    def _rate_limit_hit(self, ev: dict, raw: str) -> bool:
        """限流判据（生产同源）：事件 error code 1002/1039、或 payload 含 rate limit。"""
        if not self._guard_enabled():
            return False
        if self._status_code_of(ev) in _RATE_LIMIT_CODES:
            return True
        if ev.get("event") in ("error", "task_failed"):
            low = raw.lower()
            if "rate limit" in low or "rate_limit" in low or "rate-limit" in low or "限流" in raw:
                return True
        return False

    def _note_rate_limit(self, ev: dict, raw: str) -> None:
        """一轮最多计一次；熔断后每个 process 由 _circuit_open 打 WARNING 放弃出声。"""
        if self._rl_counted:
            return
        self._rl_counted = True
        self._rate_limit_streak += 1
        self._rate_limited_response = True
        logger.warning(
            "minimax bidi rate limit (code=%s event=%s streak=%d) — 弃会话，本轮放弃出声",
            self._status_code_of(ev), ev.get("event") or "-", self._rate_limit_streak,
        )
        print(
            f"MINIMAX_BIDI_RATE_LIMIT code={self._status_code_of(ev)} "
            f"event={ev.get('event') or '-'} streak={self._rate_limit_streak} "
            f"pushed={int(self._response_pushed)}",
            flush=True,
        )

    # ---- 纪元门禁（陈旧音频不得泄进下一 response）----

    @staticmethod
    def _drop_stale_enabled() -> bool:
        return _env_switch("MINIMAX_BIDI_DROP_STALE", True)

    def _begin_response(self) -> None:
        """新 response 分配纪元（只占号；认领=其首个 task_continue 发出后）。"""
        self._epoch_seq += 1
        self._cur_epoch = self._epoch_seq
        self._response_open = True
        self._rate_limited_response = False
        self._rl_counted = False
        self._response_pushed = False
        self._stale_msgs = 0
        self._stale_bytes = 0

    def _drain_epoch_gate(self, epoch: int) -> bool:
        """True=本 response 已认领该纪元（音频放行）；False=认领前残留（丢弃）。"""
        return epoch > 0 and self._claimed_epoch == epoch

    async def _purge_stale(self, ws) -> bool:
        """认领前把连接上残留的上一流音频丢干（门禁的「认领前丢弃」）。

        返回 False=期间收到限流事件（本 response 已弃声，不要再发 task_continue）。
        宿主 recv 是拉取式（无生产版常驻 recv_loop），残留只可能在收尾超时/打断后
        积在缓冲里——首个 task_continue 之前先清一遍，认领后即可靠靠门禁。
        """
        dropped_msgs = 0
        dropped_bytes = 0
        deadline = time.monotonic() + _PURGE_WINDOW_S
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remain, 0.01))
            except asyncio.TimeoutError:
                break
            try:
                ev = json.loads(raw)
            except Exception:  # noqa: BLE001 - 非 JSON 心跳类，忽略
                continue
            if self._rate_limit_hit(ev, raw):
                self._note_rate_limit(ev, raw)
                await self._invalidate()
                return False
            data = ev.get("data") or {}
            hex_audio = data.get("audio") or ""
            if hex_audio:
                dropped_msgs += 1
                dropped_bytes += len(hex_audio) // 2
        if dropped_msgs:
            self._stale_msgs += dropped_msgs
            self._stale_bytes += dropped_bytes
            print(
                f"MINIMAX_BIDI_DROP_STALE epoch={self._cur_epoch} "
                f"msgs={self._stale_msgs} bytes={self._stale_bytes}",
                flush=True,
            )
        return True

    async def _send_continue_claiming(self, ws, text: str) -> bool:
        """本 response 首个 task_continue：先清残留、再发送、最后认领纪元。"""
        if self._drop_stale_enabled() and not await self._purge_stale(ws):
            return False
        await ws.send(json.dumps({"event": "task_continue", "text": text}))
        self._claimed_epoch = self._cur_epoch
        return True

    async def _drain(self, ws, *, flush_wait: bool, epoch: int = 0) -> list[np.ndarray]:
        """收音频；flush_wait=True 时等到 task_flushed（阻塞收尾），否则尽力收。

        纪元门禁：本 response 尚未认领（首个 task_continue 未发）时，连接上冒出的
        音频全是上一流的迟到残留——丢弃，绝唔喂进本轮（MINIMAX_BIDI_DROP_STALE=0 关）。
        """
        out: list[np.ndarray] = []
        stale_logged = False
        deadline = time.monotonic() + _FLUSH_TIMEOUT_S
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                if flush_wait:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remain)
                else:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remain, 0.05))
            except asyncio.TimeoutError:
                break
            try:
                ev = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if self._rate_limit_hit(ev, raw):
                # 限流：弃毒化连接，本轮放弃后续音频（熔断计数在 _note_rate_limit）
                self._note_rate_limit(ev, raw)
                await self._invalidate()
                break
            et = ev.get("event", "")
            if et == "error":
                logger.warning("minimax bidi error event: %s", str(ev)[:200])
                continue
            data = ev.get("data") or {}
            hex_audio = data.get("audio") or ""
            if hex_audio and self._drop_stale_enabled() and not self._drain_epoch_gate(epoch):
                self._stale_msgs += 1
                self._stale_bytes += len(hex_audio) // 2
                if not stale_logged:
                    stale_logged = True
                    print(
                        f"MINIMAX_BIDI_DROP_STALE epoch={epoch} "
                        f"active_epoch={self._claimed_epoch} "
                        f"msgs={self._stale_msgs} bytes={self._stale_bytes}",
                        flush=True,
                    )
                continue
            if hex_audio:
                out.append(_pcm_to_pipeline(bytes.fromhex(hex_audio), self.out_sr))
            if et == "task_flushed":
                break
        return out

    # ---- handler 契约 ----

    def process(self, tts_input: TTSIn) -> Iterator[TTSOut]:
        if self._circuit_open():
            # 熔断：本会话连续限流达上限 → 后续每次 process 打一行 WARNING 并放弃
            # 出声（宿主无 HTTP 回退车道；MINIMAX_TTS_BIDI_GUARD=0 整闸回旧行为）。
            logger.warning(
                "minimax bidi circuit open (rate-limit streak=%d) — skip WS synthesis, "
                "no audio this turn (MINIMAX_TTS_BIDI_GUARD=0 to disable)",
                self._rate_limit_streak,
            )
            return

        if isinstance(tts_input, EndOfResponse):
            had_response = self._response_open
            self._response_open = False
            if self._rate_limited_response:
                # 本 response 已限流弃会话：无音频可吐，收尾尽力（连接已弃）
                self._rate_limited_response = False
                return
            epoch = self._cur_epoch if had_response else 0
        elif isinstance(tts_input, TTSInput):
            if not self._response_open:
                self._begin_response()
            if self._rate_limited_response:
                return  # 本 response 已限流：文本不再碰 WS
            epoch = self._cur_epoch
        else:
            return

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
                chunks = self._loop_thread.call(
                    self._drain(ws, flush_wait=True, epoch=epoch),
                    timeout=_FLUSH_TIMEOUT_S + 3.0,
                )
                for chunk in chunks:
                    self._response_pushed = True
                    yield chunk
            except Exception as exc:  # noqa: BLE001
                logger.warning("minimax bidi flush failed: %r", exc)
            finally:
                if self._response_pushed and not self._rate_limited_response and self._rate_limit_streak:
                    self._rate_limit_streak = 0  # WS 正常出声=连续限流断链
                if self._stale_msgs:
                    print(
                        f"MINIMAX_BIDI_DROP_STALE_TOTAL msgs={self._stale_msgs} "
                        f"bytes={self._stale_bytes}",
                        flush=True,
                    )
                self._rate_limited_response = False
                self._response_pushed = False
            return

        if not isinstance(tts_input, TTSInput):
            return
        text = str(tts_input.text or "").strip()
        if not text:
            return
        try:
            if self._drain_epoch_gate(epoch):
                self._loop_thread.call(_send_continue(ws, text), timeout=3.0)
            else:
                # 本 response 首个 continue：先清残留再认领纪元（门禁唯一认领点）
                sent = self._loop_thread.call(
                    self._send_continue_claiming(ws, text), timeout=3.0
                )
                if not sent:
                    return  # 认领前撞限流：本 response 弃声
            chunks = self._loop_thread.call(
                self._drain(ws, flush_wait=False, epoch=epoch), timeout=1.0
            )
            for chunk in chunks:
                self._response_pushed = True
                yield chunk
        except Exception as exc:  # noqa: BLE001 - 喂包失败：本响应放弃（下轮重连）
            logger.warning("minimax bidi continue failed: %r", exc)
            try:
                self._loop_thread.call(self._invalidate(), timeout=3.0)
            except Exception:  # noqa: BLE001
                pass

    def on_session_end(self) -> None:
        ws, self._ws = self._ws, None
        self._claimed_epoch = -1
        self._response_open = False
        if ws is not None:
            try:
                self._loop_thread.call(self._shutdown_ws(ws), timeout=3.0)
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
