"""Tests for the card_ref projection, written from docs/specs/sq3.md section 3 (consumer copy)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from ops_ai.ingest.consumer import apply_payment_event, consume_payments
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
CARD_REF = "0123456789abcdef0123456789abcdef"
OTHER_CARD_REF = "fedcba9876543210fedcba9876543210"
CONSUMER = "consumer-card-ref"


def _clock_now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _event(
    *, card_ref: str | None, entity_version: int = 1, payment_id: uuid.UUID | None = None
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined",
        occurred_at=OCCURRED_AT + timedelta(seconds=entity_version),
        entity_version=entity_version,
        payment_id=payment_id or uuid.uuid4(),
        merchant_id=uuid.UUID(int=7),
        amount_minor=50,
        currency="USD",
        method="card",
        state="declined",
        provider="acq_a",
        decline_code="card_declined",
        card_bin="400000",
        card_last4="0002",
        card_ref=card_ref,
    )


def _apply(session_factory: sessionmaker[Session], event: PaymentEventV1) -> None:
    with session_factory.begin() as session:
        apply_payment_event(session, _clock_now, event)


def _card_refs(session_factory: sessionmaker[Session]) -> dict[uuid.UUID, str | None]:
    with session_factory() as session:
        rows = session.execute(text("SELECT payment_id, card_ref FROM ops_ai.payment_facts"))
        return {row.payment_id: row.card_ref for row in rows}


def test_apply_payment_event_copies_the_card_ref_onto_the_payment_fact(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(card_ref=CARD_REF)

    # Act
    _apply(session_factory, event)

    # Assert
    assert _card_refs(session_factory) == {event.payment_id: CARD_REF}


def test_apply_payment_event_keeps_a_missing_card_ref_as_none(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    event = _event(card_ref=None)

    # Act
    _apply(session_factory, event)

    # Assert
    assert _card_refs(session_factory) == {event.payment_id: None}


def test_apply_payment_event_keeps_one_card_ref_per_payment_across_distinct_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _event(card_ref=CARD_REF)
    second = _event(card_ref=OTHER_CARD_REF)
    third = _event(card_ref=CARD_REF)

    # Act
    for event in (first, second, third):
        _apply(session_factory, event)

    # Assert
    assert _card_refs(session_factory) == {
        first.payment_id: CARD_REF,
        second.payment_id: OTHER_CARD_REF,
        third.payment_id: CARD_REF,
    }


def test_a_newer_version_of_a_payment_carries_its_card_ref_into_the_fact(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    payment_id = uuid.uuid4()
    created = _event(card_ref=None, entity_version=1, payment_id=payment_id)
    finalized = _event(card_ref=CARD_REF, entity_version=2, payment_id=payment_id)

    # Act
    _apply(session_factory, created)
    _apply(session_factory, finalized)

    # Assert
    assert _card_refs(session_factory) == {payment_id: CARD_REF}


def test_consume_payments_projects_the_card_ref_from_the_stream_payload(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    event = _event(card_ref=CARD_REF)
    redis_client.xadd(PAYMENTS_TOPIC, {"event_id": str(event.event_id), "payload": event.model_dump_json()})

    # Act
    read = consume_payments(session_factory, redis_client, _clock_now, CONSUMER)

    # Assert
    assert read == 1
    assert _card_refs(session_factory) == {event.payment_id: CARD_REF}


def test_consume_payments_accepts_an_old_payload_without_the_card_ref_field(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    event = _event(card_ref=None)
    payload = json.loads(event.model_dump_json())
    del payload["card_ref"]
    redis_client.xadd(PAYMENTS_TOPIC, {"event_id": str(event.event_id), "payload": json.dumps(payload)})

    # Act
    read = consume_payments(session_factory, redis_client, _clock_now, CONSUMER)

    # Assert
    assert read == 1
    assert _card_refs(session_factory) == {event.payment_id: None}
