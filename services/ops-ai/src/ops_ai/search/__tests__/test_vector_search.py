import uuid

import pytest
from ops_ai.scope import Scope
from ops_ai.search.__tests__.embedded_documents import OCCURRED_AT, seed_document, seed_embedded
from ops_ai.search.__tests__.fake_embedders import (
    OUTAGE_MESSAGE,
    FixedVectorEmbedder,
    UnavailableEmbedder,
    axis_vector,
    blend_vector,
)
from ops_ai.search.embedder import (
    LOCAL_DIMENSIONS,
    LOCAL_EMBEDDER_ID,
    Embedder,
    EmbedderUnavailableError,
    LocalHashEmbedder,
)
from ops_ai.search.openai_embedder import OPENAI_DIMENSIONS, OPENAI_EMBEDDER_ID
from ops_ai.search.service import DEFAULT_MIN_SIMILARITY, SearchHit, vector_search
from sqlalchemy.orm import Session, sessionmaker

QUERY = "the question"
SIMILARITY_TOLERANCE = 1e-5


def _query_embedder(
    dimensions: int = LOCAL_DIMENSIONS, embedder_id: str = LOCAL_EMBEDDER_ID
) -> FixedVectorEmbedder:
    return FixedVectorEmbedder(
        vectors={QUERY: axis_vector(dimensions, 0)}, embedder_id=embedder_id, dimensions=dimensions
    )


