"""RED tests for B11 notification records, written from docs/specs/gateway-basic.md (section B11).

The notifier reads `gw.payments.v1` outbox rows it has not processed yet and applies the rules:
a confirmation for a live success, a "was this you?" alert for a flagged or risk-blocked payment, nothing for
backfill or a plain decline. It writes one record plus one `gw.notifications.v1` outbox row per
(source event, kind).
"""

import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

import pytest
from contracts.events import (
    NOTIFICATIONS_TOPIC,
    PAYMENTS_TOPIC,
    NotificationEventV1,
    NotificationKind,
    PaymentEventV1,
)
from gateway.accounts.service import create_merchant
from gateway.clock import FixedClock
from gateway.notifications.models import Notification, NotificationProcessedEvent
from gateway.notifications.service import (
    NOTIFICATION_NAMESPACE,
    notification_id,
    process_pending_notifications,
)
from gateway.outbox.models import OutboxEvent
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import ChargeRequest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000002"
HIGH_USD = 500_000
CONFIRMATION: NotificationKind = "payment_confirmation"
ALERT: NotificationKind = "suspicious_activity"
PAN_BIN_LAST4_FRAGMENTS = ("4242", "@", "card_bin", "card_last4", "email")
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
GATEWAY_LOGGER = "gateway"


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID


def _harness(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    shared: Harness | None = None,
    name: str = "Acme",
) -> Harness:
    """A merchant that charges through the real service; `shared` adds a second merchant on the same deps."""
    if shared is None:
        deps = ChargeDeps(
            session_factory=session_factory,
            redis=None,
            provider=SimulatorProvider(session_factory, clock),
            clock=clock,
            card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
        )
    else:
        deps = shared.deps
    with session_factory() as session:
        credentials = create_merchant(session, clock, name, TEST_API_KEY_PEPPER)
        session.commit()
    return Harness(deps, session_factory, clock, credentials.merchant_id)


def _charge(h: Harness, pan: str = APPROVE_PAN, amount_minor: int = 1500) -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": "USD",
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    outcome = charge_card(h.deps, h.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _prime(h: Harness, pan: str, count: int) -> None:
    """`count` small charges one minute apart; the clock ends `count` minutes after it started."""
    for _ in range(count):
        _charge(h, pan=pan)
        h.clock.advance(timedelta(minutes=1))


def _succeeded_payment(h: Harness) -> uuid.UUID:
    return _charge(h)


def _flagged_success(h: Harness) -> uuid.UUID:
    """Three earlier charges on the card (velocity) plus a high amount: score 0.80, flagged, not blocked."""
    _prime(h, APPROVE_PAN, 3)
    return _charge(h, amount_minor=HIGH_USD)


def _blocked_payment(h: Harness) -> uuid.UUID:
    """Three earlier declines on the card plus a high amount: score 1.00, declined as risk_blocked."""
    _prime(h, DECLINED_PAN, 3)
    return _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD)


def _plain_decline(h: Harness) -> uuid.UUID:
    return _charge(h, pan=DECLINED_PAN)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], query: str) -> int:
    with session_factory() as session:
        return int(session.execute(text(query)).scalar_one())


def _records_of(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT id, kind, recipient_type, recipient_id, source_event_id, payment_id, channel, payload, "
        "created_at FROM gateway.notifications WHERE payment_id = :pid ORDER BY kind",
        pid=payment_id,
    )


def _all_records(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT id, kind, recipient_type, recipient_id, source_event_id, payment_id, channel, payload, "
        "created_at FROM gateway.notifications ORDER BY created_at, id",
    )


def _kinds(records: list[dict[str, object]]) -> list[object]:
    return [record["kind"] for record in records]


def _notification_outbox(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT event_id, topic, payload, published_at FROM gateway.outbox WHERE topic = :topic ORDER BY id",
        topic=NOTIFICATIONS_TOPIC,
    )


def _payment_outbox_event_ids(session_factory: sessionmaker[Session]) -> list[uuid.UUID]:
    rows = _rows(
        session_factory,
        "SELECT event_id FROM gateway.outbox WHERE topic = :topic ORDER BY id",
        topic=PAYMENTS_TOPIC,
    )
    return [uuid.UUID(str(row["event_id"])) for row in rows]


