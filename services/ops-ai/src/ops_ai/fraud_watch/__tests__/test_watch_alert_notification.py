"""Tests for the outbound notification enqueued by watch_payment, written from docs/specs/sq3.md N-2."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from contracts.events import PaymentEventV1
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.notify import alert_notification_id
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

BASE = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
CLOCK_T1 = BASE + timedelta(hours=2)
CLOCK_T2 = BASE + timedelta(hours=3)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
CARD_BIN = "400000"
Origin = Literal["live", "backfill"]


@dataclass
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def _event(n: int, at: datetime, *, card: int, declined: bool, origin: Origin) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=10_000 + n),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=at,
        origin=origin,
        entity_version=1,
        payment_id=uuid.UUID(int=n),
        merchant_id=MERCHANT,
        amount_minor=50,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin=CARD_BIN,
        card_last4="0002",
        card_ref=f"{card:032x}",
    )


def _burst(
    *, count: int = 12, first_n: int = 1, start: datetime = BASE, cards: int = 4, origin: Origin = "live"
) -> list[PaymentEventV1]:
    return [
        _event(
            first_n + i,
            start + timedelta(seconds=i * 10),
            card=i % cards + 1,
            declined=i < 6,
            origin=origin,
        )
        for i in range(count)
    ]


def _deliver_all(
    session_factory: sessionmaker[Session], events: list[PaymentEventV1], clock: MutableClock
) -> None:
    for event in events:
        with session_factory.begin() as session:
            apply_payment_event(session, clock, event)


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def _notifications(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, "SELECT * FROM ops_ai.alert_notifications ORDER BY created_at")


def _fraud_alerts(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, "SELECT * FROM ops_ai.fraud_alerts ORDER BY started_at")


def test_a_live_card_testing_burst_enqueues_exactly_one_pending_notification_for_its_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst()

    # Act
    _deliver_all(session_factory, events, MutableClock(CLOCK_T1))

    # Assert
    (fraud_alert,) = _fraud_alerts(session_factory)
    (row,) = _notifications(session_factory)
    alert_id = fraud_alert["alert_id"]
    assert isinstance(alert_id, uuid.UUID)
    assert (row["notification_id"], row["fraud_alert_id"], row["merchant_id"]) == (
        alert_notification_id(alert_id),
        alert_id,
        MERCHANT,
    )
    assert (row["status"], row["attempts"], row["created_at"]) == ("pending", 0, CLOCK_T1)


def test_the_payload_carries_the_alert_merchant_count_and_features_at_raise_time(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst()

    # Act
    _deliver_all(session_factory, events, MutableClock(CLOCK_T1))

    # Assert
    (fraud_alert,) = _fraud_alerts(session_factory)
    (row,) = _notifications(session_factory)
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert payload["schema"] == "payplat.fraud_alert.v1"
    assert payload["alert_id"] == str(fraud_alert["alert_id"])
    assert payload["merchant_id"] == str(MERCHANT)
    assert payload["kind"] == "card_testing"
    assert payload["payment_count"] == 10
    assert payload["features"] == {
        "micro_payments": 10,
        "card_payments": 10,
        "micro_share": 1.0,
        "distinct_cards": 4,
        "declined": 6,
        "decline_ratio": 0.6,
        "currencies": ["USD"],
        "reasons": ["micro_burst", "many_cards", "high_decline_ratio"],
    }
    assert payload["raised_at"] == CLOCK_T1.isoformat()


def test_the_payload_holds_no_payment_id_card_ref_bin_or_last4(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst()

    # Act
    _deliver_all(session_factory, events, MutableClock(CLOCK_T1))

    # Assert
    (row,) = _notifications(session_factory)
    rendered = json.dumps(row["payload"])
    leaks = [str(e.payment_id) for e in events if str(e.payment_id) in rendered]
    leaks += [e.card_ref for e in events if e.card_ref and e.card_ref in rendered]
    assert leaks == []
    assert CARD_BIN not in rendered
    assert "0002" not in rendered


def test_later_events_of_the_same_episode_enqueue_no_second_notification(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _deliver_all(session_factory, _burst(), MutableClock(CLOCK_T1))
    before = _notifications(session_factory)

    # Act
    _deliver_all(
        session_factory,
        _burst(count=3, first_n=13, start=BASE + timedelta(seconds=120)),
        MutableClock(CLOCK_T2),
    )

    # Assert
    assert _notifications(session_factory) == before
    (fraud_alert,) = _fraud_alerts(session_factory)
    assert fraud_alert["payment_count"] == 15


def test_redelivered_events_enqueue_no_second_notification(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    events = _burst()
    _deliver_all(session_factory, events, MutableClock(CLOCK_T1))
    before = _notifications(session_factory)

    # Act
    _deliver_all(session_factory, events, MutableClock(CLOCK_T2))

    # Assert
    assert _notifications(session_factory) == before


def test_a_backfilled_burst_enqueues_no_notification(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    events = _burst(origin="backfill")

    # Act
    _deliver_all(session_factory, events, MutableClock(CLOCK_T1))

    # Assert
    assert _fraud_alerts(session_factory) == []
    assert _notifications(session_factory) == []


def test_a_single_card_series_enqueues_no_notification(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    events = _burst(cards=1)

    # Act
    _deliver_all(session_factory, events, MutableClock(CLOCK_T1))

    # Assert
    assert _notifications(session_factory) == []


def test_the_notification_is_written_in_the_watcher_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst()

    # Act
    with session_factory() as session:
        for event in events:
            apply_payment_event(session, MutableClock(CLOCK_T1), event)
        session.rollback()

    # Assert
    assert _fraud_alerts(session_factory) == []
    assert _notifications(session_factory) == []
