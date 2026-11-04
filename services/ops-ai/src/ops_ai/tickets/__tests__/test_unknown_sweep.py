"""Tests for sweep_unknown_payments, written from the B12 tickets spec (section 4c)."""

import uuid
from datetime import UTC, datetime, timedelta

from contracts.events import PaymentEventV1
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.tickets.sources import UNKNOWN_GRACE, UNKNOWN_SWEEP_LIMIT, sweep_unknown_payments
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
PAST_GRACE = TEST_NOW - timedelta(hours=1)
TICKETS_QUERY = "SELECT * FROM ops_ai.tickets ORDER BY source_key"


def _event(
    n: int,
    at: datetime,
    *,
    state: str = "unknown",
    entity_version: int = 1,
    merchant_id: uuid.UUID = MERCHANT,
    provider: str | None = "acq_a",
    amount_minor: int = 1500,
    currency: str = "USD",
) -> PaymentEventV1:
    unknown = state == "unknown"
    declined = state == "declined"
    event_type = "payment.unknown" if unknown else ("payment.declined" if declined else "payment.succeeded")
    return PaymentEventV1.model_validate(
        {
            "event_id": uuid.UUID(int=100_000 + n * 10 + entity_version),
            "event_type": event_type,
            "occurred_at": at,
            "entity_version": entity_version,
            "payment_id": uuid.UUID(int=n),
            "merchant_id": merchant_id,
            "amount_minor": amount_minor,
            "currency": currency,
            "method": "card",
            "state": state,
            "provider": provider,
            "decline_code": "card_declined" if declined else None,
            "card_bin": "400000",
            "card_last4": "0002",
        }
    )


def _deliver(session_factory: sessionmaker[Session], *events: PaymentEventV1) -> None:
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)


def _sweep(
    session_factory: sessionmaker[Session],
    *,
    now: datetime = TEST_NOW,
    grace: timedelta = UNKNOWN_GRACE,
    limit: int = UNKNOWN_SWEEP_LIMIT,
) -> int:
    with session_factory.begin() as session:
        return sweep_unknown_payments(session, lambda: now, grace, limit)


def _tickets(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(TICKETS_QUERY)).mappings()]


def _keys(session_factory: sessionmaker[Session]) -> list[str]:
    return [str(row["source_key"]) for row in _tickets(session_factory)]


def test_the_sweep_defaults_are_a_15_minute_grace_and_a_limit_of_500() -> None:
    # Arrange / Act / Assert
    assert UNKNOWN_GRACE == timedelta(minutes=15)
    assert UNKNOWN_SWEEP_LIMIT == 500