def _processed_ids(session_factory: sessionmaker[Session]) -> set[uuid.UUID]:
    rows = _rows(session_factory, "SELECT event_id FROM gateway.notification_processed_events")
    return {uuid.UUID(str(row["event_id"])) for row in rows}


def _source_event(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> PaymentEventV1:
    rows = _rows(
        session_factory,
        "SELECT payload FROM gateway.outbox WHERE topic = :topic AND payload->>'payment_id' = :pid",
        topic=PAYMENTS_TOPIC,
        pid=str(payment_id),
    )
    assert len(rows) == 1
    return PaymentEventV1.model_validate(rows[0]["payload"])


def _enqueue_payload(
    session_factory: sessionmaker[Session], clock: FixedClock, event_id: uuid.UUID, payload: dict[str, object]
) -> None:
    with session_factory() as session:
        session.add(
            OutboxEvent(
                event_id=event_id,
                topic=PAYMENTS_TOPIC,
                payload=payload,
                headers={},
                created_at=clock(),
                published_at=None,
            )
        )
        session.commit()


def _payment_event(
    clock: FixedClock,
    payment_id: uuid.UUID | None = None,
    merchant_id: uuid.UUID | None = None,
    state: str = "succeeded",
    origin: str = "live",
) -> PaymentEventV1:
    return PaymentEventV1.model_validate(
        {
            "event_id": uuid.uuid4(),
            "event_type": f"payment.{state}",
            "occurred_at": clock(),
            "origin": origin,
            "entity_version": 1,
            "payment_id": payment_id or uuid.uuid4(),
            "merchant_id": merchant_id or uuid.uuid4(),
            "amount_minor": 1500,
            "currency": "USD",
            "method": "card",
            "state": state,
            "provider": "acq_a",
            "decline_code": None,
            "card_bin": "424242",
            "card_last4": "4242",
        }
    )


def _enqueue_event(session_factory: sessionmaker[Session], clock: FixedClock, event: PaymentEventV1) -> None:
    _enqueue_payload(session_factory, clock, event.event_id, json.loads(event.model_dump_json()))


def _gateway_messages(caplog: pytest.LogCaptureFixture, minimum: int = logging.INFO) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(GATEWAY_LOGGER) and record.levelno >= minimum
    ]


def _without_ids(value: str) -> str:
    return UUID_PATTERN.sub("<id>", value)


def _assert_no_card_or_email_data(value: str) -> None:
    visible = _without_ids(value).lower()
    leaked = [fragment for fragment in PAN_BIN_LAST4_FRAGMENTS if fragment in visible]
    assert leaked == []


def _alert_payload_keys(records: list[dict[str, object]]) -> set[str]:
    alerts = [record["payload"] for record in records if record["kind"] == ALERT]
    assert len(alerts) == 1
    payload = alerts[0]
    assert isinstance(payload, dict)
    return {str(key) for key in payload}


def _flag_payment_in_the_database(
    session_factory: sessionmaker[Session],
    payment_id: uuid.UUID,
    score: str | None,
    reasons: list[str],
) -> None:
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE gateway.payments SET risk_flagged = true, risk_score = CAST(:score AS numeric), "
                "risk_reasons = CAST(:reasons AS jsonb) WHERE id = :id"
            ),
            {"id": payment_id, "score": score, "reasons": json.dumps(reasons)},
        )
        session.commit()


# --- notification_id ---------------------------------------------------------------------------------


def test_notification_id_is_the_uuid5_of_the_source_event_and_the_kind() -> None:
    # Arrange
    source = uuid.UUID("11111111-2222-4333-8444-555555555555")

    # Act
    derived = notification_id(source, CONFIRMATION)

    # Assert
    assert derived == uuid.uuid5(NOTIFICATION_NAMESPACE, f"{source}:{CONFIRMATION}")


