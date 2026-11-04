"""Tests for raise_ops_alert and deliver_pending, written from docs/specs/failure-tools.md section 12."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from gateway.clock import FixedClock
from ops_ai.alerts import (
    MAX_DELIVERY_ATTEMPTS,
    OPS_ALERT_CHANNEL,
    OPS_ALERTS_STREAM,
    deliver_pending,
    raise_ops_alert,
)
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

StreamEntries = list[tuple[str, dict[str, str]]]

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
FRAUD_ALERT = uuid.UUID("cccccccc-0000-4000-8000-000000000003")
TITLE = "Card testing suspected"
BODY = "12 micro card payments on 4 distinct cards over 1 min 50 s"
STREAM_FIELDS = {"alert_id", "kind", "severity", "merchant_id", "title", "created_at"}


class FailingRedis:
    """Stands in for a Redis client whose server is down."""

    def __init__(self) -> None:
        self.xadd_calls = 0

    def xadd(self, name: str, fields: dict[str, str], *args: object, **kwargs: object) -> str:
        self.xadd_calls += 1
        raise RedisConnectionError("redis is down")


def _failing_redis() -> tuple[FailingRedis, Redis]:
    fake = FailingRedis()
    return fake, cast(Redis, fake)


def _raise(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    *,
    source_key: str = "card_testing:k1",
    kind: str = "card_testing",
    severity: str = "high",
    merchant_id: uuid.UUID = MERCHANT,
    fraud_alert_id: uuid.UUID | None = None,
    title: str = TITLE,
    body: str = BODY,
) -> uuid.UUID | None:
    with session_factory.begin() as session:
        return raise_ops_alert(
            session,
            clock,
            source_key=source_key,
            kind=kind,
            severity=severity,
            merchant_id=merchant_id,
            fraud_alert_id=fraud_alert_id,
            title=title,
            body=body,
        )


def _raise_three(session_factory: sessionmaker[Session], clock: FixedClock) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for key in ("a", "b", "c"):
        raised = _raise(session_factory, clock, source_key=f"card_testing:{key}")
        assert raised is not None
        ids.append(raised)
        clock.advance(timedelta(minutes=1))
    return ids


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def _alerts(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, "SELECT * FROM ops_ai.ops_alerts ORDER BY created_at, alert_id")


def _deliveries(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    query = "SELECT * FROM ops_ai.ops_alert_deliveries ORDER BY created_at, delivery_id"
    return _rows(session_factory, query)


def _entries(redis_client: Redis) -> StreamEntries:
    return cast(StreamEntries, redis_client.xrange(OPS_ALERTS_STREAM))


def _without_id(row: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in row.items() if key != "delivery_id"}


# --- constants -------------------------------------------------------------------------------------------


def test_the_stream_name_channel_and_attempt_limit_are_the_specified_values() -> None:
    # Arrange / Act
    constants = (OPS_ALERTS_STREAM, OPS_ALERT_CHANNEL, MAX_DELIVERY_ATTEMPTS)

    # Assert
    assert constants == ("ops.alerts.v1", "redis_stream", 5)


# --- raise_ops_alert -------------------------------------------------------------------------------------


def test_raise_ops_alert_inserts_the_alert_and_returns_its_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    source_key = f"card_testing:{MERCHANT}:first"

    # Act
    alert_id = _raise(session_factory, clock, source_key=source_key, fraud_alert_id=FRAUD_ALERT)

    # Assert
    assert isinstance(alert_id, uuid.UUID)
    assert _alerts(session_factory) == [
        {
            "alert_id": alert_id,
            "source_key": source_key,
            "kind": "card_testing",
            "severity": "high",
            "merchant_id": MERCHANT,
            "fraud_alert_id": FRAUD_ALERT,
            "title": TITLE,
            "body": BODY,
            "created_at": clock(),
            "acknowledged_at": None,
            "acknowledged_by": None,
        }
    ]


def test_raise_ops_alert_stores_a_null_fraud_alert_id_when_none_is_given(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    _raise(session_factory, clock, fraud_alert_id=None)

    # Assert
    (alert,) = _alerts(session_factory)
    assert alert["fraud_alert_id"] is None


def test_raise_ops_alert_adds_one_pending_redis_stream_delivery_for_the_alert(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    alert_id = _raise(session_factory, clock)

    # Assert
    (delivery,) = _deliveries(session_factory)
    assert isinstance(delivery["delivery_id"], uuid.UUID)
    assert _without_id(delivery) == {
        "alert_id": alert_id,
        "channel": "redis_stream",
        "status": "pending",
        "attempts": 0,
        "stream_entry_id": None,
        "last_error": None,
        "created_at": clock(),
        "delivered_at": None,
    }


def test_raise_ops_alert_with_a_repeated_source_key_returns_none_and_keeps_one_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = _raise(session_factory, clock, source_key="card_testing:dup", title=TITLE)

    # Act
    second = _raise(session_factory, clock, source_key="card_testing:dup", title="a different title")

    # Assert
    assert isinstance(first, uuid.UUID)
    assert second is None
    (alert,) = _alerts(session_factory)
    assert (alert["alert_id"], alert["title"]) == (first, TITLE)
    assert len(_deliveries(session_factory)) == 1


def test_raise_ops_alert_with_different_source_keys_inserts_two_alerts_with_two_deliveries(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    first = _raise(session_factory, clock, source_key="card_testing:a")
    second = _raise(session_factory, clock, source_key="card_testing:b")

    # Assert
    assert isinstance(first, uuid.UUID)
    assert isinstance(second, uuid.UUID)
    assert first != second
    assert sorted(str(a["alert_id"]) for a in _alerts(session_factory)) == sorted([str(first), str(second)])
    delivered_ids = sorted(str(d["alert_id"]) for d in _deliveries(session_factory))
    assert delivered_ids == sorted([str(first), str(second)])


def test_raise_ops_alert_does_not_commit(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        alert_id = raise_ops_alert(
            session,
            clock,
            source_key="card_testing:rolled-back",
            kind="card_testing",
            severity="high",
            merchant_id=MERCHANT,
            fraud_alert_id=None,
            title=TITLE,
            body=BODY,
        )
        session.rollback()

    # Assert
    assert isinstance(alert_id, uuid.UUID)
    assert _alerts(session_factory) == []
    assert _deliveries(session_factory) == []


# --- deliver_pending: success ----------------------------------------------------------------------------


def test_deliver_pending_adds_one_stream_entry_with_exactly_the_specified_fields(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    alert_id = _raise(session_factory, clock, kind="card_testing", severity="high", merchant_id=MERCHANT)

    # Act
    delivered = deliver_pending(session_factory, redis_client, clock)

    # Assert
    entries = _entries(redis_client)
    assert delivered == 1
    assert len(entries) == 1
    fields = entries[0][1]
    assert set(fields) == STREAM_FIELDS
    assert {key: fields[key] for key in STREAM_FIELDS - {"created_at"}} == {
        "alert_id": str(alert_id),
        "kind": "card_testing",
        "severity": "high",
        "merchant_id": str(MERCHANT),
        "title": TITLE,
    }
    assert datetime.fromisoformat(fields["created_at"]) == clock()


def test_deliver_pending_marks_the_delivery_delivered_with_the_stream_entry_id_and_time(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    alert_id = _raise(session_factory, clock)
    clock.advance(timedelta(minutes=5))

    # Act
    deliver_pending(session_factory, redis_client, clock)

    # Assert
    ((entry_id, _fields),) = _entries(redis_client)
    (delivery,) = _deliveries(session_factory)
    assert delivery["alert_id"] == alert_id
    assert delivery["status"] == "delivered"
    assert delivery["stream_entry_id"] == entry_id
    assert delivery["delivered_at"] == clock()
    assert delivery["last_error"] is None


def test_deliver_pending_a_second_time_delivers_nothing_and_leaves_the_stream_unchanged(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    _raise(session_factory, clock)
    deliver_pending(session_factory, redis_client, clock)
    entries_before = _entries(redis_client)
    deliveries_before = _deliveries(session_factory)
    clock.advance(timedelta(minutes=5))

    # Act
    second = deliver_pending(session_factory, redis_client, clock)

    # Assert
    assert second == 0
    assert redis_client.xlen(OPS_ALERTS_STREAM) == 1
    assert _entries(redis_client) == entries_before
    assert _deliveries(session_factory) == deliveries_before


def test_deliver_pending_with_nothing_pending_returns_zero_and_creates_no_stream(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange: no alerts

    # Act
    delivered = deliver_pending(session_factory, redis_client, clock)

    # Assert
    assert delivered == 0
    assert redis_client.exists(OPS_ALERTS_STREAM) == 0


def test_deliver_pending_delivers_oldest_first_and_returns_the_count(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    ids = _raise_three(session_factory, clock)

    # Act
    delivered = deliver_pending(session_factory, redis_client, clock)

    # Assert
    assert delivered == 3
    assert [fields["alert_id"] for _entry_id, fields in _entries(redis_client)] == [str(i) for i in ids]


def test_deliver_pending_honours_the_limit_and_leaves_the_newest_pending(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    ids = _raise_three(session_factory, clock)

    # Act
    delivered = deliver_pending(session_factory, redis_client, clock, limit=2)

    # Assert
    assert delivered == 2
    assert [fields["alert_id"] for _entry_id, fields in _entries(redis_client)] == [str(ids[0]), str(ids[1])]
    statuses = {str(d["alert_id"]): d["status"] for d in _deliveries(session_factory)}
    assert statuses == {str(ids[0]): "delivered", str(ids[1]): "delivered", str(ids[2]): "pending"}


def test_deliver_pending_then_a_new_alert_delivers_only_the_new_one(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    _raise(session_factory, clock, source_key="card_testing:first")
    deliver_pending(session_factory, redis_client, clock)
    clock.advance(timedelta(minutes=1))
    second = _raise(session_factory, clock, source_key="card_testing:second")

    # Act
    delivered = deliver_pending(session_factory, redis_client, clock)

    # Assert
    assert delivered == 1
    assert redis_client.xlen(OPS_ALERTS_STREAM) == 2
    assert _entries(redis_client)[1][1]["alert_id"] == str(second)


def test_the_stream_fields_hold_neither_the_body_nor_the_fraud_alert_id_nor_a_payment_or_card_field(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    body = "body-sentinel-never-streamed"
    _raise(session_factory, clock, body=body, fraud_alert_id=FRAUD_ALERT)

    # Act
    deliver_pending(session_factory, redis_client, clock)

    # Assert
    ((_entry_id, fields),) = _entries(redis_client)
    values = " ".join(fields.values())
    assert set(fields) == STREAM_FIELDS
    assert body not in values
    assert str(FRAUD_ALERT) not in values
    assert {"payment_id", "card_ref", "card_bin", "card_last4", "body"} & set(fields) == set()


def test_alerts_of_different_merchants_each_get_their_own_stream_entry(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    _raise(session_factory, clock, source_key="card_testing:own", merchant_id=MERCHANT)
    clock.advance(timedelta(seconds=1))
    _raise(session_factory, clock, source_key="card_testing:other", merchant_id=OTHER_MERCHANT)

    # Act
    delivered = deliver_pending(session_factory, redis_client, clock)

    # Assert
    assert delivered == 2
    merchants = [fields["merchant_id"] for _entry_id, fields in _entries(redis_client)]
    assert merchants == [str(MERCHANT), str(OTHER_MERCHANT)]


def test_the_streamed_created_at_is_the_alerts_utc_creation_instant(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    raised_at = FixedClock(datetime(2026, 11, 5, 8, 30, 15, tzinfo=UTC))
    _raise(session_factory, raised_at)
    raised_at.advance(timedelta(hours=1))

    # Act
    deliver_pending(session_factory, redis_client, raised_at)

    # Assert
    ((_entry_id, fields),) = _entries(redis_client)
    assert datetime.fromisoformat(fields["created_at"]) == datetime(2026, 11, 5, 8, 30, 15, tzinfo=UTC)


# --- deliver_pending: Redis failure ----------------------------------------------------------------------


def test_a_redis_failure_leaves_the_delivery_pending_with_one_attempt_and_the_error_class_name(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _raise(session_factory, clock)
    _fake, failing = _failing_redis()

    # Act
    delivered = deliver_pending(session_factory, failing, clock)

    # Assert
    assert delivered == 0
    (delivery,) = _deliveries(session_factory)
    assert delivery["status"] == "pending"
    assert delivery["attempts"] == 1
    assert delivery["last_error"] == "ConnectionError"
    assert delivery["stream_entry_id"] is None
    assert delivery["delivered_at"] is None


def test_a_redis_failure_is_absorbed_by_deliver_pending_after_exactly_one_xadd(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _raise(session_factory, clock)
    fake, failing = _failing_redis()

    # Act
    delivered = deliver_pending(session_factory, failing, clock)

    # Assert
    assert delivered == 0
    assert fake.xadd_calls == 1


@pytest.mark.parametrize("failures", [1, 2, 3, 4])
def test_each_failure_below_the_attempt_limit_adds_one_attempt_and_keeps_the_delivery_pending(
    session_factory: sessionmaker[Session], clock: FixedClock, failures: int
) -> None:
    # Arrange
    _raise(session_factory, clock)
    _fake, failing = _failing_redis()

    # Act
    for _ in range(failures):
        deliver_pending(session_factory, failing, clock)

    # Assert
    (delivery,) = _deliveries(session_factory)
    assert (delivery["status"], delivery["attempts"], delivery["last_error"]) == (
        "pending",
        failures,
        "ConnectionError",
    )


def test_the_fifth_failure_marks_the_delivery_failed_with_five_attempts(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _raise(session_factory, clock)
    _fake, failing = _failing_redis()

    # Act
    results = [deliver_pending(session_factory, failing, clock) for _ in range(MAX_DELIVERY_ATTEMPTS)]

    # Assert
    assert results == [0] * MAX_DELIVERY_ATTEMPTS
    (delivery,) = _deliveries(session_factory)
    assert (delivery["status"], delivery["attempts"], delivery["last_error"]) == (
        "failed",
        5,
        "ConnectionError",
    )
    assert delivery["delivered_at"] is None


def test_a_failed_delivery_is_not_retried_even_when_redis_is_back(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    _raise(session_factory, clock)
    fake, failing = _failing_redis()
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        deliver_pending(session_factory, failing, clock)
    failed_state = _deliveries(session_factory)

    # Act
    still_failing = deliver_pending(session_factory, failing, clock)
    recovered = deliver_pending(session_factory, redis_client, clock)

    # Assert
    assert (still_failing, recovered) == (0, 0)
    assert fake.xadd_calls == MAX_DELIVERY_ATTEMPTS
    assert redis_client.exists(OPS_ALERTS_STREAM) == 0
    assert _deliveries(session_factory) == failed_state


def test_a_delivery_that_failed_twice_is_delivered_once_redis_recovers(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    alert_id = _raise(session_factory, clock)
    _fake, failing = _failing_redis()
    deliver_pending(session_factory, failing, clock)
    deliver_pending(session_factory, failing, clock)

    # Act
    delivered = deliver_pending(session_factory, redis_client, clock)

    # Assert
    assert delivered == 1
    ((entry_id, fields),) = _entries(redis_client)
    assert fields["alert_id"] == str(alert_id)
    (delivery,) = _deliveries(session_factory)
    assert (delivery["status"], delivery["stream_entry_id"]) == ("delivered", entry_id)


def test_a_failure_does_not_touch_deliveries_that_are_already_delivered(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    _raise(session_factory, clock, source_key="card_testing:done")
    deliver_pending(session_factory, redis_client, clock)
    delivered_before = _deliveries(session_factory)
    clock.advance(timedelta(minutes=1))
    _raise(session_factory, clock, source_key="card_testing:later")
    _fake, failing = _failing_redis()

    # Act
    delivered = deliver_pending(session_factory, failing, clock)

    # Assert
    assert delivered == 0
    deliveries = _deliveries(session_factory)
    assert deliveries[0] == delivered_before[0]
    assert (deliveries[1]["status"], deliveries[1]["attempts"]) == ("pending", 1)
    assert redis_client.xlen(OPS_ALERTS_STREAM) == 1
