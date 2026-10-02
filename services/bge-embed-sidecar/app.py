"""Bok W1b embedding sidecar — local BAAI bge-m3 (MLX) text embeddings.

OpenAI-compatible POST /v1/embeddings + GET /health. Binds 127.0.0.1 only
(host is decided by the uvicorn command line; bok.py wiring passes
--host 127.0.0.1): customer utterance vectors never leave the machine.

Pooling contract (must match BAAI/bge-m3 reference): dense = CLS token
pooling + L2 normalization. Two findings from the 2026-09-23 smoke that this
shell enforces regardless of package drift:

- mlx-embedding-models (taylorai, 0.0.11) converts weights through
  torch BertModel.from_pretrained — structurally cannot consume the
  MLX-quantized safetensors (U32 packed + scales/biases), so candidate ① is
  out; mlx-embeddings (Blaizzy, 0.1.0) loads them natively.
- the installed mlx-embeddings 0.1.0 wheel HARDCODES mean_pooling in
  xlm_roberta.Model.__call__ (the pool_by_config branch only exists on
  github main), so text_embeds is mean-pooled — wrong for bge-m3. We ignore
  text_embeds and CLS-pool out.last_hidden_state[:, 0, :] ourselves, then
  L2-normalize. The pooling_config load override is kept belt-and-suspenders
  for future wheels that honor it.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import mlx.core as mx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent

DEFAULT_MODEL = os.environ.get("BGE_EMBED_MODEL", "mlx-community/bge-m3-mlx-4bit")
# Packaged-mode档与 TTS 同款：跳过暖机只损失首个请求 1-2s，换取启动不被阻塞。
WARMUP_ENABLED = os.environ.get("BGE_EMBED_WARMUP", "1") == "1"
# Input defense: truncate over-long items (customer utterances are short),
# reject oversized batches outright.
MAX_INPUT_CHARS = 512
MAX_BATCH = 256
# bge-m3 supports 8192 tokens; 512 chars of mixed zh/en tokenizes well under
# this cap, so latency stays bounded.
MAX_TOKENS = int(os.environ.get("BGE_EMBED_MAX_TOKENS", "256"))

app = FastAPI(title="Bok BGE Embed Sidecar")


class EmbedRequest(BaseModel):
    input: str | list[str]


def resolve_model_path(model_id: str) -> str:
    """Local dir wins; then mirror bok.py model_path lookup order (lmstudio
    owner/name layout, app-data owner--name layout); else return the repo id
    and let huggingface_hub snapshot_download it."""
    direct = Path(model_id)
    if direct.is_dir():
        return str(direct)
    if "/" in model_id:
        owner, name = model_id.split("/", 1)
        home = Path.home()
        lm = home / ".lmstudio" / "models" / owner / name
        if (lm / "config.json").is_file():
            return str(lm)
        app_data = home / "Library" / "Application Support" / "BokVoice" / "models" / f"{owner}--{name}"
        if (app_data / "config.json").is_file():
            return str(app_data)
    return model_id


class EmbedService:
    def __init__(self) -> None:
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._lock = threading.Lock()
        self._load_error: str | None = None
        self._ready = False
        self._dim = 0
        self.model_label: str = DEFAULT_MODEL

    def load(self) -> None:
        try:
            from mlx_embeddings.utils import load

            path = resolve_model_path(DEFAULT_MODEL)
            # pooling_config override is a no-op on the 0.1.0 wheel (it
            # hardcodes mean pooling; we CLS-pool ourselves in _forward) but
            # makes the intent explicit and future-proof for fixed wheels.
            model, tokenizer = load(
                path,
                model_config={"pooling_config": {"pooling_mode": "cls"}},
            )
            self._model = model
            self._tokenizer = tokenizer
            self.model_label = str(path)
            self._dim = int(getattr(model.config, "hidden_size", 0))
        except Exception as exc:  # pragma: no cover - model download/load can fail
            self._load_error = repr(exc)

    def ensure_loaded(self) -> None:
        if self._load_error:
            raise HTTPException(status_code=503, detail=f"model not ready: {self._load_error}")
        if self._model is None or self._tokenizer is None:
            raise HTTPException(status_code=503, detail="model not loaded")

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def dim(self) -> int:
        return self._dim

    def _forward(self, texts: list[str]) -> list[list[float]]:
        self.ensure_loaded()
        assert self._model is not None and self._tokenizer is not None
        batch = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=MAX_TOKENS,
            return_attention_mask=True,
        )
        out = self._model(
            input_ids=mx.array(batch["input_ids"]),
            attention_mask=mx.array(batch["attention_mask"]),
        )
        # bge-m3 dense pooling = CLS token + L2 normalize. The tokenizer pads
        # right, so index 0 is the <s>/CLS token for every row. Package
        # 0.1.0's text_embeds is mean-pooled (see module docstring) — ignored.
        vecs = out.last_hidden_state[:, 0, :]
        vecs = vecs / mx.maximum(mx.linalg.norm(vecs, axis=-1, keepdims=True), 1e-12)
        mx.eval(vecs)
        self._dim = int(vecs.shape[-1]) or self._dim
        return vecs.astype(mx.float32).tolist()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return self._forward(texts)

    def warmup(self) -> None:
        if not WARMUP_ENABLED:
            # Packaged档: model itself already loaded above; dim is known from
            # config, first real request pays the one-off compile.
            if self._model is not None and self._dim:
                self._ready = True
            return
        try:
            self.embed(["warmup"])
            self._ready = True
            print("BGE_EMBED_WARMUP ready", flush=True)
        except Exception as exc:  # pragma: no cover - warmup must not crash boot
            self._load_error = repr(exc)
            print("BGE_EMBED_WARMUP_ERR", repr(exc), flush=True)


service = EmbedService()


@app.on_event("startup")
def _startup() -> None:
    service.load()
    service.warmup()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model": service.model_label,
        "dim": service.dim,
        "ready": service.ready,
        "load_error": service._load_error,
    }


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest) -> dict:
    texts = [req.input] if isinstance(req.input, str) else req.input
    if not texts or not all(isinstance(t, str) for t in texts):
        raise HTTPException(status_code=400, detail="input must be a string or a list of strings")
    if len(texts) > MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"batch too large: {len(texts)} > {MAX_BATCH}")
    clipped = [t[:MAX_INPUT_CHARS] for t in texts]
    vecs = service.embed(clipped)
    return {
        "object": "list",
        "model": service.model_label,
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in enumerate(vecs)
        ],
    }
