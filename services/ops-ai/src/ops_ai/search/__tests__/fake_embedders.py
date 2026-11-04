"""Test-only embedders: controlled vectors, call recording and an outage, all behind the Embedder protocol."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ops_ai.search.embedder import LOCAL_DIMENSIONS, LOCAL_EMBEDDER_ID, Embedder, EmbedderUnavailableError

OUTAGE_MESSAGE = "embedder down"


def axis_vector(dimensions: int, index: int) -> list[float]:
    """The unit vector along one axis: cosine similarity 1.0 with itself, 0.0 with any other axis."""
    vector = [0.0] * dimensions
    vector[index] = 1.0
    return vector


def blend_vector(dimensions: int, similarity: float) -> list[float]:
    """A unit vector whose cosine similarity with axis_vector(dimensions, 0) is `similarity` (-1..1)."""
    vector = [0.0] * dimensions
    vector[0] = similarity
    vector[1] = math.sqrt(1.0 - similarity * similarity)
    return vector


@dataclass
class FixedVectorEmbedder:
    """Answers each text from a fixed table; records every embed() call."""

    vectors: Mapping[str, Sequence[float]]
    embedder_id: str = LOCAL_EMBEDDER_ID
    dimensions: int = LOCAL_DIMENSIONS
    calls: list[list[str]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self.vectors[text]) for text in texts]


@dataclass
class UnavailableEmbedder:
    """Every embed() raises EmbedderUnavailableError, as a provider outage would."""

    embedder_id: str = LOCAL_EMBEDDER_ID
    dimensions: int = LOCAL_DIMENSIONS
    calls: list[list[str]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        raise EmbedderUnavailableError(OUTAGE_MESSAGE)


class RecordingEmbedder:
    """Delegates to a real embedder and records the texts of every embed() call."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self.embedder_id = inner.embedder_id
        self.dimensions = inner.dimensions
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return self._inner.embed(texts)
