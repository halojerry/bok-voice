"""本地 TTS 音频缓存层(2026-09-08)。

把「可预知文本」(话术直念线/预生成垫话/快路应答)的合成音频缓存到 app-data,
播放时绕过 MiniMax 云端直出 PCM——命中行 ~0ms 出声,零云调用。

设计约束(docs/superpowers/specs/2026-09-08-tts-cache-design.md):
- 只拦 synthesize()(整句)路径;stream()(LLM 回复)纯透传+首音频回调,
  任何流式拦截都被调研证实是负收益(输入侧缓冲推迟首包)。
- 未命中走内芯的 tee:边播边收集,完整消费成功才落盘(打断/失败不存半截);
  内芯 _emit_beep 置 _emitted_beep 旗标,beep 音频绝不入缓存。
- key=sha1(归一化文本+voice_id+model 档+sample_rate):音色/模型档变更自然
  全量失效。归一化只统一全半角标点+空白,不做数字归一(号码读法逐字对应)。
- 开关 BOK_TTS_CACHE=0 一键全关;任何缓存 I/O 异常静默降级,绝不影响播放。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import AsyncIterable
from pathlib import Path

from livekit.agents import APIConnectOptions, tts, utils
from livekit import rtc

_ENABLE_ENV = "BOK_TTS_CACHE"
_DIR_ENV = "BOK_TTS_CACHE_DIR"
_MAX_ENTRIES_ENV = "BOK_TTS_CACHE_MAX"
_DEFAULT_MAX_ENTRIES = 500

# 全角→半角标点/空白统一(缓存 key 归一化用;不做数字归一——号码读法逐字对应,
# 归一会把不同号码错配到同一段音频)。
_PUNCT_TRANS = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "！": "!",
        "？": "?",
        "；": ";",
        "：": ":",
        "（": "(",
        "）": ")",
        "、": ",",
        "～": "~",
        "—": "-",
        "–": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "　": " ",
    }
)


def normalize_cache_text(text: str) -> str:
    t = str(text or "").translate(_PUNCT_TRANS)
    return re.sub(r"\s+", " ", t).strip()


def cache_key(text: str, *, voice_id: str, model: str, sample_rate: int) -> str:
    norm = normalize_cache_text(text)
    raw = f"{norm}\x1f{voice_id}\x1f{model}\x1f{int(sample_rate)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def tts_cache_enabled() -> bool:
    return os.environ.get(_ENABLE_ENV, "1") == "1"


def default_cache_dir() -> Path:
    """app-data 下 tts-cache(与 bok.py app_data_dir 同一布局;env 可覆盖)。"""
    explicit = os.environ.get(_DIR_ENV, "").strip()
    if explicit:
        return Path(explicit) / "tts-cache"
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
    else:
        base = Path.home() / ".local/share"
    return base / "BokVoice" / "tts-cache"


def pcm_to_frames(pcm: bytes, sample_rate: int, num_channels: int = 1) -> list[rtc.AudioFrame]:
    """裸 PCM(s16le 单声道)切 200ms AudioFrame(与 livekit_plugins 组帧同口径)。"""
    frame_bytes = int(sample_rate / 5) * 2
    frames: list[rtc.AudioFrame] = []
    for off in range(0, len(pcm), frame_bytes):
        chunk = bytes(pcm[off : off + frame_bytes])
        if len(chunk) < 2:
            break
        frames.append(
            rtc.AudioFrame(
                data=chunk,
                sample_rate=sample_rate,
                num_channels=num_channels,
                samples_per_channel=len(chunk) // 2 // num_channels,
            )
        )
    return frames


async def frames_aiter(frames: list[rtc.AudioFrame]) -> AsyncIterable[rtc.AudioFrame]:
    """list → AsyncIterable(session.say(audio=) 契约)。"""
    for f in frames:
        yield f
        await asyncio.sleep(0)


class TtsAudioCache:
    """磁盘音频缓存:sha1 → pcm(24kHz s16le 单声道)+json 元数据,LRU 有界。"""

    def __init__(self, root: Path, *, sample_rate: int = 24000, max_entries: int | None = None):
        self.root = Path(root)
        self.sample_rate = int(sample_rate)
        env_max = os.environ.get(_MAX_ENTRIES_ENV, "").strip()
        self.max_entries = int(env_max) if env_max else (max_entries or _DEFAULT_MAX_ENTRIES)

    def key_for(self, text: str, *, voice: str, model: str) -> str:
        return cache_key(text, voice_id=voice or "", model=model or "", sample_rate=self.sample_rate)

    def _pcm_path(self, key: str) -> Path:
        return self.root / f"{key}.pcm"

    def _meta_path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> bytes | None:
        try:
            path = self._pcm_path(key)
            pcm = path.read_bytes()
            if not pcm:
                return None
            try:
                os.utime(path, None)  # LRU touch
            except OSError:
                pass
            return pcm
        except OSError:
            return None

    def lookup(self, text: str, *, voice: str, model: str) -> bytes | None:
        return self.get(self.key_for(text, voice=voice, model=model))

    def store(
        self, key: str, pcm: bytes, *, text: str, voice: str, model: str, pin: bool = False
    ) -> bool:
        """原子写入+LRU 淘汰;任何失败静默 False(缓存永不影响播放)。

        pin=True=罐头集(垫话/QA 应答/静态直念线,pregen 物化)——永不逐出
        (2026-09-10:逐对象开场白这类无界动态条目会把罐头挤出 LRU,音色一致性
        静默破功,垫话回落固定资产音)。无界条目(逐对象开场白)保持不钉。
        """
        if not pcm:
            return False
        pcm = _trim_lead_silence_safe(pcm, self.sample_rate)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self._pcm_path(key).with_suffix(".tmp")
            tmp.write_bytes(pcm)
            os.replace(tmp, self._pcm_path(key))
            meta = {
                "text": str(text or ""),
                "voice": voice or "",
                "model": model or "",
                "sample_rate": self.sample_rate,
                "bytes": len(pcm),
                "stored_at": time.time(),
            }
            if pin:
                meta["pinned"] = True
            mtmp = self._meta_path(key).with_suffix(".mtmp")
            mtmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            os.replace(mtmp, self._meta_path(key))
            self._evict()
            return True
        except OSError:
            return False

    def _is_pinned(self, key: str) -> bool:
        try:
            return bool(json.loads(self._meta_path(key).read_text(encoding="utf-8")).get("pinned"))
        except (OSError, ValueError):
            return False

    def _evict(self) -> None:
        """淘汰最旧的未钉条目(只读候选区 meta,通常 0-1 个,不扫全库)。"""
        try:
            entries = sorted(
                (p for p in self.root.glob("*.pcm")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in entries[self.max_entries :]:
                if self._is_pinned(stale.stem):
                    continue
                stale.unlink(missing_ok=True)
                self._meta_path(stale.stem).unlink(missing_ok=True)
        except OSError:
            pass


def _trim_lead_silence_safe(pcm: bytes, sample_rate: int) -> bytes:
    """复用 livekit_plugins 的首包静音修剪(纯函数);失败原样返回。"""
    try:
        from .providers.livekit_plugins import _trim_lead_silence

        out, _ms = _trim_lead_silence(pcm, sample_rate)
        return out
    except Exception:  # pragma: no cover - 修剪失败存原样(多 ≤200ms 静音,无损)
        return pcm


class _CachedChunkedStream(tts.ChunkedStream):
    """缓存命中:本地 PCM 组流,零云调用。"""

    def __init__(self, *, tts_: tts.TTS, text: str, pcm: bytes):
        super().__init__(tts=tts_, input_text=text, conn_options=APIConnectOptions(max_retry=0))
        self._pcm = pcm

    async def _run(self, output_emitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )
        frame_bytes = int(self._tts.sample_rate / 5) * 2
        for off in range(0, len(self._pcm), frame_bytes):
            output_emitter.push(bytes(self._pcm[off : off + frame_bytes]))
        output_emitter.flush()


class _StoreChunkedStream(tts.ChunkedStream):
    """未命中 tee:转发内芯音频边播边收集;完整消费成功才落盘。

    打断/异常/内芯 beep(_emitted_beep 旗标)一律不存——缓存里绝不能进半截或
    错误提示音,否则毒化后该行永远播错误音频。
    """

    def __init__(self, *, tts_: tts.TTS, inner: tts.ChunkedStream, on_done) -> None:
        super().__init__(
            tts=tts_,
            input_text=str(getattr(inner, "input_text", "") or ""),
            conn_options=APIConnectOptions(max_retry=0),
        )
        self._inner = inner
        self._on_done = on_done

    async def _run(self, output_emitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )
        buf = bytearray()
        try:
            async with self._inner:
                async for ev in self._inner:
                    data = ev.frame.data
                    chunk = data.tobytes() if isinstance(data, memoryview) else bytes(data)
                    buf.extend(chunk)
                    output_emitter.push(chunk)
                output_emitter.flush()
        except BaseException:
            raise
        else:
            if not getattr(self._inner, "_emitted_beep", False):
                try:
                    self._on_done(bytes(buf))
                except Exception:  # noqa: BLE001 - 落盘失败静默
                    pass


class _RelaySynthesizeStream(tts.SynthesizeStream):
    """stream() 透传:输入逐条转内芯,音频转发本 emitter(整轮单 segment,同 bidi 口径)。

    首个音频帧触发 on_first_audio 回调一次(PR-2 垫话),随后向 hold_provider
    询问扣压时长——播放排序契约(2026-09-10):在播垫话必须播完,垫话→gap→回复,
    不再掐垫话;帧在本流内缓冲到点再放,内芯 iterator 惰性天然背压。
    """

    def __init__(self, *, tts_: tts.TTS, inner: tts.SynthesizeStream, on_first_audio, hold_provider=None) -> None:
        super().__init__(tts=tts_, conn_options=APIConnectOptions(max_retry=0))
        self._inner = inner
        self._on_first_audio = on_first_audio
        self._hold_provider = hold_provider
        self._fired = False

    # ⚠️ 勿覆写 _metrics_monitor_task:基类监视器在转发帧上算 ttfb/audio 时长并
    # emit tts_metrics。曾 pass 掉(垫话 PR,注释误以为内芯會转发,实际 session 只
    # 监听包装层)→ PERCEIVED_MS 北极星缺 tts 段、turns 账本 perceived_ms 哑火
    # (2026-09-10 实测恢复)。

    async def _run(self, output_emitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=True,
        )
        output_emitter.start_segment(segment_id=utils.shortuuid())

        async def _forward_input() -> None:
            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    self._inner.flush()
                    continue
                self._inner.push_text(data)
            self._inner.end_input()

        async def _relay_audio() -> None:
            async with self._inner:
                async for ev in self._inner:
                    if not self._fired:
                        self._fired = True
                        self._mark_started()
                        try:
                            self._on_first_audio()
                        except Exception:  # noqa: BLE001 - 回调失败唔阻播放
                            pass
                        if self._hold_provider is not None:
                            try:
                                hold = float(self._hold_provider() or 0.0)
                            except Exception:  # noqa: BLE001 - 询时失败=不扣压
                                hold = 0.0
                            if hold > 0:
                                print(
                                    f"BOK_FILLER hold reply {hold * 1000:.0f}ms"
                                    " (垫话播完+gap 后衔接回复)",
                                    flush=True,
                                )
                                await asyncio.sleep(hold)
                    data = ev.frame.data
                    chunk = data.tobytes() if isinstance(data, memoryview) else bytes(data)
                    output_emitter.push(chunk)
            output_emitter.flush()

        tasks = [asyncio.create_task(_forward_input()), asyncio.create_task(_relay_audio())]
        try:
            await asyncio.gather(*tasks)
        finally:
            await utils.aio.cancel_and_wait(*tasks)

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            await super().aclose()


class CachedTTS(tts.TTS):
    """包装现有 TTS:仅 synthesize() 走缓存,stream()/事件/预热全透传。

    模板=官方 StreamAdapter 组合姿势(转发 model/provider/metrics/aclose)。
    voice/model 取值经 provider 回调(合成时点的语言锚定音色),包装层不感知。
    """

    def __init__(
        self,
        wrapped: tts.TTS,
        *,
        cache: TtsAudioCache,
        voice_provider=None,
        model_provider=None,
    ) -> None:
        caps = wrapped.capabilities
        super().__init__(
            capabilities=tts.TTSCapabilities(
                streaming=caps.streaming, aligned_transcript=caps.aligned_transcript
            ),
            sample_rate=wrapped.sample_rate,
            num_channels=wrapped.num_channels,
        )
        self._wrapped = wrapped
        self._cache = cache
        self._voice_provider = voice_provider or (lambda: "")
        self._model_provider = model_provider or (lambda: "")
        self._first_audio_cbs: list = []
        self._hold_provider = None  # 垫话扣压(FillerDirector.hold_if_playing),agent 侧注入
        wrapped.on("metrics_collected", self._forward_metric)

    @property
    def model(self) -> str:
        return self._wrapped.model

    @property
    def provider(self) -> str:
        return self._wrapped.provider

    def resolved_voice(self) -> str:
        try:
            return str(self._voice_provider() or "")
        except Exception:
            return ""

    def resolved_model(self) -> str:
        try:
            return str(self._model_provider() or "")
        except Exception:
            return ""

    def add_first_audio_listener(self, cb) -> None:
        self._first_audio_cbs.append(cb)

    def set_hold_provider(self, cb) -> None:
        """注入垫话扣压询问(FillerDirector.hold_if_playing)——回复首帧到达时
        若垫话在播,返回「垫话剩余+gap」秒数,帧缓冲到点再放(播放排序契约)。"""
        self._hold_provider = cb

    def _fire_first_audio(self) -> None:
        for cb in list(self._first_audio_cbs):
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass

    def _forward_metric(self, *args, **kwargs) -> None:
        self.emit("metrics_collected", *args, **kwargs)

    def _norm_conn_options(self, conn_options):
        """conn_options 归一:None → 官方默认(含 max_retry)。

        内芯是官方 FallbackAdapter(hd→turbo 回退链)时,其 ChunkedStream 会读
        conn_options.max_retry——透传 None 直接 AttributeError(2026-09-09 en 腿
        _tts_task 崩实证)。自有内芯不读这键,以前 None 无害纯属侥幸。
        """
        if conn_options is None:
            from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

            return DEFAULT_API_CONNECT_OPTIONS
        return conn_options

    def synthesize(self, text: str, *, conn_options=None) -> tts.ChunkedStream:
        text = str(text or "")
        conn_options = self._norm_conn_options(conn_options)
        key = self._cache.key_for(
            text, voice=self.resolved_voice(), model=self.resolved_model()
        )
        pcm = self._cache.get(key)
        if pcm is not None:
            print(f"TTS_CACHE hit=1 key={key[:10]} chars={len(text)}", flush=True)
            return _CachedChunkedStream(tts_=self, text=text, pcm=pcm)
        print(f"TTS_CACHE hit=0 key={key[:10]} chars={len(text)}", flush=True)
        voice = self.resolved_voice()
        if not voice:
            return self._wrapped.synthesize(text, conn_options=conn_options)
        inner = self._wrapped.synthesize(text, conn_options=conn_options)

        def _done(out: bytes) -> None:
            ok = self._cache.store(key, out, text=text, voice=voice, model=self.resolved_model())
            print(f"TTS_CACHE stored=1 ok={int(ok)} key={key[:10]} bytes={len(out)}", flush=True)

        return _StoreChunkedStream(tts_=self, inner=inner, on_done=_done)

    def stream(self, *, conn_options=None) -> tts.SynthesizeStream:
        inner = self._wrapped.stream(conn_options=self._norm_conn_options(conn_options))
        return _RelaySynthesizeStream(
            tts_=self, inner=inner, on_first_audio=self._fire_first_audio,
            hold_provider=self._hold_provider,
        )

    def prewarm(self) -> None:
        self._wrapped.prewarm()

    async def aclose(self) -> None:
        self._wrapped.off("metrics_collected", self._forward_metric)
        await self._wrapped.aclose()