def test_notification_id_is_stable_across_calls() -> None:
    # Arrange
    source = uuid.UUID("11111111-2222-4333-8444-555555555555")

    # Act
    first = notification_id(source, ALERT)
    second = notification_id(source, ALERT)

    # Assert
    assert first == second


def test_notification_id_differs_per_kind_and_per_source_event() -> None:
    # Arrange
    source = uuid.UUID("11111111-2222-4333-8444-555555555555")
    other_source = uuid.UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")

    # Act
    confirmation = notification_id(source, CONFIRMATION)
    alert = notification_id(source, ALERT)
    other_confirmation = notification_id(other_source, CONFIRMATION)

    # Assert
    assert len({confirmation, alert, other_confirmation}) == 3


# --- rules ---------------------------------------------------------------------------------------------


def test_a_clean_live_success_creates_one_confirmation_for_the_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _succeeded_payment(h)
    source = _source_event(session_factory, payment_id)
    clock.advance(timedelta(hours=1))

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    records = _records_of(session_factory, payment_id)
    assert created == 1
    assert records == [
        {
            "id": notification_id(source.event_id, CONFIRMATION),
            "kind": CONFIRMATION,
            "recipient_type": "merchant",
            "recipient_id": h.merchant_id,
            "source_event_id": source.event_id,
            "payment_id": payment_id,
            "channel": "in_app",
            "payload": {"amount_minor": 1500, "currency": "USD"},
            "created_at": clock(),
        }
    ]


