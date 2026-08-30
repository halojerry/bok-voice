from __future__ import annotations

import io
import os
import time
import uuid
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

try:
    import soundfile as sf
except Exception:  # pragma: no cover
    sf = None


SAMPLE_RATE = int(os.environ.get("QWEN3_ASR_SAMPLE_RATE", "16000"))
BACKEND = os.environ.get("QWEN3_ASR_BACKEND", "transformers").lower()
MODEL_PATH = os.environ.get("QWEN3_ASR_MODEL", "Qwen/Qwen3-ASR-0.6B")

app = FastAPI(title="Bok Qwen3-ASR Sidecar")


def _resample(wav: np.ndarray, sr: int, target_sr: int) -> np.ndarray:
    if sr == target_sr:
        return wav
    try:
        import librosa

        return librosa.resample(wav, orig_sr=sr, target_sr=target_sr)
    except Exception:
        duration = wav.shape[0] / float(sr)
        out_len = int(round(duration * target_sr))
        x_old = np.linspace(0.0, duration, num=wav.shape[0], endpoint=False)
        x_new = np.linspace(0.0, duration, num=out_len, endpoint=False)
        return np.interp(x_new, x_old, wav).astype(np.float32)


def _wav_from_pcm16(pcm: bytes) -> tuple[np.ndarray, int]:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, SAMPLE_RATE


class ASRService:
    def __init__(self) -> None:
        self._model: Any | None = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._load_error: str | None = None

    def load(self) -> None:
        if os.environ.get("QWEN3_ASR_DISABLE_LOAD") == "1":
            return
        try:
            import torch
            from qwen_asr import Qwen3ASRModel

            if BACKEND == "vllm":
                self._model = Qwen3ASRModel.LLM(
                    model=MODEL_PATH,
                    gpu_memory_utilization=float(
                        os.environ.get("QWEN3_ASR_GPU_UTIL", "0.7")
                    ),
                    max_new_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
                )
            else:
                device = self._resolve_device()
                dtype = torch.bfloat16 if device == "cuda" else torch.float32
                self._model = Qwen3ASRModel.from_pretrained(
                    MODEL_PATH,
                    dtype=dtype,
                    device_map=device,
                    max_new_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
                )
        except Exception as exc:  # pragma: no cover - model load can fail
            self._load_error = repr(exc)

    @staticmethod
    def _resolve_device() -> str:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def _ensure_loaded(self) -> None:
        if self._load_error:
            raise HTTPException(status_code=503, detail=f"model not ready: {self._load_error}")
        if self._model is None:
            raise HTTPException(status_code=503, detail="model not loaded")

    def start(self) -> str:
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = {
            "chunks": bytearray(),
            "text": "",
            "language": "",
            "partial": False,
            "created_at": time.time(),
            "vllm_state": None,
        }
        return session_id

    def chunk(self, session_id: str, pcm: bytes) -> dict[str, str | bool]:
        session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        session["chunks"].extend(pcm)
        session["last_seen"] = time.time()

        if BACKEND == "vllm":
            self._ensure_loaded()
            wav, sr = _wav_from_pcm16(pcm)
            state = session["vllm_state"]
            if state is None:
                state = self._model.init_streaming_state(
                    unfixed_chunk_num=2,
                    unfixed_token_num=5,
                    chunk_size_sec=2.0,
                )
                session["vllm_state"] = state
            self._model.streaming_transcribe(_resample(wav, sr, SAMPLE_RATE), state)
            session["text"] = getattr(state, "text", "") or ""
            session["language"] = getattr(state, "language", "") or ""
        return {
            "text": session["text"],
            "language": session["language"],
            "partial": BACKEND == "vllm",
        }

    def finish(self, session_id: str) -> dict[str, str | bool]:
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        self._ensure_loaded()

        if BACKEND == "vllm":
            state = session["vllm_state"]
            if state is not None:
                self._model.finish_streaming_transcribe(state)
                return {
                    "text": getattr(state, "text", "") or "",
                    "language": getattr(state, "language", "") or "",
                    "partial": False,
                }

        pcm = bytes(session["chunks"])
        wav, sr = _wav_from_pcm16(pcm)
        if wav.size == 0:
            return {"text": "", "language": "", "partial": False}
        result = self._model.transcribe(
            audio=(_resample(wav, sr, SAMPLE_RATE), SAMPLE_RATE),
            language=None,
        )
        if not result:
            return {"text": "", "language": "", "partial": False}
        first = result[0]
        return {
            "text": getattr(first, "text", "") or "",
            "language": getattr(first, "language", "") or "",
            "partial": False,
        }


service = ASRService()


@app.on_event("startup")
def _startup() -> None:
    service.load()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "backend": BACKEND,
        "model": MODEL_PATH,
        "model_ready": service._model is not None,
        "load_error": service._load_error,
    }


@app.post("/api/start")
def start() -> dict[str, str]:
    return {"session_id": service.start()}


@app.post("/api/chunk")
async def chunk(session_id: str, request: Request) -> dict[str, str | bool]:
    pcm = await request.body()
    return service.chunk(session_id, pcm)


@app.post("/api/finish")
def finish(session_id: str) -> dict[str, str | bool]:
    return service.finish(session_id)
