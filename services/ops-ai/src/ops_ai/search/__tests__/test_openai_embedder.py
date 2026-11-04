import time
from collections.abc import Callable, Iterator, Sequence

import pytest
from ops_ai.assistant.__tests__.fake_openai import (
    FakeOpenAiServer,
    ScriptedReply,
    closed_port_base_url,
    fake_server_session,
)
from ops_ai.assistant.openai_llm import API_KEY_ERROR, BASE_URL_ERROR
from ops_ai.search.embedder import Embedder, EmbedderUnavailableError
from ops_ai.search.openai_embedder import (
    OPENAI_DIMENSIONS,
    OPENAI_EMBEDDER_ID,
    OPENAI_EMBEDDING_MODEL,
    OpenAiEmbedder,
)
from pydantic import JsonValue, SecretStr

CANARY_KEY = "sk-canary-openai-embedder-4c2a"
TIMEOUT_SECONDS = 0.3
SLOW_REPLY_SECONDS = 2.0
ELAPSED_LIMIT_SECONDS = 1.5

ServeFake = Callable[[Sequence[ScriptedReply]], FakeOpenAiServer]


@pytest.fixture
def serve() -> Iterator[ServeFake]:
    yield from fake_server_session()


def _unit(index: int, dimensions: int = OPENAI_DIMENSIONS) -> list[float]:
    vector = [0.0] * dimensions
    vector[index] = 1.0
    return vector


def _json_vector(vector: Sequence[float]) -> list[JsonValue]:
    return [*vector]


def _embeddings_reply(
    vectors: Sequence[Sequence[float]], indexes: Sequence[int] | None = None
) -> ScriptedReply:
    """One data item per vector; `indexes` overrides the `index` field of each item (default 0..n-1)."""
    chosen = indexes if indexes is not None else list(range(len(vectors)))
    data: list[JsonValue] = []
    for index, vector in zip(chosen, vectors, strict=True):
        item: dict[str, JsonValue] = {
            "object": "embedding",
            "index": index,
            "embedding": _json_vector(vector),
        }
        data.append(item)
    return ScriptedReply(
        json_body={
            "object": "list",
            "data": data,
            "model": OPENAI_EMBEDDING_MODEL,
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }
    )


def _single_embedding_reply(embedding: list[JsonValue]) -> ScriptedReply:
    item: dict[str, JsonValue] = {"index": 0, "embedding": embedding}
    return ScriptedReply(json_body={"data": [item]})


def _embedder(base_url: str, timeout_seconds: float = 5.0, key: str = CANARY_KEY) -> OpenAiEmbedder:
    return OpenAiEmbedder(api_key=SecretStr(key), base_url=base_url, timeout_seconds=timeout_seconds)


def _embed_expecting_failure(
    embedder: OpenAiEmbedder, texts: Sequence[str] = ("card declined",)
) -> EmbedderUnavailableError:
    with pytest.raises(EmbedderUnavailableError) as exc:
        embedder.embed(texts)
    return exc.value


def test_module_constants_match_the_contract() -> None:
    # Arrange: constants imported from the module under test

    # Act
    constants = (OPENAI_EMBEDDER_ID, OPENAI_DIMENSIONS, OPENAI_EMBEDDING_MODEL)

    # Assert
    assert constants == ("openai-text-embedding-3-small", 1536, "text-embedding-3-small")


def test_the_embedder_exposes_its_id_and_dimensions_and_is_assignable_to_the_port() -> None:
    # Arrange
    embedder: Embedder = _embedder(closed_port_base_url())

    # Act
    attributes = (embedder.embedder_id, embedder.dimensions)

    # Assert
    assert attributes == ("openai-text-embedding-3-small", 1536)


def test_embed_posts_one_request_to_embeddings_with_the_documented_body_and_bearer_key(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_embeddings_reply([_unit(0), _unit(1)])])
    embedder = _embedder(server.base_url)

    # Act
    embedder.embed(["card declined", "insufficient funds"])

    # Assert
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.path == "/v1/embeddings"
    assert request.headers["authorization"] == f"Bearer {CANARY_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert request.json_object() == {
        "model": "text-embedding-3-small",
        "input": ["card declined", "insufficient funds"],
        "dimensions": 1536,
    }


def test_embed_never_puts_the_key_in_the_request_body(serve: ServeFake) -> None:
    # Arrange
    server = serve([_embeddings_reply([_unit(0)])])
    embedder = _embedder(server.base_url)

    # Act
    embedder.embed(["card declined"])

    # Assert
    assert CANARY_KEY not in server.requests[0].raw_body.decode()