def test_a_flagged_success_creates_a_confirmation_and_an_alert_carrying_only_the_blocked_flag(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _flagged_success(h)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    records = _records_of(session_factory, payment_id)
    payloads = {record["kind"]: record["payload"] for record in records}
    assert created == 5
    assert _kinds(records) == [CONFIRMATION, ALERT]
    assert _alert_payload_keys(records) == {"amount_minor", "currency", "blocked"}
    assert payloads == {
        CONFIRMATION: {"amount_minor": HIGH_USD, "currency": "USD"},
        ALERT: {
            "amount_minor": HIGH_USD,
            "currency": "USD",
            "blocked": False,
        },
    }


def test_a_risk_blocked_decline_creates_only_the_alert_marked_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _blocked_payment(h)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    records = _records_of(session_factory, payment_id)
    assert created == 1
    assert _kinds(records) == [ALERT]
    assert records[0]["recipient_id"] == h.merchant_id
    assert _alert_payload_keys(records) == {"amount_minor", "currency", "blocked"}
    assert records[0]["payload"] == {
        "amount_minor": HIGH_USD,
        "currency": "USD",
        "blocked": True,
    }
    assert _all_records(session_factory) == records


def test_a_plain_decline_creates_no_record_but_is_marked_processed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _plain_decline(h)
    source = _source_event(session_factory, payment_id)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 0
    assert _all_records(session_factory) == []
    assert _notification_outbox(session_factory) == []
    assert _processed_ids(session_factory) == {source.event_id}


def test_a_flagged_provider_decline_creates_only_a_not_blocked_alert_from_the_payment_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _plain_decline(h)
    _flag_payment_in_the_database(session_factory, payment_id, "0.85", ["high_amount"])

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    records = _records_of(session_factory, payment_id)
    assert created == 1
    assert _kinds(records) == [ALERT]
    assert records[0]["payload"] == {
        "amount_minor": 1500,
        "currency": "USD",
        "blocked": False,
    }


def test_an_alert_for_a_payment_without_a_stored_score_carries_no_risk_keys(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _succeeded_payment(h)
    _flag_payment_in_the_database(session_factory, payment_id, None, [])

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    payloads = {record["kind"]: record["payload"] for record in _records_of(session_factory, payment_id)}
    assert created == 2
    alert = payloads[ALERT]
    assert alert == {"amount_minor": 1500, "currency": "USD", "blocked": False}
    assert isinstance(alert, dict)
    assert {"risk_score", "risk_reasons"} & set(alert) == set()


@pytest.mark.parametrize("state", ["failed", "unknown"])
def test_a_live_event_that_is_neither_a_success_nor_a_flagged_payment_creates_no_record(
    session_factory: sessionmaker[Session], clock: FixedClock, state: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _succeeded_payment(h)
    process_pending_notifications(session_factory, clock)
    event = _payment_event(clock, payment_id=payment_id, merchant_id=h.merchant_id, state=state)
    _enqueue_event(session_factory, clock, event)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 0
    assert len(_all_records(session_factory)) == 1
    assert event.event_id in _processed_ids(session_factory)


def test_a_backfill_event_creates_nothing_but_is_marked_processed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _flagged_success(h)
    process_pending_notifications(session_factory, clock)
    records_before = _all_records(session_factory)
    outbox_before = _notification_outbox(session_factory)
    live = _source_event(session_factory, payment_id)
    backfill = live.model_copy(update={"event_id": uuid.uuid4(), "origin": "backfill"})
    _enqueue_event(session_factory, clock, backfill)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 0
    assert _all_records(session_factory) == records_before
    assert _notification_outbox(session_factory) == outbox_before
    assert backfill.event_id in _processed_ids(session_factory)


def test_each_merchant_receives_the_confirmation_for_its_own_payment(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = _harness(session_factory, clock)
    second = _harness(session_factory, clock, shared=first, name="Other")
    first_payment = _charge(first)
    second_payment = _charge(second, amount_minor=2500)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    first_records = _records_of(session_factory, first_payment)
    second_records = _records_of(session_factory, second_payment)
    assert created == 2
    assert [(r["recipient_id"], r["payload"]) for r in first_records] == [
        (first.merchant_id, {"amount_minor": 1500, "currency": "USD"})
    ]
    assert [(r["recipient_id"], r["payload"]) for r in second_records] == [
        (second.merchant_id, {"amount_minor": 2500, "currency": "USD"})
    ]


def test_the_return_value_counts_records_not_events(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _plain_decline(h)
    _succeeded_payment(h)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 1
    assert len(_processed_ids(session_factory)) == 2


def test_an_empty_outbox_returns_zero_and_writes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: no outbox rows

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 0
    assert _count(session_factory, "SELECT count(*) FROM gateway.notifications") == 0
    assert _count(session_factory, "SELECT count(*) FROM gateway.notification_processed_events") == 0


# --- idempotency -----------------------------------------------------------------------------------------


def test_a_second_pass_returns_zero_and_changes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _flagged_success(h)
    process_pending_notifications(session_factory, clock)
    records_before = _all_records(session_factory)
    outbox_before = _notification_outbox(session_factory)
    processed_before = _processed_ids(session_factory)
    clock.advance(timedelta(hours=1))

    # Act
    second = process_pending_notifications(session_factory, clock)

    # Assert
    assert second == 0
    assert _all_records(session_factory) == records_before
    assert _notification_outbox(session_factory) == outbox_before
    assert _processed_ids(session_factory) == processed_before


def test_a_later_pass_picks_up_only_the_events_that_arrived_since(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    first_payment = _succeeded_payment(h)
    process_pending_notifications(session_factory, clock)
    second_payment = _charge(h, amount_minor=2500)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 1
    assert _kinds(_records_of(session_factory, first_payment)) == [CONFIRMATION]
    assert [r["payload"] for r in _records_of(session_factory, second_payment)] == [
        {"amount_minor": 2500, "currency": "USD"}
    ]
    assert len(_all_records(session_factory)) == 2


def test_an_event_already_marked_processed_is_skipped(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _succeeded_payment(h)
    source = _source_event(session_factory, payment_id)
    with session_factory.begin() as session:
        session.add(NotificationProcessedEvent(event_id=source.event_id, processed_at=clock()))

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 0
    assert _all_records(session_factory) == []
    assert _notification_outbox(session_factory) == []


def test_the_notifications_table_rejects_a_second_record_for_the_same_source_event_and_kind(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _succeeded_payment(h)
    source = _source_event(session_factory, payment_id)

    def record() -> Notification:
        return Notification(
            id=uuid.uuid4(),
            kind=CONFIRMATION,
            recipient_type="merchant",
            recipient_id=h.merchant_id,
            source_event_id=source.event_id,
            payment_id=payment_id,
            channel="in_app",
            payload={},
            created_at=clock(),
        )

    with session_factory.begin() as session:
        session.add(record())

    # Act
    with session_factory() as session:
        session.add(record())
        with pytest.raises(IntegrityError):
            session.flush()

    # Assert
    assert _count(session_factory, "SELECT count(*) FROM gateway.notifications") == 1


def test_the_processed_table_rejects_the_same_event_id_twice(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    event_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.add(NotificationProcessedEvent(event_id=event_id, processed_at=clock()))

    # Act
    with session_factory() as session:
        session.add(NotificationProcessedEvent(event_id=event_id, processed_at=clock()))
        with pytest.raises(IntegrityError):
            session.flush()

    # Assert
    assert _processed_ids(session_factory) == {event_id}


# --- deterministic ids and the outbound event ----------------------------------------------------------


def test_each_new_record_writes_one_outbox_event_on_the_notifications_topic_with_derived_ids(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _flagged_success(h)
    source = _source_event(session_factory, payment_id)
    clock.advance(timedelta(hours=1))

    # Act
    process_pending_notifications(session_factory, clock)

    # Assert
    rows = _notification_outbox(session_factory)
    events = {
        event.kind: event
        for event in (NotificationEventV1.model_validate(row["payload"]) for row in rows)
        if event.payment_id == payment_id
    }
    confirmation_id = notification_id(source.event_id, CONFIRMATION)
    alert_id = notification_id(source.event_id, ALERT)
    assert len(rows) == 5
    assert [row["topic"] for row in rows] == [NOTIFICATIONS_TOPIC] * 5
    assert [row["published_at"] for row in rows] == [None] * 5
    assert events == {
        CONFIRMATION: NotificationEventV1(
            event_id=uuid.uuid5(NOTIFICATION_NAMESPACE, f"event:{confirmation_id}"),
            event_type="notification.created",
            occurred_at=source.occurred_at,
            origin="live",
            notification_id=confirmation_id,
            kind=CONFIRMATION,
            source_event_id=source.event_id,
            recipient_type="merchant",
            recipient_id=h.merchant_id,
            payment_id=payment_id,
            amount_minor=HIGH_USD,
            currency="USD",
        ),
        ALERT: NotificationEventV1(
            event_id=uuid.uuid5(NOTIFICATION_NAMESPACE, f"event:{alert_id}"),
            event_type="notification.created",
            occurred_at=source.occurred_at,
            origin="live",
            notification_id=alert_id,
            kind=ALERT,
            source_event_id=source.event_id,
            recipient_type="merchant",
            recipient_id=h.merchant_id,
            payment_id=payment_id,
            amount_minor=HIGH_USD,
            currency="USD",
        ),
    }


def test_the_outbox_row_event_id_column_matches_the_payload_event_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _succeeded_payment(h)

    # Act
    process_pending_notifications(session_factory, clock)

    # Assert
    rows = _notification_outbox(session_factory)
    assert len(rows) == 1
    assert rows[0]["event_id"] == NotificationEventV1.model_validate(rows[0]["payload"]).event_id


def test_the_record_occurred_at_follows_the_source_event_and_created_at_follows_the_clock(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _succeeded_payment(h)
    source = _source_event(session_factory, payment_id)
    clock.advance(timedelta(days=2))

    # Act
    process_pending_notifications(session_factory, clock)

    # Assert
    rows = _notification_outbox(session_factory)
    record = _records_of(session_factory, payment_id)[0]
    assert NotificationEventV1.model_validate(rows[0]["payload"]).occurred_at == source.occurred_at
    assert record["created_at"] == clock()
    assert source.occurred_at != clock()


def test_the_notifier_does_not_consume_its_own_notification_events(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _succeeded_payment(h)
    process_pending_notifications(session_factory, clock)

    # Act
    second = process_pending_notifications(session_factory, clock)

    # Assert
    assert second == 0
    assert len(_notification_outbox(session_factory)) == 1
    assert _processed_ids(session_factory) == set(_payment_outbox_event_ids(session_factory))


def test_outbox_rows_on_other_topics_are_ignored_and_not_marked_processed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    foreign_event_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.add(
            OutboxEvent(
                event_id=foreign_event_id,
                topic="gw.other.v1",
                payload={"anything": "goes"},
                headers={},
                created_at=clock(),
                published_at=None,
            )
        )

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 0
    assert _processed_ids(session_factory) == set()
    assert _all_records(session_factory) == []


# --- bad input does not stall the pass ------------------------------------------------------------------


def _valid_payload(clock: FixedClock) -> dict[str, object]:
    payload: dict[str, object] = json.loads(_payment_event(clock).model_dump_json())
    return payload


def _negative_amount(payload: dict[str, object]) -> dict[str, object]:
    return {**payload, "amount_minor": -1}


def _unknown_pan_field(payload: dict[str, object]) -> dict[str, object]:
    return {**payload, "card_number": APPROVE_PAN, "email": "payer@example.test"}


def _missing_currency(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "currency"}


def _not_an_event(_payload: dict[str, object]) -> dict[str, object]:
    return {"unrelated": APPROVE_PAN}


@pytest.mark.parametrize(
    "corrupt",
    [_negative_amount, _unknown_pan_field, _missing_currency, _not_an_event],
    ids=["negative_amount", "unknown_fields", "missing_currency", "not_an_event"],
)
def test_a_payload_that_is_not_a_payment_event_is_marked_processed_with_a_warning_and_no_record(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
    corrupt: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger=GATEWAY_LOGGER)
    outbox_event_id = uuid.uuid4()
    _enqueue_payload(session_factory, clock, outbox_event_id, corrupt(_valid_payload(clock)))

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    warnings = _gateway_messages(caplog, logging.WARNING)
    assert created == 0
    assert _all_records(session_factory) == []
    assert _notification_outbox(session_factory) == []
    assert _processed_ids(session_factory) == {outbox_event_id}
    assert len(warnings) >= 1
    _assert_no_card_or_email_data("\n".join(_gateway_messages(caplog)))


def test_an_event_whose_payment_row_is_missing_is_marked_processed_with_a_warning_and_no_record(
    session_factory: sessionmaker[Session], clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger=GATEWAY_LOGGER)
    orphan = _payment_event(clock)
    _enqueue_event(session_factory, clock, orphan)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    warnings = _gateway_messages(caplog, logging.WARNING)
    assert created == 0
    assert _all_records(session_factory) == []
    assert _notification_outbox(session_factory) == []
    assert _processed_ids(session_factory) == {orphan.event_id}
    assert len(warnings) >= 1
    _assert_no_card_or_email_data("\n".join(_gateway_messages(caplog)))


def test_a_bad_row_ahead_of_a_good_one_does_not_stall_the_pass(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bad_event_id = uuid.uuid4()
    _enqueue_payload(session_factory, clock, bad_event_id, {"unrelated": "row"})
    orphan = _payment_event(clock)
    _enqueue_event(session_factory, clock, orphan)
    h = _harness(session_factory, clock)
    payment_id = _succeeded_payment(h)
    source = _source_event(session_factory, payment_id)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 1
    assert _kinds(_records_of(session_factory, payment_id)) == [CONFIRMATION]
    assert _processed_ids(session_factory) == {bad_event_id, orphan.event_id, source.event_id}


# --- the outbox row and the payment row are the authority ---------------------------------------------------


def test_a_payload_event_id_that_differs_from_the_outbox_rows_event_id_is_marked_processed_with_no_record(
    session_factory: sessionmaker[Session], clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger=GATEWAY_LOGGER)
    h = _harness(session_factory, clock)
    payment_id = _flagged_success(h)
    process_pending_notifications(session_factory, clock)
    records_before = _all_records(session_factory)
    outbox_before = _notification_outbox(session_factory)
    live = _source_event(session_factory, payment_id)
    row_event_id = uuid.uuid4()
    payload_event_id = uuid.uuid4()
    forged_payload = {**json.loads(live.model_dump_json()), "event_id": str(payload_event_id)}
    _enqueue_payload(session_factory, clock, row_event_id, forged_payload)
    caplog.clear()

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    assert created == 0
    assert _all_records(session_factory) == records_before
    assert _notification_outbox(session_factory) == outbox_before
    assert row_event_id in _processed_ids(session_factory)
    assert payload_event_id not in _processed_ids(session_factory)
    assert len(_gateway_messages(caplog, logging.WARNING)) >= 1


def test_a_payload_merchant_id_that_differs_from_the_payment_rows_is_marked_processed_with_no_record(
    session_factory: sessionmaker[Session], clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger=GATEWAY_LOGGER)
    owner = _harness(session_factory, clock)
    victim = _harness(session_factory, clock, shared=owner, name="Other")
    payment_id = _flagged_success(owner)
    process_pending_notifications(session_factory, clock)
    records_before = _all_records(session_factory)
    live = _source_event(session_factory, payment_id)
    forged = live.model_copy(update={"event_id": uuid.uuid4(), "merchant_id": victim.merchant_id})
    _enqueue_event(session_factory, clock, forged)
    caplog.clear()

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    recipients = {record["recipient_id"] for record in _all_records(session_factory)}
    assert created == 0
    assert _all_records(session_factory) == records_before
    assert recipients == {owner.merchant_id}
    assert forged.event_id in _processed_ids(session_factory)
    assert len(_gateway_messages(caplog, logging.WARNING)) >= 1


def test_recipient_amount_and_currency_come_from_the_payment_row_not_the_event(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _succeeded_payment(h)
    source = _source_event(session_factory, payment_id)
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.payments SET amount_minor = 2000, currency = 'EUR' WHERE id = :id"),
            {"id": payment_id},
        )

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    record = _records_of(session_factory, payment_id)[0]
    rows = _notification_outbox(session_factory)
    event = NotificationEventV1.model_validate(rows[0]["payload"])
    assert (source.amount_minor, source.currency) == (1500, "USD")
    assert created == 1
    assert record["recipient_id"] == h.merchant_id
    assert record["payload"] == {"amount_minor": 2000, "currency": "EUR"}
    assert (event.recipient_id, event.amount_minor, event.currency) == (h.merchant_id, 2000, "EUR")


def test_a_leftover_notification_outbox_row_with_the_derived_event_id_does_not_fail_the_pass(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _succeeded_payment(h)
    source = _source_event(session_factory, payment_id)
    record_id = notification_id(source.event_id, CONFIRMATION)
    derived_event_id = uuid.uuid5(NOTIFICATION_NAMESPACE, f"event:{record_id}")
    with session_factory.begin() as session:
        session.add(
            OutboxEvent(
                event_id=derived_event_id,
                topic=NOTIFICATIONS_TOPIC,
                payload={"leftover": "from an earlier run"},
                headers={},
                created_at=clock(),
                published_at=None,
            )
        )

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    outbox = _notification_outbox(session_factory)
    with_that_id = [row for row in outbox if row["event_id"] == derived_event_id]
    assert created == 1
    assert _kinds(_records_of(session_factory, payment_id)) == [CONFIRMATION]
    assert len(with_that_id) == 1
    assert source.event_id in _processed_ids(session_factory)


# --- batch_size ---------------------------------------------------------------------------------------------


def test_batch_size_limits_the_events_per_pass_and_takes_them_in_outbox_order(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    for _ in range(3):
        _succeeded_payment(h)
    event_ids = _payment_outbox_event_ids(session_factory)

    # Act
    first = process_pending_notifications(session_factory, clock, batch_size=2)
    after_first = {record["source_event_id"] for record in _all_records(session_factory)}
    second = process_pending_notifications(session_factory, clock, batch_size=2)
    third = process_pending_notifications(session_factory, clock, batch_size=2)

    # Assert
    assert [first, second, third] == [2, 1, 0]
    assert after_first == set(event_ids[:2])
    assert {record["source_event_id"] for record in _all_records(session_factory)} == set(event_ids)


def test_batch_size_counts_events_taken_even_when_they_create_no_record(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _plain_decline(h)
    _succeeded_payment(h)
    _succeeded_payment(h)

    # Act
    counts = [process_pending_notifications(session_factory, clock, batch_size=1) for _ in range(4)]

    # Assert
    assert counts == [0, 1, 1, 0]
    assert len(_all_records(session_factory)) == 2


def test_the_default_batch_size_is_one_hundred_events(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    for _ in range(101):
        _enqueue_event(session_factory, clock, _payment_event(clock))

    # Act
    first = process_pending_notifications(session_factory, clock)
    processed_after_first = len(_processed_ids(session_factory))
    second = process_pending_notifications(session_factory, clock)

    # Assert
    assert first == 0
    assert processed_after_first == 100
    assert second == 0
    assert len(_processed_ids(session_factory)) == 101


# --- no card or e-mail data ---------------------------------------------------------------------------------


def test_records_events_and_logs_carry_no_pan_bin_last4_or_email(
    session_factory: sessionmaker[Session], clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger=GATEWAY_LOGGER)
    h = _harness(session_factory, clock)
    _flagged_success(h)
    _blocked_payment(h)

    # Act
    created = process_pending_notifications(session_factory, clock)

    # Assert
    record_text = json.dumps([[str(value) for value in r.values()] for r in _all_records(session_factory)])
    event_text = json.dumps([row["payload"] for row in _notification_outbox(session_factory)], default=str)
    log_text = "\n".join(_gateway_messages(caplog, logging.DEBUG))
    assert created == 6
    assert "risk_score" not in record_text + event_text
    assert "risk_reasons" not in record_text + event_text
    _assert_no_card_or_email_data(record_text)
    _assert_no_card_or_email_data(event_text)
    _assert_no_card_or_email_data(log_text)


def test_each_new_record_logs_one_info_line_with_its_ids_and_kind(
    session_factory: sessionmaker[Session], clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger=GATEWAY_LOGGER)
    h = _harness(session_factory, clock)
    payment_id = _flagged_success(h)
    caplog.clear()

    # Act
    process_pending_notifications(session_factory, clock)

    # Assert
    records = _records_of(session_factory, payment_id)
    info_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(GATEWAY_LOGGER) and record.levelno == logging.INFO
    ]
    for stored in records:
        matching = [line for line in info_lines if str(stored["id"]) in line]
        assert len(matching) == 1
        assert str(stored["kind"]) in matching[0]


def test_a_pass_with_nothing_to_record_logs_no_info_line_for_a_record(
    session_factory: sessionmaker[Session], clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger=GATEWAY_LOGGER)
    h = _harness(session_factory, clock)
    _plain_decline(h)
    caplog.clear()

    # Act
    process_pending_notifications(session_factory, clock)

    # Assert
    kinds_logged = [line for line in _gateway_messages(caplog) if CONFIRMATION in line or ALERT in line]
    assert kinds_logged == []


# --- concurrency -------------------------------------------------------------------------------------------


def test_concurrent_passes_create_exactly_one_record_and_one_event_per_source_event_and_kind(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _flagged_success(h)
    for _ in range(3):
        _succeeded_payment(h)
    workers = 4
    rounds = 6
    barrier = threading.Barrier(workers)

    def pass_loop() -> int:
        barrier.wait(timeout=30)
        return sum(process_pending_notifications(session_factory, clock, batch_size=2) for _ in range(rounds))

    # Act
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(pass_loop) for _ in range(workers)]
        created = sum(future.result(timeout=120) for future in futures)
    created += process_pending_notifications(session_factory, clock)

    # Assert
    duplicates = _rows(
        session_factory,
        "SELECT source_event_id, kind, count(*) AS n FROM gateway.notifications "
        "GROUP BY source_event_id, kind HAVING count(*) > 1",
    )
    assert created == 8
    assert duplicates == []
    assert len(_all_records(session_factory)) == 8
    assert len(_notification_outbox(session_factory)) == 8
    assert _processed_ids(session_factory) == set(_payment_outbox_event_ids(session_factory))
