"""Tests for the card_testing ticket source column, written from the B12 tickets spec (section 4d)."""

import uuid
from datetime import UTC, datetime, timedelta

from contracts.events import PaymentEventV1
from ops_ai.ingest.consumer import apply_payment_event
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

BASE = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
CLOCK_NOW = BASE + timedelta(hours=2)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")


def _burst_event(index: int) -> PaymentEventV1:
    declined = index < 6
    return PaymentEventV1(
        event_id=uuid.UUID(int=10_000 + index),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=BASE + timedelta(seconds=index * 10),
        origin="live",
        entity_version=1,
        payment_id=uuid.UUID(int=index + 1),
        merchant_id=MERCHANT,
        amount_minor=50,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="400000",
        card_last4="0002",
        card_ref=f"{index % 4 + 1:032x}",
    )


def _deliver_burst(session_factory: sessionmaker[Session]) -> None:
    for index in range(12):
        with session_factory.begin() as session:
            apply_payment_event(session, lambda: CLOCK_NOW, _burst_event(index))


def test_a_card_testing_burst_creates_a_fraud_high_ticket_with_source_card_testing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    _deliver_burst(session_factory)

    # Assert
    with session_factory() as session:
        rows = [dict(row) for row in session.execute(text("SELECT * FROM ops_ai.tickets")).mappings()]
    (row,) = rows
    assert row["source"] == "card_testing"
    assert (row["queue"], row["priority"], row["status"]) == ("fraud", "high", "open")
    assert row["source_key"] == f"card_testing:{MERCHANT}:{uuid.UUID(int=1)}"
    assert (row["assignee"], row["updated_at"]) == (None, None)
    assert row["created_at"] == CLOCK_NOW


def test_a_card_testing_burst_creates_the_ticket_only_once_across_the_whole_burst(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    _deliver_burst(session_factory)

    # Assert
    with session_factory() as session:
        sources = list(session.execute(text("SELECT source FROM ops_ai.tickets")).scalars())
    assert sources == ["card_testing"]
