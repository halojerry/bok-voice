from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

try:
    import soundfile as sf
except Exception:  # pragma: no cover - test environment may not install it
    sf = None

try:
    import librosa
except Exception:  # pragma: no cover
    librosa = None


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("QWEN3_TTS_DATA_DIR", APP_DIR / "data"))
VOICE_REGISTRY_PATH = DATA_DIR / "voice_registry.json"

DEFAULT_PRESET_MODEL = os.environ.get(
    "QWEN3_TTS_PRESET_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
)
DEFAULT_CLONE_MODEL = os.environ.get(
    "QWEN3_TTS_CLONE_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
)
SAMPLE_RATE = int(os.environ.get("QWEN3_TTS_SAMPLE_RATE", "24000"))
MAX_REF_SECONDS = float(os.environ.get("QWEN3_TTS_MAX_REF_SECONDS", "10"))
REF_TARGET_SECONDS = float(os.environ.get("QWEN3_TTS_REF_TARGET_SECONDS", "8"))
BACKEND = os.environ.get("QWEN3_TTS_BACKEND", "transformers").lower()

app = FastAPI(title="Bok Qwen3-TTS Sidecar")


class TTSService:
    def __init__(self) -> None:
        self._preset_model: Any | None = None
        self._clone_model: Any | None = None
        self._clone_prompts: dict[str, Any] = {}
        self._speaker_emb_cache: dict[str, Any] = {}
        self._gen_lock = threading.RLock()
        self._registry: dict[str, dict[str, str]] = self._load_registry()
        self._load_error: str | None = None

    def _load_registry(self) -> dict[str, dict[str, str]]:
        if VOICE_REGISTRY_PATH.exists():
            try:
                return json.loads(VOICE_REGISTRY_PATH.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_registry(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        VOICE_REGISTRY_PATH.write_text(
            json.dumps(self._registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self) -> None:
        if os.environ.get("QWEN3_TTS_DISABLE_LOAD") == "1":
            return
        try:
            if BACKEND == "mlx":
                from mlx_audio.tts.utils import load_model as mlx_load_model

                self._preset_model = mlx_load_model(DEFAULT_PRESET_MODEL)
                self._clone_model = mlx_load_model(DEFAULT_CLONE_MODEL)
                self._install_speaker_embedding_cache()
                if os.environ.get("QWEN3_TTS_WARMUP", "1") == "1":
                    # Synchronous, on the main thread during startup: MLX is
                    # thread-safe, but audioread's CoreAudio backend used by
                    # mlx_audio.load_audio segfaults when called from a
                    # background thread (observed: warmup thread killed the
                    # whole process after the sox fallback warning). Startup
                    # blocks anyway, so warm inline is both safe and free.
                    self._warmup()
                return
            import torch
            from qwen_tts import Qwen3TTSModel

            device = self._resolve_device()
            dtype = torch.bfloat16 if device in ("cuda", "mps") else torch.float32
            attn = "flash_attention_2" if device == "cuda" else "sdpa"
            self._preset_model = Qwen3TTSModel.from_pretrained(
                DEFAULT_PRESET_MODEL,
                device_map=device,
                dtype=dtype,
                attn_implementation=attn,
            )
            # Clone model loads eagerly on the main thread at startup
            # (lazy loading from uvicorn's threadpool segfaults on MPS) and
            # uses float32 on MPS: bf16 breaks the Base/ICL clone generation
            # (runs to max_new_tokens without emitting EOS -> minutes of
            # garbage audio). Preset stays bf16 for low latency.
            clone_dtype = torch.float32 if device == "mps" else dtype
            self._clone_model = Qwen3TTSModel.from_pretrained(
                DEFAULT_CLONE_MODEL,
                device_map=device,
                dtype=clone_dtype,
                attn_implementation=attn,
            )
        except Exception as exc:  # pragma: no cover - model download/load can fail
            self._load_error = repr(exc)

    def _install_speaker_embedding_cache(self) -> None:
        """Cache per-reference-audio speaker embeddings on the MLX clone model.

        The Base model re-runs the speech-tokenizer encoder over the reference
        audio on every synthesis. Registered voices use a fixed file, so the
        encoder output is identical every request. Caching by waveform bytes
        turns the per-request 100-300ms encoder + mel pass into a dict lookup.
        """
        model = self._clone_model
        if model is None or not hasattr(model, "extract_speaker_embedding"):
            return
        orig = model.extract_speaker_embedding

        def cached(audio, *args, **kwargs):
            key: str | None = None
            try:
                arr = np.asarray(audio, dtype=np.float32)
                key = hashlib.sha1(np.ascontiguousarray(arr).tobytes()).hexdigest()
                hit = self._speaker_emb_cache.get(key)
                if hit is not None:
                    return hit
            except Exception:
                key = None
            emb = orig(audio, *args, **kwargs)
            if key is not None:
                self._speaker_emb_cache[key] = emb
            return emb

        model.extract_speaker_embedding = cached  # type: ignore[method-assign]

    def _warmup(self) -> None:
        """Absorb MLX first-call compilation + allocation so the first real
        synthesis is as fast as steady state. Runs synchronously right after
        load, before the server accepts requests.
        """
        if BACKEND != "mlx":
            return
        try:
            t0 = time.perf_counter()
            with self._gen_lock:
                list(
                    self._synthesize_mlx_stream(
                        text="嗯",
                        language="zh",
                        voice="Vivian",
                        instruct="",
                        sample_rate=SAMPLE_RATE,
                        chunk_ms=200,
                        max_new_tokens=64,
                        temperature=None,
                        top_k=None,
                    )
                )
                # Pre-compute speaker embeddings for registered clone voices so
                # the first clone request after boot skips the encoder. Must go
                # through the same loader as request-time synthesis so the
                # waveform-hash cache key matches.
                if self._clone_model is not None and getattr(
                    self._clone_model, "speaker_encoder", None
                ) is not None:
                    for meta in self._registry.values():
                        try:
                            ref = meta.get("ref_audio")
                            if ref and Path(ref).exists():
                                self._clone_model.extract_speaker_embedding(
                                    self._load_ref_audio_mx(ref)
                                )
                        except Exception:
                            continue
            print(
                "QWEN3_TTS_WARMUP",
                f"done={time.perf_counter()-t0:.2f}s",
                f"voices_cached={len(self._speaker_emb_cache)}",
                flush=True,
            )
        except Exception as exc:  # pragma: no cover - warmup must never block boot
            print("QWEN3_TTS_WARMUP_ERR", repr(exc), flush=True)

    @staticmethod
    def _load_ref_audio_mx(path: str) -> Any:
        """Load a clone reference audio as a 24kHz float32 MLX array.

        Uses soundfile (libsndfile) + librosa resampling instead of
        mlx_audio.load_audio, whose audioread probe prints a confusing
        sox-missing warning and uses CoreAudio backends. Keeping the loader
        identical between warmup and request-time synthesis makes the
        speaker-embedding cache (keyed by waveform bytes) hit reliably.
        """
        import mlx.core as mx

        if sf is None:
            raise RuntimeError("soundfile not installed")
        wav, sr = sf.read(path, dtype="float32", always_2d=False)
        if sr != SAMPLE_RATE:
            wav = TTSService._resample(np.asarray(wav, dtype=np.float32), sr, SAMPLE_RATE)
        return mx.array(np.asarray(wav, dtype=np.float32))

    @staticmethod
    def _resolve_device() -> str:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def ensure_loaded(self) -> None:
        if self._load_error:
            raise HTTPException(status_code=503, detail=f"model not ready: {self._load_error}")
        if self._preset_model is None or self._clone_model is None:
            raise HTTPException(status_code=503, detail="model not loaded")

    def list_speakers(self) -> list[str]:
        model = self._preset_model
        if model is None:
            return []
        getter = getattr(model, "get_supported_speakers", None)
        if getter is None:
            return []
        try:
            return list(getter())
        except Exception:
            return []

    def list_voices(self) -> list[dict[str, str]]:
        return [
            {"voice_id": voice_id, **meta}
            for voice_id, meta in self._registry.items()
        ]

    def delete_voice(self, voice_id: str) -> bool:
        """Delete a cloned voice: registry entry + clone prompt cache + ref audio.

        Returns False when the voice is not a registered clone (presets are
        built into the model and can't be deleted).
        """
        meta = self._registry.pop(voice_id, None)
        if meta is None:
            return False
        self._clone_prompts.pop(voice_id, None)
        self._save_registry()
        ref = meta.get("ref_audio")
        if ref:
            try:
                Path(ref).unlink(missing_ok=True)
            except Exception:
                pass
        return True

    async def register_voice(
        self,
        *,
        file: UploadFile,
        voice_id: str,
        ref_text: str,
        language: str = "zh",
    ) -> dict[str, str]:
        if not voice_id or not ref_text:
            raise HTTPException(status_code=400, detail="voice_id and ref_text are required")
        if not file.filename:
            raise HTTPException(status_code=400, detail="audio file is required")
        self.ensure_loaded()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "reference.wav").suffix or ".wav"
        safe = hashlib.sha1(voice_id.encode("utf-8")).hexdigest()[:12]
        target = DATA_DIR / f"{safe}{suffix}"
        raw = await file.read()
        try:
            target.write_bytes(_trim_reference_audio(raw, suffix))
        except Exception:
            # Fall back to the original upload if trimming fails (e.g. an
            # unusual codec soundfile can't decode).
            target.write_bytes(raw)
        if BACKEND != "mlx":
            try:
                prompt = self._clone_model.create_voice_clone_prompt(
                    ref_audio=str(target),
                    ref_text=ref_text,
                )
                self._clone_prompts[voice_id] = prompt
            except Exception as exc:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"voice clone failed: {exc}") from exc
        self._registry[voice_id] = {
            "voice_id": voice_id,
            "ref_audio": str(target),
            "ref_text": ref_text,
            "language": language,
        }
        self._save_registry()
        return self._registry[voice_id]

    def synthesize(
        self,
        *,
        text: str,
        language: str,
        voice: str = "",
        instruct: str = "",
        sample_rate: int = SAMPLE_RATE,
        streaming: bool = True,
        max_new_tokens: int | None = None,
        do_sample: bool | None = None,
        temperature: float | None = None,
        top_k: int | None = None,
    ) -> bytes:
        self.ensure_loaded()
        if not text:
            return b""
        if BACKEND == "mlx":
            return self._synthesize_mlx(
                text=text,
                language=language,
                voice=voice,
                instruct=instruct,
                sample_rate=sample_rate,
                streaming=streaming,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_k=top_k,
            )
        mem_before = _mps_mem()
        t_start = time.perf_counter()
        gen_kwargs = {}
        if max_new_tokens is not None:
            gen_kwargs["max_new_tokens"] = max_new_tokens
        else:
            # Package default is 4096 (≈341s of 12Hz codec audio); a runaway
            # generation stalls MPS for minutes. 512 tokens ≈ 42s audio cap,
            # far beyond any single customer-service sentence.
            gen_kwargs["max_new_tokens"] = int(
                os.environ.get("QWEN3_TTS_MAX_NEW_TOKENS", "512")
            )
        if do_sample is not None:
            gen_kwargs["do_sample"] = do_sample
        if temperature is not None:
            gen_kwargs["temperature"] = temperature
        if top_k is not None:
            gen_kwargs["top_k"] = top_k
        if voice in self._registry or voice in self._clone_prompts:
            language = self._normalize_language(self._clone_model, language)
            wavs, sr = self._synthesize_clone(
                text=text,
                language=language,
                voice=voice,
                streaming=streaming,
                **gen_kwargs,
            )
        else:
            language = self._normalize_language(self._preset_model, language)
            speaker = voice or os.environ.get("QWEN3_TTS_DEFAULT_SPEAKER", "Vivian")
            wavs, sr = self._preset_model.generate_custom_voice(
                text=text,
                language=language or "Auto",
                speaker=speaker,
                instruct=instruct,
                non_streaming_mode=not streaming,
                **gen_kwargs,
            )
        if isinstance(wavs, list):
            wav = np.asarray(wavs[0], dtype=np.float32)
        else:
            wav = np.asarray(wavs, dtype=np.float32)
        if wav.ndim > 1:
            wav = wav.mean(axis=-1)
        wav = self._resample(wav, sr, sample_rate)
        pcm = self._to_pcm16(wav)
        mem_after = _mps_mem()
        _release_mps_cache()
        print(
            "QWEN3_TTS_SYNTH",
            f"text={len(text)} stream={streaming}",
            f"audio={len(pcm)/2/sample_rate:.2f}s",
            f"time={time.perf_counter()-t_start:.2f}s",
            f"mps_before={mem_before[0]:.0f}MB",
            f"mps_after={mem_after[0]:.0f}MB",
            f"mps_driver={mem_after[1]:.0f}MB",
            flush=True,
        )
        return self._compress_pcm(pcm, sample_rate)

    def synthesize_chunks(
        self,
        *,
        text: str,
        language: str,
        voice: str = "",
        instruct: str = "",
        sample_rate: int = SAMPLE_RATE,
        chunk_ms: int = 200,
        max_new_tokens: int | None = None,
        do_sample: bool | None = None,
        temperature: float | None = None,
        top_k: int | None = None,
    ):
        """Streaming synthesis: yields (is_first, pcm_frame) as frames become
        available. The qwen_tts package currently simulates streaming (fast
        generation, complete text input), so frames are produced as the full
        waveform is generated; the endpoint stays chunked so clients can start
        playback as soon as synthesis completes instead of waiting for the
        whole response body.
        """
        self.ensure_loaded()
        if not text:
            return
        if BACKEND == "mlx":
            yield from self._synthesize_mlx_stream(
                text=text,
                language=language,
                voice=voice,
                instruct=instruct,
                sample_rate=sample_rate,
                chunk_ms=chunk_ms,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_k=top_k,
            )
            return
        pcm = self.synthesize(
            text=text,
            language=language,
            voice=voice,
            instruct=instruct,
            sample_rate=sample_rate,
            streaming=True,
            max_new_tokens=max_new_tokens,
            do_sample=do_sample,
            temperature=temperature,
            top_k=top_k,
        )
        frame_bytes = int(sample_rate * chunk_ms / 1000) * 2  # 16-bit mono
        first = True
        for i in range(0, len(pcm), frame_bytes):
            yield first, pcm[i : i + frame_bytes]
            first = False

    @staticmethod
    def _normalize_language(model: Any, language: str) -> str:
        """Map requested language to one the model actually supports.

        Qwen3-TTS does not list Cantonese/yue among its codec language ids, but
        community-verified ICL voice cloning with a Cantonese reference audio
        produces Cantonese output when the language token is `chinese`.
        Fall back to `Auto` for any other unsupported request instead of
        failing the whole turn.
        """
        requested = (language or "").strip().lower()
        if not requested:
            return "Auto"
        getter = getattr(model, "get_supported_languages", None)
        supported = set()
        if callable(getter):
            try:
                supported = {str(s).lower() for s in (getter() or [])}
            except Exception:
                supported = set()
        if supported and requested in supported:
            return language
        if requested in {"yue", "cantonese", "cantonese_chinese"}:
            return "chinese"
        return "Auto"

    def _synthesize_clone(
        self, *, text: str, language: str, voice: str, streaming: bool = True, **gen_kwargs
    ) -> tuple[Any, int]:
        prompt = self._clone_prompts.get(voice)
        if prompt is None:
            meta = self._registry[voice]
            prompt = self._clone_model.create_voice_clone_prompt(
                ref_audio=meta["ref_audio"],
                ref_text=meta["ref_text"],
            )
            self._clone_prompts[voice] = prompt
        return self._clone_model.generate_voice_clone(
            text=text,
            language=language or "Auto",
            voice_clone_prompt=prompt,
            non_streaming_mode=not streaming,
            **gen_kwargs,
        )

    def _mlx_generate(
        self,
        *,
        text: str,
        language: str,
        voice: str,
        instruct: str,
        stream: bool,
        streaming_interval: float = 0.5,
        max_tokens: int,
        temperature: float | None,
        top_k: int | None,
    ):
        """Yield (model, generator) for the preset or clone MLX path."""
        gen_kwargs: dict[str, Any] = {
            "max_tokens": max_tokens,
            "stream": stream,
            "streaming_interval": streaming_interval,
        }
        if temperature is not None:
            gen_kwargs["temperature"] = temperature
        if top_k is not None:
            gen_kwargs["top_k"] = top_k
        if voice in self._registry:
            meta = self._registry[voice]
            lang = self._normalize_language(self._clone_model, language)
            yield self._clone_model, self._clone_model.generate(
                text=text,
                ref_audio=self._load_ref_audio_mx(meta["ref_audio"]),
                ref_text=meta["ref_text"],
                lang_code=lang or "auto",
                **gen_kwargs,
            )
            return
        lang = self._normalize_language(self._preset_model, language)
        speaker = voice or os.environ.get("QWEN3_TTS_DEFAULT_SPEAKER", "Vivian")
        yield self._preset_model, self._preset_model.generate_custom_voice(
            text=text,
            speaker=speaker,
            language=lang or "auto",
            instruct=instruct,
            **gen_kwargs,
        )

    def _synthesize_mlx(
        self,
        *,
        text: str,
        language: str,
        voice: str,
        instruct: str,
        sample_rate: int,
        streaming: bool,
        max_new_tokens: int | None,
        temperature: float | None,
        top_k: int | None,
    ) -> bytes:
        del streaming  # non-streaming full generation
        max_tokens = int(
            max_new_tokens
            if max_new_tokens is not None
            else os.environ.get("QWEN3_TTS_MAX_NEW_TOKENS", "512")
        )
        with self._gen_lock:
            for model, gen in self._mlx_generate(
                text=text,
                language=language,
                voice=voice,
                instruct=instruct,
                stream=False,
                max_tokens=max_tokens,
                temperature=temperature,
                top_k=top_k,
            ):
                results = list(gen)
                wavs = [
                    np.asarray(r.audio, dtype=np.float32)
                    for r in results
                    if getattr(r, "audio", None) is not None
                ]
                if not wavs:
                    return b""
                wav = np.concatenate(wavs) if len(wavs) > 1 else wavs[0]
                if wav.ndim > 1:
                    wav = wav.mean(axis=-1)
                return self._compress_pcm(
                    self._to_pcm16(
                        self._resample(wav, model.sample_rate, sample_rate)
                    ),
                    sample_rate,
                )
            return b""

    @staticmethod
    def _compress_pcm(pcm: bytes, sample_rate: int) -> bytes:
        """Clamp over-long silent runs in an already-rendered full PCM buffer."""
        if not SILENCE_COMPRESS or not pcm:
            return pcm
        comp = _SilenceCompressor(sample_rate=sample_rate)
        out = bytearray()
        for fr in comp.push(pcm, len(pcm)):
            out.extend(fr)
        for fr in comp.flush(len(pcm)):
            out.extend(fr)
        return bytes(out)

    def _synthesize_mlx_stream(
        self,
        *,
        text: str,
        language: str,
        voice: str,
        instruct: str,
        sample_rate: int,
        chunk_ms: int,
        max_new_tokens: int | None,
        temperature: float | None,
        top_k: int | None,
    ):
        max_tokens = int(
            max_new_tokens
            if max_new_tokens is not None
            else os.environ.get("QWEN3_TTS_MAX_NEW_TOKENS", "512")
        )
        # Yield cadence for the model's streaming decoder. Independent of the
        # HTTP frame size: 0.1s ≈ one 12Hz codec token per yield, so the first
        # audio packet leaves the sidecar after a single decode step instead of
        # waiting for two (the old 0.2s floor).
        interval = max(
            0.05,
            float(os.environ.get("QWEN3_TTS_STREAM_INTERVAL", "0.1")),
        )
        frame_bytes = int(sample_rate * chunk_ms / 1000) * 2
        first = True
        # Qwen3-TTS generates unnaturally long inter-sentence pauses (0.5-1.4s
        # of near-silence at 。！, observed on clone and preset alike). Clamp
        # them at the byte level so multi-sentence replies don't sound choppy.
        # Disable with QWEN3_TTS_SILENCE_COMPRESS=0 to keep raw pacing.
        compressor = (
            _SilenceCompressor(sample_rate=sample_rate)
            if SILENCE_COMPRESS
            else None
        )
        with self._gen_lock:
            for model, gen in self._mlx_generate(
                text=text,
                language=language,
                voice=voice,
                instruct=instruct,
                stream=True,
                streaming_interval=interval,
                max_tokens=max_tokens,
                temperature=temperature,
                top_k=top_k,
            ):
                for result in gen:
                    chunk = getattr(result, "audio", None)
                    if chunk is None:
                        continue
                    chunk = np.asarray(chunk, dtype=np.float32)
                    if chunk.ndim > 1:
                        chunk = chunk.mean(axis=-1)
                    pcm = self._to_pcm16(
                        self._resample(chunk, model.sample_rate, sample_rate)
                    )
                    if compressor is None:
                        for i in range(0, len(pcm), frame_bytes):
                            yield first, pcm[i : i + frame_bytes]
                            first = False
                        continue
                    # Feed raw model PCM through the compressor; it re-chunks
                    # into the HTTP frame size after clamping long silences.
                    for frame in compressor.push(pcm, frame_bytes):
                        yield first, frame
                        first = False
            if compressor is not None:
                for frame in compressor.flush(frame_bytes):
                    yield first, frame
                    first = False

    @staticmethod
    def _resample(wav: np.ndarray, sr: int, target_sr: int) -> np.ndarray:
        if sr == target_sr:
            return wav
        if librosa is not None:
            return librosa.resample(wav, orig_sr=sr, target_sr=target_sr)
        duration = wav.shape[0] / float(sr)
        out_len = int(round(duration * target_sr))
        x_old = np.linspace(0.0, duration, num=wav.shape[0], endpoint=False)
        x_new = np.linspace(0.0, duration, num=out_len, endpoint=False)
        return np.interp(x_new, x_old, wav).astype(np.float32)

    @staticmethod
    def _to_pcm16(wav: np.ndarray) -> bytes:
        wav = np.clip(wav, -1.0, 1.0)
        return (wav * 32767.0).astype(np.int16).tobytes()


SILENCE_COMPRESS = os.environ.get("QWEN3_TTS_SILENCE_COMPRESS", "1") == "1"
SILENCE_KEEP_SEC = float(os.environ.get("QWEN3_TTS_SILENCE_KEEP_SEC", "0.3"))
SILENCE_FRAME_SEC = float(os.environ.get("QWEN3_TTS_SILENCE_FRAME_SEC", "0.02"))
SILENCE_RMS = float(os.environ.get("QWEN3_TTS_SILENCE_RMS", "0.018"))


class _SilenceCompressor:
    """Clamp unnaturally long runs of near-silence in TTS audio.

    Qwen3-TTS leaves 0.5-1.4s gaps between sentences (worse on multi-sentence
    replies), which playback renders as choppy/stuttering audio. This streams
    the generated PCM and caps any silent run at ``SILENCE_KEEP_SEC`` while
    leaving speech and short natural pauses untouched.

    Implementation: the raw model PCM is chopped into tiny ``SILENCE_FRAME_SEC``
    (20ms) analysis frames. Frames below an RMS gate are buffered (not emitted).
    When speech resumes, only the first ``SILENCE_KEEP_SEC`` of the buffered
    silence is released; the rest (the over-long tail) is dropped. Output is
    re-chunked into ``out_frame_bytes`` so downstream 200ms HTTP frame semantics
    are preserved. A trailing silent run at end-of-stream is kept whole —
    trimming it would clip the natural end-of-utterance decay.
    """

    def __init__(self, *, sample_rate: int):
        self._frame_bytes = max(2, int(sample_rate * SILENCE_FRAME_SEC) * 2)
        self._keep_bytes = max(self._frame_bytes, int(sample_rate * SILENCE_KEEP_SEC) * 2)
        self._pending = bytearray()
        self._carry = bytearray()

    def push(self, pcm: bytes, out_frame_bytes: int) -> list[bytes]:
        if pcm:
            self._carry.extend(pcm)
        frames = []
        # Chop only complete frames; any remainder stays for the next call so
        # callers may feed arbitrary chunk sizes without losing audio.
        n = len(self._carry) - (len(self._carry) % self._frame_bytes)
        for i in range(0, n, self._frame_bytes):
            frames.append(bytes(self._carry[i : i + self._frame_bytes]))
        del self._carry[:n]
        out = bytearray()
        for fr in frames:
            if self._is_silence(fr):
                self._pending.extend(fr)
            else:
                if self._pending:
                    keep = self._pending[: self._keep_bytes]
                    out.extend(keep)
                    self._pending.clear()
                out.extend(fr)
        return self._chunk(out, out_frame_bytes)

    def flush(self, out_frame_bytes: int) -> list[bytes]:
        # Emit any final partial frame, then the pending (trailing) silence.
        tail = bytearray(self._carry)
        self._carry.clear()
        tail.extend(self._pending)
        self._pending.clear()
        return self._chunk(tail, out_frame_bytes)

    @staticmethod
    def _chunk(buf: bytearray, out_frame_bytes: int) -> list[bytes]:
        if not buf:
            return []
        return [
            bytes(buf[i : i + out_frame_bytes])
            for i in range(0, len(buf), out_frame_bytes)
        ]

    def _is_silence(self, frame: bytes) -> bool:
        if len(frame) < 2:
            return True
        # RMS over 16-bit mono samples, compared to a small absolute gate.
        a = np.frombuffer(frame, dtype="<i2").astype(np.float32)
        rms = float(np.sqrt(np.mean(a * a)) / 32768.0)
        return rms < SILENCE_RMS


def _mps_mem() -> tuple[float, float]:
    """Return (current_allocated_mb, driver_allocated_mb) for MPS, or (0, 0)."""
    try:
        import torch

        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return (
                torch.mps.current_allocated_memory() / 1024 / 1024,
                torch.mps.driver_allocated_memory() / 1024 / 1024,
            )
    except Exception:
        pass
    return 0.0, 0.0


def _release_mps_cache() -> None:
    """Return cached MPS tensors (KV cache, intermediates) to the driver so
    repeated requests cannot accumulate unified-memory pressure."""
    try:
        import torch

        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def _trim_reference_audio(raw: bytes, suffix: str) -> bytes:
    """Limit a voice-clone reference clip to REF_TARGET_SECONDS.

    A long reference (e.g. a 138s test wav) produces an oversized ref_code
    that overflows the ICL context: generation degenerates into a repetition
    loop that never emits EOS, burning CPU/MPS for minutes and exhausting
    memory. Keep the loudest middle segment so the cloned timbre is preserved
    without the context blowup.
    """
    if sf is None or librosa is None:
        return raw
    try:
        wav, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    except Exception:
        return raw
    if wav.ndim > 1:
        wav = wav.mean(axis=-1)
    if wav.shape[0] / sr <= MAX_REF_SECONDS:
        return raw
    # Keep a window centered on the loudest RMS region.
    total = wav.shape[0]
    target_n = int(sr * REF_TARGET_SECONDS)
    win = int(sr * 0.5)
    rms = []
    for start in range(0, total - win, win):
        seg = wav[start : start + win]
        rms.append((float(np.sqrt(np.mean(seg**2))), start))
    if not rms:
        start = 0
    else:
        _, best = max(rms, key=lambda x: x[0])
        start = max(0, min(best + win // 2 - target_n // 2, total - target_n))
    seg = wav[start : start + target_n]
    if len(seg) < target_n:
        seg = np.pad(seg, (0, target_n - len(seg)))
    out = io.BytesIO()
    sf.write(out, seg, sr, format="WAV", subtype="PCM_16")
    return out.getvalue()

service = TTSService()


@app.on_event("startup")
def _startup() -> None:
    service.load()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "backend": BACKEND,
        "model_ready": service._preset_model is not None,
        "clone_model_ready": service._clone_model is not None,
        "load_error": service._load_error,
    }


@app.get("/v1/speakers")
def list_speakers() -> list[str]:
    return service.list_speakers()


@app.get("/v1/voices")
def list_voices() -> list[dict[str, str]]:
    return service.list_voices()


@app.post("/v1/voices/register")
async def register_voice(
    file: UploadFile = File(...),
    voice_id: str = Form(...),
    ref_text: str = Form(...),
    language: str = Form("zh"),
) -> dict[str, str]:
    return await service.register_voice(
        file=file,
        voice_id=voice_id,
        ref_text=ref_text,
        language=language,
    )


@app.delete("/v1/voices/{voice_id}")
def delete_voice(voice_id: str) -> dict:
    if not service.delete_voice(voice_id):
        raise HTTPException(status_code=404, detail=f"clone voice not found: {voice_id}")
    return {"voice_id": voice_id, "deleted": True}


@app.post("/v1/audio/speech")
async def audio_speech(payload: dict[str, Any]) -> Response:
    text = str(payload.get("input") or payload.get("text") or "")
    language = str(payload.get("language") or "Auto")
    voice = str(payload.get("voice") or "")
    instruct = str(payload.get("instruct") or "")
    sample_rate = int(payload.get("sample_rate") or SAMPLE_RATE)
    streaming = bool(payload.get("streaming", True))
    chunk_ms = int(payload.get("chunk_ms") or 200)
    max_new_tokens = payload.get("max_new_tokens")
    do_sample = payload.get("do_sample")
    temperature = payload.get("temperature")
    top_k = payload.get("top_k")

    if streaming:
        def _gen():
            # Sync generator: Starlette iterates it in a threadpool so the
            # blocking model inference never stalls the event loop.
            yield from (
                frame
                for _, frame in service.synthesize_chunks(
                    text=text,
                    language=language,
                    voice=voice,
                    instruct=instruct,
                    sample_rate=sample_rate,
                    chunk_ms=chunk_ms,
                    max_new_tokens=max_new_tokens,
                    do_sample=do_sample,
                    temperature=temperature,
                    top_k=top_k,
                )
            )

        return StreamingResponse(
            _gen(),
            media_type="audio/pcm",
            headers={
                "X-Sample-Rate": str(sample_rate),
                "X-Streaming": "true",
                "X-Chunk-Ms": str(chunk_ms),
                "Cache-Control": "no-cache",
            },
        )

    pcm = service.synthesize(
        text=text,
        language=language,
        voice=voice,
        instruct=instruct,
        sample_rate=sample_rate,
        streaming=False,
        max_new_tokens=max_new_tokens,
        do_sample=do_sample,
        temperature=temperature,
        top_k=top_k,
    )
    return Response(
        content=pcm,
        media_type="audio/pcm",
        headers={
            "X-Sample-Rate": str(sample_rate),
            "X-Streaming": "false",
        },
    )
