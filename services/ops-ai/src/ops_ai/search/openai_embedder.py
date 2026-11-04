import json
import math
from collections.abc import Sequence

import httpx
from pydantic import SecretStr

from ops_ai.assistant.openai_llm import API_KEY_ERROR, OPENAI_BASE_URL, validate_base_url
from ops_ai.search.embedder import EmbedderUnavailableError

OPENAI_EMBEDDER_ID = "openai-text-embedding-3-small"
OPENAI_DIMENSIONS = 1536
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_TIMEOUT_SECONDS = 10.0
MAX_EMBEDDING_RESPONSE_BYTES = 64 * 1024 * 1024


class OpenAiEmbedder:
    """OpenAI /embeddings (text-embedding-3-small, 1536 dims). Every failure → EmbedderUnavailableError with a
    short reason, raised `from None` so no traceback holds the request headers.
    """

    def __init__(
        self,
        api_key: SecretStr,
        base_url: str = OPENAI_BASE_URL,
        timeout_seconds: float = EMBEDDING_TIMEOUT_SECONDS,
    ) -> None:
        raw = api_key.get_secret_value()
        if not raw or not raw.isascii() or not raw.isprintable() or raw != raw.strip():
            raise ValueError(API_KEY_ERROR)
        self._api_key = api_key
        self._url = f"{validate_base_url(base_url).rstrip('/')}/embeddings"
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"OpenAiEmbedder(model={OPENAI_EMBEDDING_MODEL!r})"

    @property
    def embedder_id(self) -> str:
        return OPENAI_EMBEDDER_ID

    @property
    def dimensions(self) -> int:
        return OPENAI_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        content = self._post(list(texts))
        vectors = _parse(content, len(texts))
        if vectors is None:
            raise EmbedderUnavailableError("openai embeddings: invalid body")
        return vectors

    def _post(self, texts: list[str]) -> bytes:
        payload = {"model": OPENAI_EMBEDDING_MODEL, "input": texts, "dimensions": OPENAI_DIMENSIONS}
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        reason: str
        try:
            with httpx.Client(timeout=httpx.Timeout(self._timeout_seconds), trust_env=False) as client:
                response = client.post(self._url, json=payload, headers=headers)
                if response.status_code == 200 and len(response.content) <= MAX_EMBEDDING_RESPONSE_BYTES:
                    return response.content
                reason = f"http {response.status_code}" if response.status_code != 200 else "too large"
        except httpx.TimeoutException:
            reason = "timeout"
        except httpx.HTTPError:
            reason = "connection error"
        except Exception:
            reason = "client error"
        raise EmbedderUnavailableError(f"openai embeddings: {reason}") from None


def _parse(content: bytes, expected: int) -> list[list[float]] | None:
    try:
        body: object = json.loads(content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, list) or len(data) != expected:
        return None
    by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            return None
        index = item.get("index")
        vector = _vector(item.get("embedding"))
        if not isinstance(index, int) or isinstance(index, bool) or vector is None or index in by_index:
            return None
        by_index[index] = vector
    if sorted(by_index) != list(range(expected)):
        return None
    return [by_index[i] for i in range(expected)]


def _vector(raw: object) -> list[float] | None:
    if not isinstance(raw, list) or len(raw) != OPENAI_DIMENSIONS:
        return None
    values: list[float] = []
    for x in raw:
        if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x):
            return None
        values.append(float(x))
    return values
