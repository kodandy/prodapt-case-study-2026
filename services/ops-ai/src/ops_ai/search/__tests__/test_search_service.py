import uuid
from datetime import UTC, datetime

from ops_ai.scope import Scope
from ops_ai.search.models import SearchDocument
from ops_ai.search.service import SearchHit, search_documents
from sqlalchemy.orm import Session, sessionmaker

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)


def _seed(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    title: str,
    body: str,
    kind: str = "payment",
) -> uuid.UUID:
    ref_id = uuid.uuid4()
    with session_factory() as session:
        session.add(
            SearchDocument(
                kind=kind,
                ref_id=ref_id,
                merchant_id=merchant_id,
                title=title,
                body=body,
                occurred_at=OCCURRED_AT,
            )
        )
        session.commit()
    return ref_id


def _search(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, query: str, limit: int = 10
) -> list[SearchHit]:
    with session_factory() as session:
        return search_documents(session, Scope(merchant_id=merchant_id), query, limit=limit)


def test_search_documents_returns_only_documents_of_the_scoped_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant_a = uuid.uuid4()
    merchant_b = uuid.uuid4()
    body = "Visa card was declined by acq_a for insufficient funds"
    ref_a = _seed(session_factory, merchant_a, "Card payment declined", body)
    ref_b = _seed(session_factory, merchant_b, "Card payment declined", body)

    # Act
    hits_a = _search(session_factory, merchant_a, "insufficient")
    hits_b = _search(session_factory, merchant_b, "insufficient")

    # Assert
    assert [hit.ref_id for hit in hits_a] == [ref_a]
    assert [hit.ref_id for hit in hits_b] == [ref_b]


def test_search_documents_matches_a_body_word_and_returns_the_hit_fields(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    body = "Visa card was declined by acq_a for insufficient funds"
    ref_id = _seed(session_factory, merchant, "Card payment declined", body)

    # Act
    hits = _search(session_factory, merchant, "insufficient")

    # Assert
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.kind, hit.ref_id, hit.title, hit.body) == ("payment", ref_id, "Card payment declined", body)
    assert hit.occurred_at == OCCURRED_AT
    assert hit.rank > 0


def test_search_documents_matches_inflected_forms_of_a_title_word(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    ref_id = _seed(session_factory, merchant, "Card payment declined", "acq_a USD")

    # Act
    hits = _search(session_factory, merchant, "declines")

    # Assert
    assert [hit.ref_id for hit in hits] == [ref_id]


def test_search_documents_returns_an_empty_list_for_an_unrelated_word(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed(session_factory, merchant, "Card payment declined", "Visa card was declined by acq_a")

    # Act
    hits = _search(session_factory, merchant, "refrigerator")

    # Assert
    assert hits == []


def test_search_documents_returns_nothing_for_a_merchant_without_documents(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed(session_factory, uuid.uuid4(), "Card payment declined", "Visa card was declined by acq_a")

    # Act
    hits = _search(session_factory, uuid.uuid4(), "declined")

    # Assert
    assert hits == []


def test_search_documents_orders_by_rank_best_first(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    weak = _seed(session_factory, merchant, "Card payment", "gateway reported a timeout once")
    strong = _seed(session_factory, merchant, "Timeout timeout", "timeout timeout timeout upstream")

    # Act
    hits = _search(session_factory, merchant, "timeout")

    # Assert
    assert [hit.ref_id for hit in hits] == [strong, weak]
    assert hits[0].rank >= hits[1].rank


def test_search_documents_respects_the_limit(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    for _ in range(3):
        _seed(session_factory, merchant, "Card payment declined", "Visa card was declined by acq_a")

    # Act
    hits = _search(session_factory, merchant, "declined", limit=2)

    # Assert
    assert len(hits) == 2
