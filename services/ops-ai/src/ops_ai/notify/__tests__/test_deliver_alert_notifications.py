"""Tests for deliver_alert_notifications, written from docs/specs/sq3.md N-3."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.notify import (
    MAX_NOTIFICATION_ATTEMPTS,
    AlertNotificationMessage,
    AlertSinkError,
    deliver_alert_notifications,
    enqueue_alert_notification,
)
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
RAISED_AT = datetime(2026, 11, 6, 9, 30, tzinfo=UTC)
FEATURES: dict[str, object] = {"micro_payments": 12, "distinct_cards": 4, "reasons": ["micro_burst"]}
INSERT_FRAUD_ALERT = text(
    "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, started_at, "
    "last_seen_at, payment_count, features, created_at) VALUES (:alert_id, :window_key, :merchant_id, "
    "'card_testing', :first_payment_id, :at, :at, 12, '{}'::jsonb, :at)"
)


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class RecordingSink:
    def __init__(self) -> None:
        self.sent: list[tuple[AlertNotificationMessage, datetime]] = []

    def send(self, message: AlertNotificationMessage, now: datetime) -> None:
        self.sent.append((message, now))


class FailingSink:
    def __init__(self, reason: str = "http_503") -> None:
        self.reason = reason
        self.calls = 0

    def send(self, message: AlertNotificationMessage, now: datetime) -> None:
        self.calls += 1
        raise AlertSinkError(self.reason)


def _raise_notification(session_factory: sessionmaker[Session], clock: FixedClock) -> uuid.UUID:
    alert_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_FRAUD_ALERT,
            {
                "alert_id": alert_id,
                "window_key": f"card_testing:{MERCHANT}:{uuid.uuid4()}",
                "merchant_id": MERCHANT,
                "first_payment_id": uuid.uuid4(),
                "at": clock(),
            },
        )
        notification_id = enqueue_alert_notification(
            session,
            clock,
            fraud_alert_id=alert_id,
            merchant_id=MERCHANT,
            kind="card_testing",
            payment_count=12,
            features=FEATURES,
        )
    assert isinstance(notification_id, uuid.UUID)
    return notification_id


def _raise_three(session_factory: sessionmaker[Session], clock: FixedClock) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for _ in range(3):
        ids.append(_raise_notification(session_factory, clock))
        clock.advance(timedelta(minutes=1))
    return ids


def _rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    query = text("SELECT * FROM ops_ai.alert_notifications ORDER BY created_at, notification_id")
    with session_factory() as session:
        return [dict(row) for row in session.execute(query).mappings()]


def test_the_attempt_limit_is_five() -> None:
    # Arrange / Act
    limit = MAX_NOTIFICATION_ATTEMPTS

    # Assert
    assert limit == 5


def test_delivery_sends_the_payload_as_compact_sorted_json_and_marks_the_row_delivered(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    notification_id = _raise_notification(session_factory, clock)
    (pending,) = _rows(session_factory)
    clock.advance(timedelta(seconds=30))
    sink = RecordingSink()

    # Act
    delivered = deliver_alert_notifications(session_factory, sink, clock)

    # Assert
    assert delivered == 1
    ((message, sent_at),) = sink.sent
    assert message.notification_id == notification_id
    assert message.body == json.dumps(pending["payload"], sort_keys=True, separators=(",", ":")).encode()
    assert sent_at == clock()
    (row,) = _rows(session_factory)
    assert (row["status"], row["attempts"], row["last_error"], row["delivered_at"]) == (
        "delivered",
        0,
        None,
        clock(),
    )


def test_a_second_pass_sends_nothing_again(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    _raise_notification(session_factory, clock)
    sink = RecordingSink()
    deliver_alert_notifications(session_factory, sink, clock)
    rows_before = _rows(session_factory)

    # Act
    second = deliver_alert_notifications(session_factory, sink, clock)

    # Assert
    assert second == 0
    assert len(sink.sent) == 1
    assert _rows(session_factory) == rows_before


def test_nothing_pending_returns_zero_and_sends_nothing(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    sink = RecordingSink()

    # Act
    delivered = deliver_alert_notifications(session_factory, sink, FixedClock(RAISED_AT))

    # Assert
    assert delivered == 0
    assert sink.sent == []


def test_delivery_goes_oldest_first_and_honours_the_limit(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    ids = _raise_three(session_factory, clock)
    sink = RecordingSink()

    # Act
    delivered = deliver_alert_notifications(session_factory, sink, clock, limit=2)

    # Assert
    assert delivered == 2
    assert [message.notification_id for message, _now in sink.sent] == ids[:2]
    statuses = {row["notification_id"]: row["status"] for row in _rows(session_factory)}
    assert statuses == {ids[0]: "delivered", ids[1]: "delivered", ids[2]: "pending"}


def test_a_sink_error_adds_one_attempt_and_records_the_reason(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    _raise_notification(session_factory, clock)
    sink = FailingSink("http_503")

    # Act
    delivered = deliver_alert_notifications(session_factory, sink, clock)

    # Assert
    assert delivered == 0
    assert sink.calls == 1
    (row,) = _rows(session_factory)
    assert (row["status"], row["attempts"], row["last_error"], row["delivered_at"]) == (
        "pending",
        1,
        "http_503",
        None,
    )


def test_the_recorded_reason_is_cut_to_64_characters(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    _raise_notification(session_factory, clock)

    # Act
    deliver_alert_notifications(session_factory, FailingSink("x" * 100), clock)

    # Assert
    (row,) = _rows(session_factory)
    assert row["last_error"] == "x" * 64


@pytest.mark.parametrize("failures", [1, 2, 3, 4])
def test_failures_below_the_limit_keep_the_row_pending(
    session_factory: sessionmaker[Session], failures: int
) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    _raise_notification(session_factory, clock)
    sink = FailingSink()

    # Act
    for _ in range(failures):
        deliver_alert_notifications(session_factory, sink, clock)

    # Assert
    (row,) = _rows(session_factory)
    assert (row["status"], row["attempts"]) == ("pending", failures)


def test_the_fifth_failure_marks_the_row_failed_and_it_is_never_sent_again(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    _raise_notification(session_factory, clock)
    failing = FailingSink()
    for _ in range(MAX_NOTIFICATION_ATTEMPTS):
        deliver_alert_notifications(session_factory, failing, clock)
    recovered = RecordingSink()

    # Act
    delivered = deliver_alert_notifications(session_factory, recovered, clock)

    # Assert
    assert delivered == 0
    assert recovered.sent == []
    assert failing.calls == MAX_NOTIFICATION_ATTEMPTS
    (row,) = _rows(session_factory)
    assert (row["status"], row["attempts"], row["delivered_at"]) == ("failed", 5, None)


def test_a_row_that_failed_twice_is_delivered_once_the_sink_recovers(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    notification_id = _raise_notification(session_factory, clock)
    failing = FailingSink()
    deliver_alert_notifications(session_factory, failing, clock)
    deliver_alert_notifications(session_factory, failing, clock)
    recovered = RecordingSink()

    # Act
    delivered = deliver_alert_notifications(session_factory, recovered, clock)

    # Assert
    assert delivered == 1
    assert [message.notification_id for message, _now in recovered.sent] == [notification_id]
    (row,) = _rows(session_factory)
    assert (row["status"], row["attempts"], row["last_error"]) == ("delivered", 2, None)


def test_a_failure_does_not_touch_already_delivered_rows(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    _raise_notification(session_factory, clock)
    deliver_alert_notifications(session_factory, RecordingSink(), clock)
    delivered_before = _rows(session_factory)[0]
    clock.advance(timedelta(minutes=1))
    _raise_notification(session_factory, clock)

    # Act
    deliver_alert_notifications(session_factory, FailingSink(), clock)

    # Assert
    rows = _rows(session_factory)
    assert rows[0] == delivered_before
    assert (rows[1]["status"], rows[1]["attempts"]) == ("pending", 1)
