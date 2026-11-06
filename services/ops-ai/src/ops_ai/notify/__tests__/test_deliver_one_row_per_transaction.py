"""Tests for the one-row-per-transaction delivery, written from docs/specs/sq3.md N-4 "Hardening"."""

import uuid
from datetime import UTC, datetime, timedelta

from ops_ai.notify import (
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
LOCK_PENDING = text(
    "SELECT notification_id FROM ops_ai.alert_notifications WHERE status = 'pending' FOR UPDATE SKIP LOCKED"
)


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class LockProbingSink:
    """While sending the first row, a separate session tries to lock every pending row."""

    def __init__(self, session_factory: sessionmaker[Session], first_id: uuid.UUID) -> None:
        self._session_factory = session_factory
        self._first_id = first_id
        self.sent_ids: list[uuid.UUID] = []
        self.lockable_during_first_send: list[uuid.UUID] | None = None

    def send(self, message: AlertNotificationMessage, now: datetime) -> None:
        self.sent_ids.append(message.notification_id)
        if message.notification_id != self._first_id:
            return
        with self._session_factory() as probe:
            self.lockable_during_first_send = [row[0] for row in probe.execute(LOCK_PENDING)]
            probe.rollback()


class FailingSink:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: AlertNotificationMessage, now: datetime) -> None:
        self.calls += 1
        raise AlertSinkError("http_503")


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


def _raise_two(session_factory: sessionmaker[Session], clock: FixedClock) -> tuple[uuid.UUID, uuid.UUID]:
    first = _raise_notification(session_factory, clock)
    clock.advance(timedelta(minutes=1))
    second = _raise_notification(session_factory, clock)
    clock.advance(timedelta(minutes=1))
    return first, second


def _attempts(session_factory: sessionmaker[Session]) -> list[tuple[uuid.UUID, int, str]]:
    query = text(
        "SELECT notification_id, attempts, status FROM ops_ai.alert_notifications "
        "ORDER BY created_at, notification_id"
    )
    with session_factory() as session:
        return [(row[0], row[1], row[2]) for row in session.execute(query)]


def test_while_the_first_row_is_being_sent_only_the_other_pending_row_is_lockable(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    first, second = _raise_two(session_factory, clock)
    sink = LockProbingSink(session_factory, first)

    # Act
    delivered = deliver_alert_notifications(session_factory, sink, clock)

    # Assert
    assert delivered == 2
    assert sink.sent_ids == [first, second]
    assert sink.lockable_during_first_send == [second]


def test_a_sink_that_always_fails_adds_exactly_one_attempt_to_each_row_per_call(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    first, second = _raise_two(session_factory, clock)
    sink = FailingSink()

    # Act
    delivered = deliver_alert_notifications(session_factory, sink, clock)

    # Assert
    assert delivered == 0
    assert sink.calls == 2
    assert _attempts(session_factory) == [(first, 1, "pending"), (second, 1, "pending")]
