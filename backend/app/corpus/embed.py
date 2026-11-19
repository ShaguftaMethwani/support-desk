"""Text embeddings.

FastEmbedder runs BAAI/bge-small-en-v1.5 locally through ONNX (no PyTorch), about 200 MB of
memory. HashEmbedder is a deterministic stand-in for tests: no model download, no network.
"""

import hashlib
import math
import re
from functools import cached_property
from pathlib import Path
from typing import Any, Protocol

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
# bge models expect this instruction on queries (not on documents).
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "fastembed"
# Small batches keep memory flat: a batch pads to its longest text, and attention memory grows
# with batch x length^2. Embedding 320 of the longest reviews peaked 444 MB above the loaded
# model at batch size 32 (over Render's 512 MB on its own), and 52 MB at 4, which was no slower.
BATCH_SIZE = 4


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedder:
    dim = EMBED_DIM

    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        self._cache_dir = cache_dir

    @cached_property
    def _model(self) -> Any:
        from fastembed import TextEmbedding

        return TextEmbedding(EMBED_MODEL, cache_dir=str(self._cache_dir))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts, batch_size=BATCH_SIZE)]

    def embed_query(self, text: str) -> list[float]:
        vector = next(iter(self._model.embed([QUERY_INSTRUCTION + text])))
        result: list[float] = vector.tolist()
        return result


_WORD = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    """Bag-of-words hashed into a fixed vector. Similar words, similar vectors. Tests only."""

    dim = EMBED_DIM

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in _WORD.findall(text.lower()):
            digest = hashlib.md5(word.encode()).digest()
            vec[int.from_bytes(digest[:4], "little") % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)
