import uuid
from datetime import timedelta

import pytest
from ops_ai.scope import Scope
from ops_ai.search.__tests__.embedded_documents import (
    NEEDLE_BODY,
    NEEDLE_WORD,
    OCCURRED_AT,
    seed_document,
    seed_embedded,
)
from ops_ai.search.__tests__.fake_embedders import (
    FixedVectorEmbedder,
    UnavailableEmbedder,
    axis_vector,
)
from ops_ai.search.embedder import (
    LOCAL_DIMENSIONS,
    Embedder,
    LocalHashEmbedder,
    cosine_similarity,
    document_text,
)
from ops_ai.search.service import (
    DEFAULT_MIN_SIMILARITY,
    SearchHit,
    hybrid_search,
    search_documents,
    vector_search,
)
from sqlalchemy.orm import Session, sessionmaker

SCORE_TOLERANCE = 1e-9
TYPO_QUERY = "insufficent"
TYPO_TITLE = "Card payment declined"
TYPO_BODY = "insufficient funds"
NEEDLE_QUERY = NEEDLE_WORD
NEEDLE_TITLE = "Card payment declined"
UNRELATED_TITLE = "Settlement payout delayed"
UNRELATED_BODY = "bank holiday"


def _hybrid(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    query: str,
    embedder: Embedder,
    limit: int = 10,
    k: int = 60,
) -> list[SearchHit]:
    with session_factory() as session:
        return hybrid_search(session, Scope(merchant_id=merchant_id), query, embedder, limit=limit, k=k)


def _keyword(session_factory: sessionmaker[Session], merchant_id: uuid.UUID, query: str) -> list[SearchHit]:
    with session_factory() as session:
        return search_documents(session, Scope(merchant_id=merchant_id), query)


def _vector(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, query: str, embedder: Embedder
) -> list[SearchHit]:
    with session_factory() as session:
        return vector_search(session, Scope(merchant_id=merchant_id), query, embedder)


def _similarity(query: str, title: str, body: str) -> float:
    embedder = LocalHashEmbedder()
    return cosine_similarity(embedder.embed([query])[0], embedder.embed([document_text(title, body)])[0])


def _refs(hits: list[SearchHit]) -> list[uuid.UUID]:
    return [hit.ref_id for hit in hits]


def test_hybrid_search_finds_a_typo_the_keyword_leg_misses_through_the_vector_leg(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    typo_doc = seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)
    seed_embedded(session_factory, merchant, UNRELATED_TITLE, UNRELATED_BODY)
    embedder = LocalHashEmbedder()
    assert _keyword(session_factory, merchant, TYPO_QUERY) == []
    assert _similarity(TYPO_QUERY, TYPO_TITLE, TYPO_BODY) >= DEFAULT_MIN_SIMILARITY
    assert _refs(_vector(session_factory, merchant, TYPO_QUERY, embedder)) == [typo_doc]

    # Act
    hits = _hybrid(session_factory, merchant, TYPO_QUERY, embedder)

    # Assert
    assert _refs(hits) == [typo_doc]
    assert hits[0].rank == pytest.approx(1 / 61, abs=SCORE_TOLERANCE)