def test_an_unknown_payment_past_the_grace_window_gets_a_payments_ops_ticket_with_the_exact_reason(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(1, PAST_GRACE)
    _deliver(session_factory, event)

    # Act
    created = _sweep(session_factory)

    # Assert
    (row,) = _tickets(session_factory)
    assert created == 1
    assert isinstance(row["ticket_id"], uuid.UUID)
    assert {key: value for key, value in row.items() if key != "ticket_id"} == {
        "source_key": f"unknown_payment:{event.payment_id}",
        "source": "unknown_payment",
        "queue": "payments_ops",
        "priority": "normal",
        "status": "open",
        "assignee": None,
        "merchant_id": MERCHANT,
        "title": "Payment outcome unknown",
        "reason": (
            f"Payment {event.payment_id} (1500 USD minor units, provider acq_a) "
            f"has been in state unknown since {PAST_GRACE.isoformat()}"
        ),
        "alert_id": None,
        "created_at": TEST_NOW,
        "updated_at": None,
    }


def test_the_reason_says_provider_none_when_the_payment_has_no_provider(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(1, PAST_GRACE, provider=None, amount_minor=250, currency="EUR")
    _deliver(session_factory, event)

    # Act
    created = _sweep(session_factory)

    # Assert
    (row,) = _tickets(session_factory)
    assert created == 1
    assert row["reason"] == (
        f"Payment {event.payment_id} (250 EUR minor units, provider none) "
        f"has been in state unknown since {PAST_GRACE.isoformat()}"
    )


def test_an_unknown_payment_inside_the_grace_window_gets_no_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _deliver(session_factory, _event(1, TEST_NOW - timedelta(minutes=14, seconds=59)))

    # Act
    created = _sweep(session_factory)

    # Assert
    assert created == 0
    assert _tickets(session_factory) == []


def test_an_unknown_payment_exactly_at_the_grace_boundary_gets_a_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(1, TEST_NOW - timedelta(minutes=15))
    _deliver(session_factory, event)

    # Act
    created = _sweep(session_factory)

    # Assert
    assert created == 1
    assert _keys(session_factory) == [f"unknown_payment:{event.payment_id}"]


def test_a_custom_grace_changes_which_payments_are_old_enough(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(1, TEST_NOW - timedelta(minutes=10))
    _deliver(session_factory, event)

    # Act
    with_default = _sweep(session_factory)
    with_short_grace = _sweep(session_factory, grace=timedelta(minutes=5))

    # Assert
    assert (with_default, with_short_grace) == (0, 1)
    assert _keys(session_factory) == [f"unknown_payment:{event.payment_id}"]


def test_a_payment_that_a_later_event_resolved_gets_no_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _deliver(
        session_factory,
        _event(1, PAST_GRACE, state="unknown", entity_version=1),
        _event(1, PAST_GRACE + timedelta(minutes=1), state="succeeded", entity_version=2),
    )

    # Act
    created = _sweep(session_factory)

    # Assert
    assert created == 0
    assert _tickets(session_factory) == []


def test_a_payment_that_a_later_event_declined_gets_no_ticket_while_another_unknown_one_does(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    still_unknown = _event(2, PAST_GRACE)
    _deliver(
        session_factory,
        _event(1, PAST_GRACE, state="unknown", entity_version=1),
        _event(1, PAST_GRACE + timedelta(minutes=1), state="declined", entity_version=2),
        still_unknown,
    )

    # Act
    created = _sweep(session_factory)

    # Assert
    assert created == 1
    assert _keys(session_factory) == [f"unknown_payment:{still_unknown.payment_id}"]


def test_payments_that_were_never_unknown_get_no_ticket(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _deliver(
        session_factory,
        _event(1, PAST_GRACE, state="succeeded"),
        _event(2, PAST_GRACE, state="declined"),
    )

    # Act
    created = _sweep(session_factory)

    # Assert
    assert created == 0
    assert _tickets(session_factory) == []


def test_a_rerun_creates_nothing_and_returns_zero(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _deliver(session_factory, _event(1, PAST_GRACE))
    first = _sweep(session_factory)
    (before,) = _tickets(session_factory)

    # Act
    second = _sweep(session_factory, now=TEST_NOW + timedelta(hours=1))

    # Assert
    assert (first, second) == (1, 0)
    assert _tickets(session_factory) == [before]


def test_a_rerun_does_not_reopen_a_ticket_that_is_already_resolved(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _deliver(session_factory, _event(1, PAST_GRACE))
    _sweep(session_factory)
    with session_factory.begin() as session:
        session.execute(text("UPDATE ops_ai.tickets SET status = 'assigned', assignee = 'alice'"))
        session.execute(text("UPDATE ops_ai.tickets SET status = 'resolved'"))

    # Act
    second = _sweep(session_factory)

    # Assert
    (row,) = _tickets(session_factory)
    assert second == 0
    assert (row["status"], row["assignee"]) == ("resolved", "alice")


def test_the_limit_caps_tickets_per_call_oldest_first_and_later_calls_pick_up_the_rest(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    oldest = _event(1, TEST_NOW - timedelta(hours=3))
    middle = _event(2, TEST_NOW - timedelta(hours=2))
    newest = _event(3, TEST_NOW - timedelta(hours=1))
    _deliver(session_factory, newest, oldest, middle)

    # Act
    first = _sweep(session_factory, limit=2)
    keys_after_first = _keys(session_factory)
    second = _sweep(session_factory, limit=2)

    # Assert
    assert first == 2
    assert keys_after_first == sorted(
        [f"unknown_payment:{oldest.payment_id}", f"unknown_payment:{middle.payment_id}"]
    )
    assert second == 1
    assert _keys(session_factory) == sorted(
        f"unknown_payment:{event.payment_id}" for event in (oldest, middle, newest)
    )


def test_a_limit_of_one_opens_exactly_one_ticket_for_the_oldest_payment(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    oldest = _event(1, TEST_NOW - timedelta(hours=3))
    newer = _event(2, TEST_NOW - timedelta(hours=2))
    _deliver(session_factory, newer, oldest)

    # Act
    created = _sweep(session_factory, limit=1)

    # Assert
    assert created == 1
    assert _keys(session_factory) == [f"unknown_payment:{oldest.payment_id}"]


def test_each_merchants_unknown_payment_gets_a_ticket_owned_by_that_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    mine = _event(1, PAST_GRACE, merchant_id=MERCHANT)
    theirs = _event(2, PAST_GRACE, merchant_id=OTHER_MERCHANT)
    _deliver(session_factory, mine, theirs)

    # Act
    created = _sweep(session_factory)

    # Assert
    owners = {str(row["source_key"]): row["merchant_id"] for row in _tickets(session_factory)}
    assert created == 2
    assert owners == {
        f"unknown_payment:{mine.payment_id}": MERCHANT,
        f"unknown_payment:{theirs.payment_id}": OTHER_MERCHANT,
    }


def test_the_sweep_does_not_commit(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _deliver(session_factory, _event(1, datetime(2026, 11, 3, 10, 0, tzinfo=UTC)))

    # Act
    with session_factory() as session:
        created = sweep_unknown_payments(session, lambda: TEST_NOW)
        session.rollback()

    # Assert
    assert created == 1
    assert _tickets(session_factory) == []