def test_embed_returns_the_vectors_in_input_order(serve: ServeFake) -> None:
    # Arrange
    first = _unit(0)
    second = _unit(1)
    third = _unit(2)
    server = serve([_embeddings_reply([first, second, third])])
    embedder = _embedder(server.base_url)

    # Act
    vectors = embedder.embed(["a", "b", "c"])

    # Assert
    assert vectors == [first, second, third]


def test_embed_orders_the_result_by_the_index_field_not_by_the_position_in_data(
    serve: ServeFake,
) -> None:
    # Arrange
    for_index_0 = _unit(10)
    for_index_1 = _unit(11)
    for_index_2 = _unit(12)
    server = serve([_embeddings_reply([for_index_2, for_index_0, for_index_1], indexes=[2, 0, 1])])
    embedder = _embedder(server.base_url)

    # Act
    vectors = embedder.embed(["a", "b", "c"])

    # Assert
    assert vectors == [for_index_0, for_index_1, for_index_2]


def test_embed_accepts_integer_valued_numbers_in_a_vector(serve: ServeFake) -> None:
    # Arrange
    integers: list[JsonValue] = [1, *(0 for _ in range(OPENAI_DIMENSIONS - 1))]
    server = serve([_single_embedding_reply(integers)])
    embedder = _embedder(server.base_url)

    # Act
    vectors = embedder.embed(["card declined"])

    # Assert
    assert vectors == [_unit(0)]


def test_embed_of_no_texts_returns_an_empty_list_and_sends_no_request(serve: ServeFake) -> None:
    # Arrange
    server = serve([_embeddings_reply([_unit(0)])])
    embedder = _embedder(server.base_url)

    # Act
    vectors = embedder.embed([])

    # Assert
    assert vectors == []
    assert server.requests == []


def test_embed_of_no_texts_does_not_need_the_provider_to_be_reachable() -> None:
    # Arrange
    embedder = _embedder(closed_port_base_url())

    # Act
    vectors = embedder.embed([])

    # Assert
    assert vectors == []


_TOO_SHORT: list[JsonValue] = [0.1, 0.2, 0.3]
_NON_NUMERIC: list[JsonValue] = ["x" for _ in range(OPENAI_DIMENSIONS)]
_NULLS: list[JsonValue] = [None for _ in range(OPENAI_DIMENSIONS)]
FAILING_REPLIES = [
    pytest.param(ScriptedReply(status=500, json_body={"error": {"message": "boom"}}), id="http-500"),
    pytest.param(ScriptedReply(status=429, json_body={"error": {"message": "rate limit"}}), id="http-429"),
    pytest.param(
        ScriptedReply(status=401, json_body={"error": {"message": f"bad key {CANARY_KEY}"}}),
        id="http-401-echoing-the-key",
    ),
    pytest.param(ScriptedReply(status=404, json_body={}), id="http-404"),
    pytest.param(ScriptedReply(raw_body=b"<html>not json</html>"), id="body-is-not-json"),
    pytest.param(ScriptedReply(raw_body=b""), id="empty-body"),
    pytest.param(ScriptedReply(json_body=[1, 2, 3]), id="json-array-instead-of-object"),
    pytest.param(ScriptedReply(json_body={"object": "list"}), id="data-field-missing"),
    pytest.param(ScriptedReply(json_body={"data": "nope"}), id="data-is-not-a-list"),
    pytest.param(_single_embedding_reply(_TOO_SHORT), id="wrong-dimension"),
    pytest.param(_single_embedding_reply(_NON_NUMERIC), id="non-numeric-values"),
    pytest.param(_single_embedding_reply(_NULLS), id="null-values"),
    pytest.param(ScriptedReply(json_body={"data": [{"index": 0}]}), id="embedding-field-missing"),
    pytest.param(ScriptedReply(json_body={"data": []}), id="no-data-items"),
    pytest.param(ScriptedReply(drop_connection=True), id="connection-dropped"),
]


@pytest.mark.parametrize("reply", FAILING_REPLIES)
def test_embed_maps_a_bad_provider_answer_to_embedder_unavailable_without_the_key(
    serve: ServeFake, reply: ScriptedReply
) -> None:
    # Arrange
    server = serve([reply])
    embedder = _embedder(server.base_url)

    # Act
    error = _embed_expecting_failure(embedder)

    # Assert
    assert CANARY_KEY not in str(error)
    assert CANARY_KEY not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__ is True
    assert len(server.requests) == 1


def test_embed_maps_a_wrong_vector_count_to_embedder_unavailable(serve: ServeFake) -> None:
    # Arrange
    server = serve([_embeddings_reply([_unit(0)])])
    embedder = _embedder(server.base_url)

    # Act
    error = _embed_expecting_failure(embedder, ["a", "b"])

    # Assert
    assert CANARY_KEY not in str(error)
    assert type(error) is EmbedderUnavailableError


