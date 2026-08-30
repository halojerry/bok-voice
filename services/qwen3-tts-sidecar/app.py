from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

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

app = FastAPI(title="Bok Qwen3-TTS Sidecar")


class TTSService:
    def __init__(self) -> None:
        self._preset_model: Any | None = None
        self._clone_model: Any | None = None
        self._clone_prompts: dict[str, Any] = {}
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
            import torch
            from qwen_tts import Qwen3TTSModel

            device = self._resolve_device()
            dtype = torch.bfloat16 if device == "cuda" else torch.float32
            attn = "flash_attention_2" if device == "cuda" else "sdpa"
            self._preset_model = Qwen3TTSModel.from_pretrained(
                DEFAULT_PRESET_MODEL,
                device_map=device,
                dtype=dtype,
                attn_implementation=attn,
            )
            self._clone_model = Qwen3TTSModel.from_pretrained(
                DEFAULT_CLONE_MODEL,
                device_map=device,
                dtype=dtype,
                attn_implementation=attn,
            )
        except Exception as exc:  # pragma: no cover - model download/load can fail
            self._load_error = repr(exc)

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
        target.write_bytes(await file.read())
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
    ) -> bytes:
        self.ensure_loaded()
        if not text:
            return b""
        if voice in self._registry or voice in self._clone_prompts:
            language = self._normalize_language(self._clone_model, language)
            wavs, sr = self._synthesize_clone(
                text=text,
                language=language,
                voice=voice,
            )
        else:
            language = self._normalize_language(self._preset_model, language)
            speaker = voice or os.environ.get("QWEN3_TTS_DEFAULT_SPEAKER", "Vivian")
            wavs, sr = self._preset_model.generate_custom_voice(
                text=text,
                language=language or "Auto",
                speaker=speaker,
                instruct=instruct,
            )
        if isinstance(wavs, list):
            wav = np.asarray(wavs[0], dtype=np.float32)
        else:
            wav = np.asarray(wavs, dtype=np.float32)
        if wav.ndim > 1:
            wav = wav.mean(axis=-1)
        wav = self._resample(wav, sr, sample_rate)
        return self._to_pcm16(wav)

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

    def _synthesize_clone(self, *, text: str, language: str, voice: str) -> tuple[Any, int]:
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
        )

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


service = TTSService()


@app.on_event("startup")
def _startup() -> None:
    service.load()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model_ready": service._preset_model is not None and service._clone_model is not None,
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


@app.post("/v1/audio/speech")
async def audio_speech(payload: dict[str, Any]) -> Response:
    text = str(payload.get("input") or payload.get("text") or "")
    language = str(payload.get("language") or "Auto")
    voice = str(payload.get("voice") or "")
    instruct = str(payload.get("instruct") or "")
    sample_rate = int(payload.get("sample_rate") or SAMPLE_RATE)
    pcm = service.synthesize(
        text=text,
        language=language,
        voice=voice,
        instruct=instruct,
        sample_rate=sample_rate,
    )
    return Response(
        content=pcm,
        media_type="audio/pcm",
        headers={"X-Sample-Rate": str(sample_rate)},
    )