def test_hybrid_search_finds_a_word_in_a_long_body_the_vector_leg_misses_through_the_keyword_leg(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    needle_doc = seed_embedded(session_factory, merchant, NEEDLE_TITLE, NEEDLE_BODY)
    seed_embedded(session_factory, merchant, UNRELATED_TITLE, UNRELATED_BODY)
    embedder = LocalHashEmbedder()
    assert _similarity(NEEDLE_QUERY, NEEDLE_TITLE, NEEDLE_BODY) < DEFAULT_MIN_SIMILARITY
    assert _vector(session_factory, merchant, NEEDLE_QUERY, embedder) == []
    assert _refs(_keyword(session_factory, merchant, NEEDLE_QUERY)) == [needle_doc]

    # Act
    hits = _hybrid(session_factory, merchant, NEEDLE_QUERY, embedder)

    # Assert
    assert _refs(hits) == [needle_doc]
    assert hits[0].rank == pytest.approx(1 / 61, abs=SCORE_TOLERANCE)


def test_hybrid_search_ranks_a_document_found_by_both_legs_with_the_sum_of_both_reciprocals(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    both = seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)
    seed_embedded(session_factory, merchant, UNRELATED_TITLE, UNRELATED_BODY)
    embedder = LocalHashEmbedder()
    query = "insufficient funds"
    assert _refs(_keyword(session_factory, merchant, query)) == [both]
    assert _refs(_vector(session_factory, merchant, query, embedder)) == [both]

    # Act
    hits = _hybrid(session_factory, merchant, query, embedder)

    # Assert
    assert _refs(hits) == [both]
    assert hits[0].rank == pytest.approx(2 / 61, abs=SCORE_TOLERANCE)


def test_hybrid_search_uses_the_given_k_in_the_fused_score(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    both = seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)

    # Act
    hits = _hybrid(session_factory, merchant, "insufficient funds", LocalHashEmbedder(), k=10)

    # Assert
    assert _refs(hits) == [both]
    assert hits[0].rank == pytest.approx(2 / 11, abs=SCORE_TOLERANCE)


def test_hybrid_search_returns_the_hit_fields_of_the_document(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    ref_id = seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY, kind="incident")

    # Act
    hits = _hybrid(session_factory, merchant, "insufficient funds", LocalHashEmbedder())

    # Assert
    assert [(hit.kind, hit.ref_id, hit.title, hit.body, hit.occurred_at) for hit in hits] == [
        ("incident", ref_id, TYPO_TITLE, TYPO_BODY, OCCURRED_AT)
    ]


def test_hybrid_search_fetches_deeper_than_the_limit_from_each_leg_before_fusing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    strong_keyword_only = seed_document(
        session_factory,
        merchant,
        "Timeout timeout",
        "timeout timeout timeout upstream",
        occurred_at=OCCURRED_AT + timedelta(hours=1),
    )
    in_both = seed_document(
        session_factory,
        merchant,
        "Card payment",
        "gateway reported a timeout once",
        local_vector=axis_vector(LOCAL_DIMENSIONS, 0),
    )
    embedder = FixedVectorEmbedder(vectors={"timeout": axis_vector(LOCAL_DIMENSIONS, 0)})
    assert _refs(_keyword(session_factory, merchant, "timeout")) == [strong_keyword_only, in_both]
    assert _refs(_vector(session_factory, merchant, "timeout", embedder)) == [in_both]

    # Act
    hits = _hybrid(session_factory, merchant, "timeout", embedder, limit=1)

    # Assert
    assert _refs(hits) == [in_both]
    assert hits[0].rank == pytest.approx(1 / 61 + 1 / 62, abs=SCORE_TOLERANCE)


def test_hybrid_search_respects_the_limit(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    for _ in range(4):
        seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)

    # Act
    hits = _hybrid(session_factory, merchant, "insufficient funds", LocalHashEmbedder(), limit=3)

    # Assert
    assert len(hits) == 3
    assert len(set(_refs(hits))) == 3


def test_hybrid_search_falls_back_to_the_keyword_leg_when_the_embedder_is_unavailable(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    weak = seed_embedded(session_factory, merchant, "Card payment", "gateway reported a timeout once")
    strong = seed_embedded(session_factory, merchant, "Timeout timeout", "timeout timeout timeout upstream")
    seed_embedded(session_factory, merchant, UNRELATED_TITLE, UNRELATED_BODY)
    assert _refs(_keyword(session_factory, merchant, "timeout")) == [strong, weak]
    embedder = UnavailableEmbedder()

    # Act
    hits = _hybrid(session_factory, merchant, "timeout", embedder)

    # Assert
    assert _refs(hits) == [strong, weak]
    assert [hit.rank for hit in hits] == pytest.approx([1 / 61, 1 / 62], abs=SCORE_TOLERANCE)
    assert embedder.calls == [["timeout"]]


def test_hybrid_search_fallback_does_not_return_vector_only_documents(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)

    # Act
    hits = _hybrid(session_factory, merchant, TYPO_QUERY, UnavailableEmbedder())

    # Assert
    assert hits == []


def test_hybrid_search_does_not_swallow_an_unknown_embedder_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)
    unknown = FixedVectorEmbedder(
        vectors={"insufficient funds": axis_vector(LOCAL_DIMENSIONS, 0)}, embedder_id="no-such-embedder-v0"
    )

    # Act
    with pytest.raises(ValueError) as exc:
        _hybrid(session_factory, merchant, "insufficient funds", unknown)

    # Assert
    assert type(exc.value) is ValueError


def test_hybrid_search_returns_each_merchant_only_its_own_documents_for_identical_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant_a = uuid.uuid4()
    merchant_b = uuid.uuid4()
    ref_a = seed_embedded(session_factory, merchant_a, TYPO_TITLE, TYPO_BODY)
    ref_b = seed_embedded(session_factory, merchant_b, TYPO_TITLE, TYPO_BODY)
    embedder = LocalHashEmbedder()

    # Act
    hits_a = _hybrid(session_factory, merchant_a, "insufficient funds", embedder)
    hits_b = _hybrid(session_factory, merchant_b, "insufficient funds", embedder)

    # Assert
    assert _refs(hits_a) == [ref_a]
    assert _refs(hits_b) == [ref_b]
    assert hits_a[0].rank == pytest.approx(2 / 61, abs=SCORE_TOLERANCE)


def test_hybrid_search_keyword_leg_never_returns_another_merchants_document(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    seed_embedded(session_factory, mine, UNRELATED_TITLE, UNRELATED_BODY)
    other = seed_document(session_factory, theirs, TYPO_TITLE, TYPO_BODY)
    embedder = LocalHashEmbedder()
    assert _refs(_keyword(session_factory, theirs, "insufficient")) == [other]

    # Act
    hits = _hybrid(session_factory, mine, "insufficient", embedder)

    # Assert
    assert hits == []


def test_hybrid_search_vector_leg_never_returns_another_merchants_document(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    seed_embedded(session_factory, mine, UNRELATED_TITLE, UNRELATED_BODY)
    other = seed_embedded(session_factory, theirs, TYPO_TITLE, TYPO_BODY)
    embedder = LocalHashEmbedder()
    assert _refs(_vector(session_factory, theirs, TYPO_QUERY, embedder)) == [other]

    # Act
    hits = _hybrid(session_factory, mine, TYPO_QUERY, embedder)

    # Assert
    assert hits == []


def test_hybrid_search_returns_nothing_for_a_query_without_tokens(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)

    # Act
    hits = _hybrid(session_factory, merchant, "!!!", LocalHashEmbedder())

    # Assert
    assert hits == []


def test_hybrid_search_returns_nothing_for_a_merchant_without_documents(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seed_embedded(session_factory, uuid.uuid4(), TYPO_TITLE, TYPO_BODY)

    # Act
    hits = _hybrid(session_factory, uuid.uuid4(), "insufficient funds", LocalHashEmbedder())

    # Assert
    assert hits == []
