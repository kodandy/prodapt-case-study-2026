"""Tests for enqueue_alert_notification, written from docs/specs/sq3.md N-2."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from ops_ai.notify import (
    ALERT_NOTIFICATION_SCHEMA,
    NOTIFIED_FEATURES,
    alert_notification_id,
    enqueue_alert_notification,
)
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
RAISED_AT = datetime(2026, 11, 6, 9, 30, tzinfo=UTC)
FULL_FEATURES: dict[str, object] = {
    "micro_payments": 12,
    "card_payments": 12,
    "micro_share": 1.0,
    "distinct_cards": 4,
    "declined": 6,
    "decline_ratio": 0.5,
    "card_bins": ["400000", "424242"],
    "currencies": ["USD"],
    "reasons": ["micro_burst", "many_cards", "high_decline_ratio"],
    "card_ref": "0" * 32,
}
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


def _fraud_alert(session_factory: sessionmaker[Session], merchant_id: uuid.UUID = MERCHANT) -> uuid.UUID:
    alert_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_FRAUD_ALERT,
            {
                "alert_id": alert_id,
                "window_key": f"card_testing:{merchant_id}:{uuid.uuid4()}",
                "merchant_id": merchant_id,
                "first_payment_id": uuid.uuid4(),
                "at": RAISED_AT,
            },
        )
    return alert_id


def _enqueue(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    fraud_alert_id: uuid.UUID,
    *,
    payment_count: int = 12,
    features: dict[str, object] | None = None,
) -> uuid.UUID | None:
    with session_factory.begin() as session:
        return enqueue_alert_notification(
            session,
            clock,
            fraud_alert_id=fraud_alert_id,
            merchant_id=MERCHANT,
            kind="card_testing",
            payment_count=payment_count,
            features=FULL_FEATURES if features is None else features,
        )


def _notifications(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    query = text("SELECT * FROM ops_ai.alert_notifications ORDER BY created_at, notification_id")
    with session_factory() as session:
        return [dict(row) for row in session.execute(query).mappings()]


def test_the_schema_name_and_notified_features_are_the_specified_values() -> None:
    # Arrange / Act
    constants = (ALERT_NOTIFICATION_SCHEMA, NOTIFIED_FEATURES)

    # Assert
    assert constants == (
        "payplat.fraud_alert.v1",
        (
            "micro_payments",
            "card_payments",
            "micro_share",
            "distinct_cards",
            "declined",
            "decline_ratio",
            "currencies",
            "reasons",
        ),
    )


def test_the_notification_id_is_deterministic_per_alert_and_differs_between_alerts() -> None:
    # Arrange
    first, second = uuid.UUID(int=1), uuid.UUID(int=2)

    # Act
    ids = (alert_notification_id(first), alert_notification_id(first), alert_notification_id(second))

    # Assert
    assert ids[0] == ids[1]
    assert ids[0] != ids[2]
    assert ids[0] != first


def test_enqueue_inserts_one_pending_row_with_the_full_payload(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    fraud_alert_id = _fraud_alert(session_factory)

    # Act
    notification_id = _enqueue(session_factory, clock, fraud_alert_id)

    # Assert
    assert notification_id == alert_notification_id(fraud_alert_id)
    (row,) = _notifications(session_factory)
    assert row == {
        "notification_id": notification_id,
        "fraud_alert_id": fraud_alert_id,
        "merchant_id": MERCHANT,
        "payload": {
            "schema": "payplat.fraud_alert.v1",
            "notification_id": str(notification_id),
            "alert_id": str(fraud_alert_id),
            "kind": "card_testing",
            "merchant_id": str(MERCHANT),
            "payment_count": 12,
            "features": {
                "micro_payments": 12,
                "card_payments": 12,
                "micro_share": 1.0,
                "distinct_cards": 4,
                "declined": 6,
                "decline_ratio": 0.5,
                "currencies": ["USD"],
                "reasons": ["micro_burst", "many_cards", "high_decline_ratio"],
            },
            "raised_at": RAISED_AT.isoformat(),
        },
        "status": "pending",
        "attempts": 0,
        "last_error": None,
        "created_at": RAISED_AT,
        "delivered_at": None,
    }


def test_the_payload_drops_card_bins_and_every_key_outside_the_allowlist(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    fraud_alert_id = _fraud_alert(session_factory)

    # Act
    _enqueue(session_factory, clock, fraud_alert_id)

    # Assert
    (row,) = _notifications(session_factory)
    payload = row["payload"]
    assert isinstance(payload, dict)
    rendered = json.dumps(payload)
    assert set(payload["features"]) == set(NOTIFIED_FEATURES)
    assert "400000" not in rendered
    assert "424242" not in rendered
    assert "card_bins" not in rendered
    assert "card_ref" not in rendered


def test_the_payload_keeps_only_the_allowed_features_that_are_present(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    fraud_alert_id = _fraud_alert(session_factory)
    partial: dict[str, object] = {"micro_payments": 40, "distinct_cards": None, "card_bins": ["411111"]}

    # Act
    _enqueue(session_factory, clock, fraud_alert_id, payment_count=40, features=partial)

    # Assert
    (row,) = _notifications(session_factory)
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert payload["features"] == {"micro_payments": 40, "distinct_cards": None}
    assert payload["payment_count"] == 40


def test_a_second_enqueue_for_the_same_alert_returns_none_and_keeps_the_first_row(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    fraud_alert_id = _fraud_alert(session_factory)
    first = _enqueue(session_factory, clock, fraud_alert_id, payment_count=12)
    clock.advance(timedelta(minutes=5))

    # Act
    second = _enqueue(session_factory, clock, fraud_alert_id, payment_count=13)

    # Assert
    assert first == alert_notification_id(fraud_alert_id)
    assert second is None
    (row,) = _notifications(session_factory)
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert (row["created_at"], payload["payment_count"]) == (RAISED_AT, 12)


def test_two_alerts_get_two_notifications(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    first_alert = _fraud_alert(session_factory)
    second_alert = _fraud_alert(session_factory)

    # Act
    _enqueue(session_factory, clock, first_alert)
    _enqueue(session_factory, clock, second_alert)

    # Assert
    alert_ids = sorted(str(row["fraud_alert_id"]) for row in _notifications(session_factory))
    assert alert_ids == sorted([str(first_alert), str(second_alert)])


def test_enqueue_does_not_commit(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    clock = FixedClock(RAISED_AT)
    fraud_alert_id = _fraud_alert(session_factory)

    with session_factory() as session:
        # Act
        notification_id = enqueue_alert_notification(
            session,
            clock,
            fraud_alert_id=fraud_alert_id,
            merchant_id=MERCHANT,
            kind="card_testing",
            payment_count=12,
            features=FULL_FEATURES,
        )
        session.rollback()

    # Assert
    assert notification_id == alert_notification_id(fraud_alert_id)
    assert _notifications(session_factory) == []
