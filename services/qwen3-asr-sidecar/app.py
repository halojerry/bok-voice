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

def _trim_trailing_silence(pcm: bytes) -> bytes:
    """finish 前裁掉尾部静音帧(简单能量门限)。

    VAD min_silence 0.45s + 端点推理意味着 finish buffer 天然带 ~0.5s 静音尾巴,
    整句/增量解码都在为它烧 GPU。20ms 帧逐帧 RMS,从尾往前吞掉低于门限的帧;
    最多裁 TRIM_MAX_SEC(防整段皆静音被裁光),裁穿即住手交原 buffer。
    """
    if not pcm or TRIM_MAX_SEC <= 0:
        return pcm
    frame = SAMPLE_RATE // 50  # 20ms
    total_frames = (len(pcm) // 2) // frame
    if total_frames == 0:
        return pcm
    x = np.frombuffer(pcm[: total_frames * frame * 2], dtype=np.int16).astype(np.float32) / 32768.0
    rms = np.sqrt((x.reshape(total_frames, frame) ** 2).mean(axis=1))
    max_silent = int(TRIM_MAX_SEC * 50)
    k = 0
    for i in range(total_frames - 1, -1, -1):
        if rms[i] >= TRIM_RMS or k >= max_silent:
            break
        k += 1
    if k == 0:
        return pcm  # 尾部无静音:原样返回(唔顺手裁掉非帧对齐余数)
    keep = (total_frames - k) * frame * 2
    if keep <= 0 or keep >= len(pcm):
        return pcm  # 整段皆静音:不动,交模型与旧路径判空
    return pcm[:keep]

def _has_latin_or_digit(text: str) -> bool:
    """尾巴文本含任何数字/拉丁字符(连续串即 WhatsApp 捕获高危)。

    尾段解码缺左上下文,号码/英文被缝腰斩或听错是最高危场景——凡出现一律
    回退整句高精度转写,保「停嘴整句兜底」铁律。
    """
    return any(ch.isascii() and ch.isalnum() for ch in text)

def _seam_risky(partial_text: str) -> bool:
    """接缝安全检查:partial 尾字符是 latin/数字 → 词/号码可能被缝截断,不可验证。

    CJK 字素自成音节,缝前后按序拼接基本无损(粤语/普通话转写以字为单位);
    latin 词跨缝被劈开则无法从文本侧验证 → 宁可回退整段。
    """
    if not partial_text:
        return True
    ch = partial_text[-1]
    return ch.isascii() and ch.isalnum()

def _join_stitched(left: str, right: str) -> str:
    """拼接 partial 与尾段:CJK 边界直接相连,含空格/标点边界保持自然间隔。"""
    if not left:
        return right
    if not right:
        return left
    if left[-1].isascii() and left[-1].isalnum():
        return f"{left} {right}"
    return f"{left}{right}"


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
            "partial_covered": None,
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
            # 解码快照:喺持锁期间、generate 之前拍照。incremental finish 的
            # partial_covered 必须以呢份快照长度为准(睇下面赋值处注释)。
            pcm = bytes(session["chunks"])
            dur_sec = len(pcm) / 2 / SAMPLE_RATE
            if elapsed_ms < PARTIAL_INTERVAL_MS:
                return cached
            if dur_sec < 0.6:
                return cached  # 太短没有转写价值,等下一窗
            capped = dur_sec > PARTIAL_MAX_SEC
            if capped:
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
            # covered=解码快照长度(上面 bytes(chunks) 拍照),【唔系】解码完成后的
            # len(chunks):/api/finish 的 PCM body 会在锁外追加进 chunks(端点收 body
            # 唔使锁),若记当刻长度会「多认覆盖」→ finish 见 covered==len(pcm) →
            # 尾巴被当已解码直接转正 partial → 尾段语音/号码静默丢失。
            # 裁剪窗(>PARTIAL_MAX_SEC)只解了尾部 25s,头段从未进 partial →
            # covered=None,增量路径让位整句兜底(唔得被本次赋值翻案)。
            session["partial_covered"] = None if capped else len(pcm)
            session["last_partial_at"] = now
            return {"text": text, "language": language, "partial": True}
        except Exception as exc:
            # partial 失败不影响主链路：回 cached；finish 整句高精度兜底。
            return cached
        finally:
            lock.release()

    def _try_incremental_finish(
        self, session: dict, pcm: bytes, hint: str | None
    ) -> dict[str, str | bool] | None:
        """增量 finish:新鲜 partial 已覆盖 buffer 主体时,只解码尾部小段再拼接。

        partial 滑窗 ≤400ms 前刚解码过几乎同一份 buffer,finish 再整段重推是
        0.5-1.2s 的纯重复 GPU 时间。这里在 inf_lock 纪律下只解码
        partial_covered 之后的尾部(通常 ≤2-3s),FINAL = partial_text + tail_text。
        任一前提不成立 → 返回 None → 调用方整句高精度兜底:
        - 未开 QWEN3_ASR_INC_FINISH / 无 fresh partial / 窗口被 PARTIAL_MAX_SEC 裁过
        - partial 不新鲜(> FINISH_PARTIAL_FRESH_SEC)或尾段超 FINISH_TAIL_MAX_SEC
        - 锁忙超过 FINISH_LOCK_WAIT_SEC(说明还有 partial 在飞,接缝未定)
        - 接缝可疑(partial 尾字符是 latin/数字,词/号码可能被缝截断)
        - 尾段文本含数字/拉丁串(WhatsApp 捕获零降级,绝不赌号码转写)
        """
        if not INC_FINISH:
            return None
        lock = session.get("inf_lock")
        if lock is None:
            return None
        # 小等一把:让在飞的 partial 跑完(它的结果才覆盖接缝前的音频)。
        if not lock.acquire(timeout=FINISH_LOCK_WAIT_SEC):
            return None
        try:
            partial_text = str(session.get("partial_text") or "")
            covered = session.get("partial_covered")
            last_at = float(session.get("last_partial_at") or 0.0)
            if not partial_text or covered is None:
                return None
            if (time.monotonic() - last_at) > FINISH_PARTIAL_FRESH_SEC:
                return None
            if not (0 < covered <= len(pcm)):
                return None
            tail = pcm[covered:]
            tail_sec = len(tail) / 2 / SAMPLE_RATE
            if tail_sec > FINISH_TAIL_MAX_SEC:
                return None
            if _seam_risky(partial_text):
                return None
            if not tail:
                # 尾巴为零:最后一窗已解码完整 buffer(≤PARTIAL_MAX_SEC 才有 covered),
                # partial 即整句结果,直接转正,一次 GPU 都不用再烧。
                return {
                    "text": partial_text,
                    "language": str(session.get("partial_lang") or "") or _fallback_language(partial_text),
                    "partial": False,
                }
            wav, sr = _wav_from_pcm16(tail)
            out = self._model.generate(
                _resample(wav, sr, SAMPLE_RATE),
                language=hint,
                max_tokens=int(os.environ.get("QWEN3_ASR_MAX_TOKENS", "256")),
            )
            tail_text = getattr(out, "text", "") or ""
            if not tail_text.strip():
                # 有音频却转不出字:接缝可能劈在音节中间,不可信 → 整句兜底。
                return None
            if _has_latin_or_digit(tail_text):
                return None
            langs = getattr(out, "language", None) or []
            language = str(langs[0] or "") if isinstance(langs, list) and langs else ""
            if not language:
                language = str(session.get("partial_lang") or "")
            stitched = _join_stitched(partial_text, tail_text)
            if not language:
                language = _fallback_language(stitched)
            print(
                f"[qwen3-asr] incremental finish: partial={len(partial_text)}ch "
                f"tail={tail_sec:.2f}s (skipped {covered / 2 / SAMPLE_RATE:.1f}s re-decode)",
                flush=True,
            )
            return {
                "text": stitched,
                "language": language,
                "partial": False,
            }
        except Exception:
            # 增量任何一步失手都退回整句兜底,绝不因提速牺牲正确性。
            return None
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
        if len(pcm) < 2:
            return {"text": "", "language": "", "partial": False}
        # EOT 卫生:先裁掉尾部静音再解码(VAD min_silence 0.45s + 推理尾巴不该烧 GPU),
        # 整句与增量两条路径都受益。
        pcm = _trim_trailing_silence(pcm)
        wav, sr = _wav_from_pcm16(pcm)
        hint = session.get("language") or None  # "cantonese" 等;空 = auto
        if BACKEND == "mlx":
            # 增量 fast path:新鲜 partial 已覆盖 buffer 主体 → 只解码尾巴再拼接;
            # 任一前提不成立则回退整句高精度兜底(WhatsApp 捕获零降级)。
            incremental = self._try_incremental_finish(session, pcm, hint)
            if incremental is not None:
                return incremental
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
# 增量 finish:partial 已覆盖 buffer 主体时只解码尾部小段再拼接(RCA-1 主刀)。
# QWEN3_ASR_INC_FINISH=0 一键回退整句重解码;安全阀全部 env 可调。
INC_FINISH = os.environ.get("QWEN3_ASR_INC_FINISH", "1") == "1"
FINISH_PARTIAL_FRESH_SEC = float(os.environ.get("QWEN3_ASR_PARTIAL_FRESH_SEC", "2.5"))
FINISH_TAIL_MAX_SEC = float(os.environ.get("QWEN3_ASR_FINISH_TAIL_MAX_SEC", "3.0"))
FINISH_LOCK_WAIT_SEC = float(os.environ.get("QWEN3_ASR_FINISH_LOCK_WAIT", "0.3"))
# finish 尾部静音裁剪:20ms 帧 RMS 门限(≈-54dBFS),最多裁 TRIM_MAX_SEC。
TRIM_RMS = float(os.environ.get("QWEN3_ASR_TRIM_RMS", "0.002"))
TRIM_MAX_SEC = float(os.environ.get("QWEN3_ASR_TRIM_MAX_SEC", "2.0"))

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
    # 增量路径要在 inf_lock 上小等在飞 partial(≤FINISH_LOCK_WAIT_SEC),解码本身也是
    # 秒级阻塞计算——与 /api/chunk 同款丢线程池,唔阻塞事件循环(并发会话共用 loop)。
    return await run_in_threadpool(service.finish, session_id)
