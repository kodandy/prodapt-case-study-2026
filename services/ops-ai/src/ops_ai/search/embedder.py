import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

LOCAL_EMBEDDER_ID = "local-hash-v1"
LOCAL_DIMENSIONS = 512

_TOKEN = re.compile(r"[a-z0-9]+")
_SIGN_BIT = 1 << 63


class EmbedderUnavailableError(Exception):
    """The embedder could not produce vectors (provider outage, bad reply). Message never holds secrets."""


class Embedder(Protocol):
    """One vector column per `embedder_id` in search_documents; `dimensions` must match that column."""

    @property
    def embedder_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def document_text(title: str, body: str) -> str:
    return f"{title} {body}"


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (norm_a * norm_b)


def is_zero(vector: Sequence[float]) -> bool:
    return all(x == 0.0 for x in vector)


class LocalHashEmbedder:
    """Deterministic feature-hashing embedder: words + boundary-marked character trigrams, signed blake2b
    buckets, L2-normalised. Captures spelling/sub-token similarity (typos, BIN prefixes), not meaning.
    """

    @property
    def embedder_id(self) -> str:
        return LOCAL_EMBEDDER_ID

    @property
    def dimensions(self) -> int:
        return LOCAL_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * LOCAL_DIMENSIONS
        for token in _TOKEN.findall(text.lower()):
            marked = f"#{token}#"
            features = [f"w:{token}", *(f"g:{marked[i : i + 3]}" for i in range(len(marked) - 2))]
            for feature in features:
                h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
                vector[h % LOCAL_DIMENSIONS] += -1.0 if h & _SIGN_BIT else 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0.0:
            return vector
        return [x / norm for x in vector]
