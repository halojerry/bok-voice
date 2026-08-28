from __future__ import annotations

import hashlib
import math
import re


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", text.lower())


class CharHashEmbedding:
    """Deterministic, dependency-free embedder for tests/CI.

    Builds a binary bag-of-token vector (CJK chars + words) so that texts sharing
    tokens have cosine > 0. Swap for a real local embedder (BGE/ONNX) in production;
    the interface is the same.
    """

    def __init__(self, dim: int = 384):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokens(text):
            idx = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim
            vec[idx] = 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
