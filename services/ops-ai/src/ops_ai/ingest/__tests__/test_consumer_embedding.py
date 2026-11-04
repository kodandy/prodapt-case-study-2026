"""apply_payment_event also writes the local-hash-v1 embedding of the payment's search document."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from contracts.events import PaymentEventV1
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.scope import Scope
from ops_ai.search.__tests__.embedded_documents import stored_vector
from ops_ai.search.embedder import LocalHashEmbedder, document_text
from ops_ai.search.service import vector_search
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
FLOAT4_TOLERANCE = 1e-6


def _clock_now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _event(
    *,
    payment_id: uuid.UUID | None = None,
    merchant_id: uuid.UUID | None = None,
    declined: bool = True,
    entity_version: int = 1,
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=OCCURRED_AT + timedelta(seconds=entity_version),
        entity_version=entity_version,
        payment_id=payment_id or uuid.uuid4(),
        merchant_id=merchant_id or uuid.uuid4(),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="400000" if declined else "424242",
        card_last4="0002" if declined else "4242",
    )


def _apply(session_factory: sessionmaker[Session], event: PaymentEventV1) -> bool:
    with session_factory() as session:
        applied = apply_payment_event(session, _clock_now, event)
        session.commit()
    return applied


def _title_and_body(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> tuple[str, str]:
    with session_factory() as session:
        row = session.execute(
            text("SELECT title, body FROM ops_ai.search_documents WHERE ref_id = :ref_id"),
            {"ref_id": payment_id},
        ).one()
    return str(row.title), str(row.body)


def _expected_embedding(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> list[float]:
    title, body = _title_and_body(session_factory, payment_id)
    return LocalHashEmbedder().embed([document_text(title, body)])[0]


def test_apply_payment_event_writes_the_local_embedding_of_the_search_document(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(declined=True)

    # Act
    _apply(session_factory, event)

    # Assert
    expected = _expected_embedding(session_factory, event.payment_id)
    assert len(expected) == 512
    assert stored_vector(session_factory, event.payment_id, "local") == pytest.approx(
        expected, abs=FLOAT4_TOLERANCE
    )


def test_apply_payment_event_does_not_write_the_openai_embedding(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(declined=True)

    # Act
    _apply(session_factory, event)

    # Assert
    assert stored_vector(session_factory, event.payment_id, "openai") is None


def test_apply_payment_event_embeds_a_succeeded_payment_too(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(declined=False)

    # Act
    _apply(session_factory, event)

    # Assert
    assert stored_vector(session_factory, event.payment_id, "local") == pytest.approx(
        _expected_embedding(session_factory, event.payment_id), abs=FLOAT4_TOLERANCE
    )


def test_apply_payment_event_refreshes_the_embedding_when_a_newer_version_updates_the_document(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    payment_id = uuid.uuid4()
    merchant_id = uuid.uuid4()
    _apply(session_factory, _event(payment_id=payment_id, merchant_id=merchant_id, declined=False))
    first_vector = stored_vector(session_factory, payment_id, "local")

    # Act
    _apply(
        session_factory,
        _event(payment_id=payment_id, merchant_id=merchant_id, declined=True, entity_version=2),
    )

    # Assert
    second_vector = stored_vector(session_factory, payment_id, "local")
    expected = _expected_embedding(session_factory, payment_id)
    assert second_vector == pytest.approx(expected, abs=FLOAT4_TOLERANCE)
    assert second_vector != first_vector


def test_apply_payment_event_keeps_the_embedding_when_an_older_version_is_dropped(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    payment_id = uuid.uuid4()
    merchant_id = uuid.uuid4()
    _apply(
        session_factory,
        _event(payment_id=payment_id, merchant_id=merchant_id, declined=True, entity_version=2),
    )
    before = stored_vector(session_factory, payment_id, "local")

    # Act
    _apply(
        session_factory,
        _event(payment_id=payment_id, merchant_id=merchant_id, declined=False, entity_version=1),
    )

    # Assert
    assert stored_vector(session_factory, payment_id, "local") == before
    assert before == pytest.approx(_expected_embedding(session_factory, payment_id), abs=FLOAT4_TOLERANCE)


def test_apply_payment_event_keeps_the_embedding_for_a_duplicate_event(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(declined=True)
    _apply(session_factory, event)
    before = stored_vector(session_factory, event.payment_id, "local")

    # Act
    second = _apply(session_factory, event)

    # Assert
    assert second is False
    assert stored_vector(session_factory, event.payment_id, "local") == before


def test_an_ingested_payment_is_found_by_the_vector_search_of_its_merchant_only(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    mine = _event(declined=True)
    theirs = _event(declined=True)
    _apply(session_factory, mine)
    _apply(session_factory, theirs)
    title, body = _title_and_body(session_factory, mine.payment_id)
    question = f"{title} {body}"

    # Act
    with session_factory() as session:
        hits = vector_search(session, Scope(merchant_id=mine.merchant_id), question, LocalHashEmbedder())

    # Assert
    assert [hit.ref_id for hit in hits] == [mine.payment_id]
    assert hits[0].rank == pytest.approx(1.0, abs=1e-5)