def _search(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    query: str,
    embedder: Embedder,
    limit: int = 10,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> list[SearchHit]:
    with session_factory() as session:
        return vector_search(
            session,
            Scope(merchant_id=merchant_id),
            query,
            embedder,
            limit=limit,
            min_similarity=min_similarity,
        )


def _refs(hits: list[SearchHit]) -> list[uuid.UUID]:
    return [hit.ref_id for hit in hits]


def test_vector_search_returns_only_documents_of_the_scoped_merchant_for_identical_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant_a = uuid.uuid4()
    merchant_b = uuid.uuid4()
    ref_a = seed_embedded(session_factory, merchant_a, "Card payment declined", "insufficient funds")
    ref_b = seed_embedded(session_factory, merchant_b, "Card payment declined", "insufficient funds")
    embedder = LocalHashEmbedder()

    # Act
    hits_a = _search(session_factory, merchant_a, "insufficient funds", embedder)
    hits_b = _search(session_factory, merchant_b, "insufficient funds", embedder)

    # Assert
    assert _refs(hits_a) == [ref_a]
    assert _refs(hits_b) == [ref_b]


def test_vector_search_returns_nothing_for_a_merchant_without_documents(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seed_embedded(session_factory, uuid.uuid4(), "Card payment declined", "insufficient funds")

    # Act
    hits = _search(session_factory, uuid.uuid4(), "insufficient funds", LocalHashEmbedder())

    # Assert
    assert hits == []


def test_vector_search_returns_the_hit_fields_and_the_cosine_similarity_as_rank(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    ref_id = seed_document(
        session_factory,
        merchant,
        "Settlement delayed",
        "payout is late",
        kind="incident",
        local_vector=blend_vector(LOCAL_DIMENSIONS, 0.8),
    )

    # Act
    hits = _search(session_factory, merchant, QUERY, _query_embedder())

    # Assert
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.kind, hit.ref_id, hit.title, hit.body) == (
        "incident",
        ref_id,
        "Settlement delayed",
        "payout is late",
    )
    assert hit.occurred_at == OCCURRED_AT
    assert hit.rank == pytest.approx(0.8, abs=SIMILARITY_TOLERANCE)


def test_vector_search_orders_best_similarity_first(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    middle = seed_document(
        session_factory, merchant, "middle", "m", local_vector=blend_vector(LOCAL_DIMENSIONS, 0.6)
    )
    best = seed_document(
        session_factory, merchant, "best", "b", local_vector=axis_vector(LOCAL_DIMENSIONS, 0)
    )
    weaker = seed_document(
        session_factory, merchant, "weaker", "w", local_vector=blend_vector(LOCAL_DIMENSIONS, 0.8)
    )

    # Act
    hits = _search(session_factory, merchant, QUERY, _query_embedder())

    # Assert
    assert _refs(hits) == [best, weaker, middle]
    assert [hit.rank for hit in hits] == pytest.approx([1.0, 0.8, 0.6], abs=SIMILARITY_TOLERANCE)


def test_vector_search_excludes_rows_whose_embedding_is_null(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    embedded = seed_embedded(session_factory, merchant, "Card payment declined", "insufficient funds")
    seed_document(session_factory, merchant, "Card payment declined", "insufficient funds")

    # Act
    hits = _search(session_factory, merchant, "insufficient funds", LocalHashEmbedder())

    # Assert
    assert _refs(hits) == [embedded]


def test_vector_search_excludes_hits_below_the_default_threshold(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    below = blend_vector(LOCAL_DIMENSIONS, DEFAULT_MIN_SIMILARITY - 0.1)
    above = blend_vector(LOCAL_DIMENSIONS, DEFAULT_MIN_SIMILARITY + 0.1)
    opposite = [-value for value in axis_vector(LOCAL_DIMENSIONS, 0)]
    seed_document(session_factory, merchant, "below", "x", local_vector=below)
    seed_document(session_factory, merchant, "opposite", "x", local_vector=opposite)
    kept = seed_document(session_factory, merchant, "above", "x", local_vector=above)

    # Act
    hits = _search(session_factory, merchant, QUERY, _query_embedder())

    # Assert
    assert _refs(hits) == [kept]


def test_vector_search_uses_the_given_min_similarity(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    exact = seed_document(
        session_factory, merchant, "exact", "x", local_vector=axis_vector(LOCAL_DIMENSIONS, 0)
    )
    strong = seed_document(
        session_factory, merchant, "strong", "x", local_vector=blend_vector(LOCAL_DIMENSIONS, 0.8)
    )
    seed_document(session_factory, merchant, "weak", "x", local_vector=blend_vector(LOCAL_DIMENSIONS, 0.6))

    # Act
    hits = _search(session_factory, merchant, QUERY, _query_embedder(), min_similarity=0.7)

    # Assert
    assert _refs(hits) == [exact, strong]


def test_vector_search_keeps_a_hit_whose_similarity_equals_min_similarity(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    exact = seed_document(
        session_factory, merchant, "exact", "x", local_vector=axis_vector(LOCAL_DIMENSIONS, 0)
    )
    seed_document(session_factory, merchant, "near", "x", local_vector=blend_vector(LOCAL_DIMENSIONS, 0.9))

    # Act
    hits = _search(session_factory, merchant, QUERY, _query_embedder(), min_similarity=1.0)

    # Assert
    assert _refs(hits) == [exact]


def test_vector_search_respects_the_limit_after_ordering(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    third = seed_document(
        session_factory, merchant, "third", "x", local_vector=blend_vector(LOCAL_DIMENSIONS, 0.5)
    )
    first = seed_document(
        session_factory, merchant, "first", "x", local_vector=axis_vector(LOCAL_DIMENSIONS, 0)
    )
    second = seed_document(
        session_factory, merchant, "second", "x", local_vector=blend_vector(LOCAL_DIMENSIONS, 0.7)
    )

    # Act
    top_two = _search(session_factory, merchant, QUERY, _query_embedder(), limit=2)
    all_three = _search(session_factory, merchant, QUERY, _query_embedder(), limit=3)

    # Assert
    assert _refs(top_two) == [first, second]
    assert _refs(all_three) == [first, second, third]


def test_vector_search_embeds_the_query_text_once(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    embedder = _query_embedder()

    # Act
    _search(session_factory, merchant, QUERY, embedder)

    # Assert
    assert embedder.calls == [[QUERY]]


def test_vector_search_returns_nothing_for_a_query_without_tokens(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    seed_embedded(session_factory, merchant, "Card payment declined", "insufficient funds")

    # Act
    hits = _search(session_factory, merchant, "!!!", LocalHashEmbedder())

    # Assert
    assert hits == []


def test_vector_search_finds_a_typo_through_the_local_embedder_and_skips_unrelated_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    related = seed_embedded(session_factory, merchant, "Card payment declined", "insufficient funds")
    seed_embedded(session_factory, merchant, "Settlement payout delayed", "bank holiday")

    # Act
    hits = _search(session_factory, merchant, "insufficent", LocalHashEmbedder())

    # Assert
    assert _refs(hits) == [related]
    assert DEFAULT_MIN_SIMILARITY <= hits[0].rank <= 1.0


def test_vector_search_reads_the_column_of_the_embedder_it_is_given(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    local_only = seed_document(
        session_factory, merchant, "local only", "x", local_vector=axis_vector(LOCAL_DIMENSIONS, 0)
    )
    openai_only = seed_document(
        session_factory, merchant, "openai only", "x", openai_vector=axis_vector(OPENAI_DIMENSIONS, 0)
    )
    both = seed_document(
        session_factory,
        merchant,
        "both",
        "x",
        local_vector=blend_vector(LOCAL_DIMENSIONS, 0.8),
        openai_vector=blend_vector(OPENAI_DIMENSIONS, 0.6),
    )
    openai_embedder = _query_embedder(OPENAI_DIMENSIONS, OPENAI_EMBEDDER_ID)

    # Act
    local_hits = _search(session_factory, merchant, QUERY, _query_embedder())
    openai_hits = _search(session_factory, merchant, QUERY, openai_embedder)

    # Assert
    assert _refs(local_hits) == [local_only, both]
    assert _refs(openai_hits) == [openai_only, both]
    assert openai_hits[1].rank == pytest.approx(0.6, abs=SIMILARITY_TOLERANCE)


def test_vector_search_is_scoped_for_the_openai_column_too(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    own = seed_document(session_factory, mine, "own", "x", openai_vector=axis_vector(OPENAI_DIMENSIONS, 0))
    seed_document(session_factory, theirs, "other", "x", openai_vector=axis_vector(OPENAI_DIMENSIONS, 0))

    # Act
    hits = _search(session_factory, mine, QUERY, _query_embedder(OPENAI_DIMENSIONS, OPENAI_EMBEDDER_ID))

    # Assert
    assert _refs(hits) == [own]


def test_vector_search_raises_value_error_for_an_embedder_id_without_a_column(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    seed_embedded(session_factory, merchant, "Card payment declined", "insufficient funds")
    unknown = FixedVectorEmbedder(
        vectors={QUERY: axis_vector(LOCAL_DIMENSIONS, 0)}, embedder_id="no-such-embedder-v0"
    )

    # Act
    with pytest.raises(ValueError) as exc:
        _search(session_factory, merchant, QUERY, unknown)

    # Assert
    assert type(exc.value) is ValueError


def test_vector_search_propagates_the_embedder_unavailable_error(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    seed_embedded(session_factory, merchant, "Card payment declined", "insufficient funds")

    # Act
    with pytest.raises(EmbedderUnavailableError) as exc:
        _search(session_factory, merchant, QUERY, UnavailableEmbedder())

    # Assert
    assert str(exc.value) == OUTAGE_MESSAGE
