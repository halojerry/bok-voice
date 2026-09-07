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


class HybridLexicalEmbedding:
    """字 bigram + 词元哈希的轻量词法嵌入（KB 分析检索默认档,零模型依赖）。

    对 CharHash 的升级:加入相邻字 bigram（「退换」「运费」等词级信号）与
    latin 词元,向量维度 512、L2 归一。语义能力弱于神经 embedder,但比单字
    袋强一档;分析侧检索（跨通话找相似问题）够用。神经网络 embedder 可用后
    由 MlxEmbedding 按环境选入（接口同 EmbeddingService）。"""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _tokens(self, text: str) -> list[str]:
        import re as _re

        low = (text or "").lower()
        out: list[str] = []
        for word in _re.findall(r"[a-zA-Z0-9]+", low):
            out.append("w:" + word)
        cjk = _re.findall(r"[\u4e00-\u9fff]", low)
        out.extend(cjk)
        out.extend(a + b for a, b in zip(cjk, cjk[1:]))
        return out

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs: list[list[float]] = []
        for text in texts:
            v = [0.0] * self.dim
            for tok in self._tokens(text):
                h = int(hashlib.md5(tok.encode()).hexdigest()[:8], 16)
                v[h % self.dim] += 1.0
            norm = sum(x * x for x in v) ** 0.5
            vecs.append([x / norm for x in v] if norm else v)
        return vecs


class MlxEmbedding:
    """mlx 本地神经 embedding（env KB_EMBEDDING_MODEL 选入,模型懒加载）。

    HF 可达时经 mlx_embedding_models 下载 mlx-community/*embedding* 模型;
    当前 HF 不可达/模型未就位时构造失败,由选点逻辑回退 HybridLexical。"""

    def __init__(self, model: str, dim: int = 1024) -> None:
        self.model = model
        self.dim = dim
        self._inner = None

    def _load(self):
        if self._inner is None:
            from mlx_embedding_models.embedding import EmbeddingModel

            self._inner = EmbeddingModel.from_pretrained(self.model)
        return self._inner

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vecs = model.embed(list(texts))
        try:
            import mlx.core as mx

            return [list(map(float, v)) for v in vecs]
        except Exception:  # pragma: no cover
            return [list(map(float, v)) for v in vecs]
