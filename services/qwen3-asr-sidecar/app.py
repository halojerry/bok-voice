from __future__ import annotations

import io
import os
import threading
import time
import uuid
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
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

def _fallback_language(text: str) -> str:
    """Qwen3-ASR's raw output sometimes omits the `language ...<asr_text>`
    metadata tag (observed on CPU), leaving the language field empty. Fall
    back to a character-set heuristic so the agent's LanguageState can still
    pick the right TTS voice/language. Cantonese text is detected via its
    distinctive characters and returned as `Cantonese`."""
    if not text:
        return ""
    # Conservative Cantonese-specific characters (avoid common Mandarin
    # particles like 呢/啦/嘛 which would misclassify zh text).
    cantonese_markers = set(
        "冇唔嘅係哋佢喺嚟啲嗰喎㗎冚瞓攞揾搵嘥咗乜嘢咩"
        "傾偈倾偈倾下傾下唔該而家依家啱啱咁睇嚟睇来同我哋"
    )
    if any(ch in cantonese_markers for ch in text):
        return "Cantonese"
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if han >= latin and han > 0:
        return "Chinese"
    if latin > 0:
        return "English"
    return ""

class ASRService:
    def __init__(self) -> None:
        self._model: Any | None = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._load_error: str | None = None

    def load(self) -> None:
        if os.environ.get("QWEN3_ASR_DISABLE_LOAD") == "1":
            return
        try:
            if BACKEND == "mlx":
                from mlx_audio.stt.utils import load as mlx_load

                self._model = mlx_load(MODEL_PATH)
                try:
                    import mlx.core as mx

                    print(f"[qwen3-asr] loaded {MODEL_PATH} device={mx.default_device()}", flush=True)
                except Exception:  # pragma: no cover
                    pass
                return
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
        override = os.environ.get("QWEN3_ASR_DEVICE", "").strip().lower()
        if override in {"cpu", "cuda", "mps"}:
            return override
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

    def start(self, language: str = "") -> str:
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = {
            "chunks": bytearray(),
            "text": "",
            "language": language,
            "partial": False,
            "created_at": time.time(),
            "vllm_state": None,
            "partial_text": "",
            "partial_lang": "",
            "last_partial_at": 0.0,
            "inf_lock": threading.Lock(),
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
            session["language"] = getattr(state, "language", "") or _fallback_language(session["text"])
            return {
                "text": session["text"],
                "language": session["language"],
                "partial": True,
            }
        if BACKEND == "mlx" and STREAM_PARTIAL:
            return self._partial_mlx(session)

        # transformers backend: batch transcribe at finish(); chunks just buffer.
        return {
            "text": session["text"],
            "language": session["language"],
            "partial": False,
        }

    def _partial_mlx(self, session: dict) -> dict[str, str | bool]:
        """滑窗 partial：说话期间每 PARTIAL_INTERVAL_MS 对累积 buffer 重推一次。

        - 忙时跳过：上一窗没跑完直接回上一窗结果，唔排队叠窗（GPU 串行，叠了也白排）。
        - buffer 裁最近 ~PARTIAL_MAX_SEC：长句重算成本线性涨；超长独白的 partial
          只反映最近窗口，/api/finish 整句高精度转写兜底唔受影响。
        - 语言提示与会话一致（cantonese 等），partial 与 final 唔会各说各话。
        """
        cached = {
            "text": session.get("partial_text", ""),
            "language": session.get("partial_lang", ""),
            "partial": True,
        }
        lock = session.setdefault("inf_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return cached
        try:
            now = time.monotonic()
            elapsed_ms = (now - float(session.get("last_partial_at") or 0.0)) * 1000
            pcm = bytes(session["chunks"])
            dur_sec = len(pcm) / 2 / SAMPLE_RATE
            if elapsed_ms < PARTIAL_INTERVAL_MS:
                return cached
            if dur_sec < 0.6:
                return cached  # 太短没有转写价值,等下一窗
            if dur_sec > PARTIAL_MAX_SEC:
                pcm = pcm[-int(PARTIAL_MAX_SEC * SAMPLE_RATE) * 2 :]
            wav, sr = _wav_from_pcm16(pcm)
            out = self._model.generate(
                _resample(wav, sr, SAMPLE_RATE),
                language=session.get("language") or None,
                max_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
            )
            text = getattr(out, "text", "") or ""
            langs = getattr(out, "language", None) or []
            language = str(langs[0] or "") if isinstance(langs, list) and langs else ""
            if not language:
                language = _fallback_language(text)
            session["partial_text"] = text
            session["partial_lang"] = language
            session["last_partial_at"] = now
            return {"text": text, "language": language, "partial": True}
        except Exception as exc:
            # partial 失败不影响主链路：回 cached；finish 整句高精度兜底。
            return cached
        finally:
            lock.release()

    def finish(self, session_id: str) -> dict[str, str | bool]:
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        self._ensure_loaded()

        if BACKEND == "vllm":
            state = session["vllm_state"]
            if state is not None:
                self._model.finish_streaming_transcribe(state)
                text = getattr(state, "text", "") or ""
                language = getattr(state, "language", "") or ""
                if not language:
                    language = _fallback_language(text)
                return {
                    "text": text,
                    "language": language,
                    "partial": False,
                }

        pcm = bytes(session["chunks"])
        wav, sr = _wav_from_pcm16(pcm)
        if wav.size == 0:
            return {"text": "", "language": "", "partial": False}
        hint = session.get("language") or None  # "cantonese" 等;空 = auto
        if BACKEND == "mlx":
            out = self._model.generate(
                _resample(wav, sr, SAMPLE_RATE),
                language=hint,
                max_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
            )
            text = getattr(out, "text", "") or ""
            langs = getattr(out, "language", None) or []
            language = ""
            if isinstance(langs, list) and langs:
                language = str(langs[0] or "")
            if not language:
                language = _fallback_language(text)
            return {
                "text": text,
                "language": language,
                "partial": False,
            }
        result = self._model.transcribe(
            audio=(_resample(wav, sr, SAMPLE_RATE), SAMPLE_RATE),
            language=hint,
        )
        if not result:
            return {"text": "", "language": "", "partial": False}
        first = result[0]
        text = getattr(first, "text", "") or ""
        language = getattr(first, "language", "") or ""
        if not language:
            language = _fallback_language(text)
        return {
            "text": text,
            "language": language,
            "partial": False,
        }

service = ASRService()

# 滑窗 partial(说话期间边说边出文字):mlx 后端每 PARTIAL_INTERVAL_MS 对累积
# buffer 重推一次。QWEN3_ASR_STREAM=0 一键回退纯离线模式(只缓冲,finish 才转写)。
STREAM_PARTIAL = os.environ.get("QWEN3_ASR_STREAM", "1") == "1"
PARTIAL_INTERVAL_MS = float(os.environ.get("QWEN3_ASR_PARTIAL_MS", "400"))
# 长句重算成本随 buffer 线性涨:partial 只裁最近 ~25s;finish 整句高精度兜底不受影响。
PARTIAL_MAX_SEC = float(os.environ.get("QWEN3_ASR_PARTIAL_MAX_SEC", "25"))

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
async def start(language: str = "") -> dict[str, str]:
    # language: 可选转写语言提示(如 "cantonese")。agent 在会话语言为粤语时传入,
    # 强制模型按 Cantonese 转写(auto 检测对粤语常误判成普通话);留空 = 交给模型 auto。
    return {"session_id": service.start(language=language.strip())}

@app.post("/api/chunk")
async def chunk(session_id: str, request: Request) -> dict[str, str | bool]:
    pcm = await request.body()
    # mlx partial 推理是秒级内的阻塞计算,丢线程池跑,唔阻塞事件循环(并发会话共用 loop)。
    return await run_in_threadpool(service.chunk, session_id, pcm)

@app.post("/api/finish")
async def finish(session_id: str, request: Request) -> dict[str, str | bool]:
    # 兼容两种调用:①逐块 chunk 攒到会话缓冲,finish 无 body;②agent 整包上传——
    # PCM body 直接在 finish 带过来,优先用 body(避免 2-6s 语音被拆成几十次小 HTTP)。
    body = await request.body()
    if body:
        session = service._sessions.get(session_id)
        if session is not None:
            session["chunks"].extend(body)
    return service.finish(session_id)