def test_embed_maps_more_vectors_than_texts_to_embedder_unavailable(serve: ServeFake) -> None:
    # Arrange
    server = serve([_embeddings_reply([_unit(0), _unit(1), _unit(2)])])
    embedder = _embedder(server.base_url)

    # Act
    error = _embed_expecting_failure(embedder, ["a", "b"])

    # Assert
    assert CANARY_KEY not in str(error)
    assert type(error) is EmbedderUnavailableError


def test_embed_maps_one_wrong_dimension_among_several_vectors_to_embedder_unavailable(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_embeddings_reply([_unit(0), _unit(0, dimensions=512)])])
    embedder = _embedder(server.base_url)

    # Act
    error = _embed_expecting_failure(embedder, ["a", "b"])

    # Assert
    assert CANARY_KEY not in str(error)
    assert type(error) is EmbedderUnavailableError


def test_embed_maps_a_refused_connection_to_embedder_unavailable_without_the_key() -> None:
    # Arrange
    embedder = _embedder(closed_port_base_url())

    # Act
    error = _embed_expecting_failure(embedder)

    # Assert
    assert CANARY_KEY not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__ is True


def test_embed_maps_a_timeout_to_embedder_unavailable_within_the_timeout(serve: ServeFake) -> None:
    # Arrange
    server = serve([ScriptedReply(delay_seconds=SLOW_REPLY_SECONDS, json_body={"data": []})])
    embedder = _embedder(server.base_url, timeout_seconds=TIMEOUT_SECONDS)
    started = time.monotonic()

    # Act
    error = _embed_expecting_failure(embedder)

    # Assert
    assert time.monotonic() - started < ELAPSED_LIMIT_SECONDS
    assert CANARY_KEY not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__ is True


def test_embed_makes_a_single_attempt_and_does_not_retry_a_failure(serve: ServeFake) -> None:
    # Arrange
    server = serve([ScriptedReply(status=500, json_body={}), _embeddings_reply([_unit(0)])])
    embedder = _embedder(server.base_url)

    # Act
    _embed_expecting_failure(embedder)

    # Assert
    assert len(server.requests) == 1


def test_embed_works_again_after_a_failed_call(serve: ServeFake) -> None:
    # Arrange
    server = serve([ScriptedReply(status=500, json_body={}), _embeddings_reply([_unit(3)])])
    embedder = _embedder(server.base_url)
    _embed_expecting_failure(embedder)

    # Act
    vectors = embedder.embed(["card declined"])

    # Assert
    assert vectors == [_unit(3)]


def test_repr_never_contains_the_api_key() -> None:
    # Arrange
    embedder = _embedder(closed_port_base_url())

    # Act
    rendered = repr(embedder)

    # Assert
    assert CANARY_KEY not in rendered


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("", id="empty"),
        pytest.param(" sk-x", id="leading-space"),
        pytest.param("sk-x\n", id="trailing-newline"),
        pytest.param("sk-é", id="latin-accent"),
        pytest.param("sk-\tx", id="inner-tab"),
        pytest.param("sk-\x7fx", id="delete-control"),
    ],
)
def test_constructor_rejects_an_api_key_with_invalid_characters(key: str) -> None:
    # Arrange: the key is the parameter

    # Act
    with pytest.raises(ValueError) as exc:
        _embedder(closed_port_base_url(), key=key)

    # Assert
    assert str(exc.value) == API_KEY_ERROR


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://api.openai.com/v1", id="http-on-a-public-host"),
        pytest.param("https://user:pw@api.openai.com/v1", id="userinfo"),
        pytest.param("https://api.openai.com/v1?x=1", id="query"),
        pytest.param("https://api.openai.com/v1#f", id="fragment"),
        pytest.param("ftp://api.openai.com", id="other-scheme"),
    ],
)
def test_constructor_rejects_an_unsafe_base_url_with_the_fixed_message(url: str) -> None:
    # Arrange: the url is the parameter

    # Act
    with pytest.raises(ValueError) as exc:
        OpenAiEmbedder(api_key=SecretStr(CANARY_KEY), base_url=url)

    # Assert
    assert str(exc.value) == BASE_URL_ERROR
    assert "user:pw" not in str(exc.value)


def test_constructor_accepts_the_default_base_url_and_a_loopback_http_base_url() -> None:
    # Arrange
    default = OpenAiEmbedder(api_key=SecretStr(CANARY_KEY))
    loopback = OpenAiEmbedder(api_key=SecretStr(CANARY_KEY), base_url="http://127.0.0.1:9/v1")

    # Act
    ids = (default.embedder_id, loopback.embedder_id)

    # Assert
    assert ids == (OPENAI_EMBEDDER_ID, OPENAI_EMBEDDER_ID)
