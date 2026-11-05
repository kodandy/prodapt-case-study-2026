"""Tests for the ops alert raised by watch_payment, written from docs/specs/failure-tools.md section 12."""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from contracts.events import PaymentEventV1
from ops_ai.alerts import OPS_ALERTS_STREAM, deliver_pending
from ops_ai.fraud_watch import watch_payment
from ops_ai.ingest.consumer import apply_payment_event
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

BASE = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
CLOCK_T1 = BASE + timedelta(hours=2)
CLOCK_T2 = BASE + timedelta(hours=3)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
Origin = Literal["live", "backfill"]
StreamEntries = list[tuple[str, dict[str, str]]]


@dataclass
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def _card_ref(n: int) -> str:
    return f"{n:032x}"


def _event(
    n: int, at: datetime, *, card: int = 1, declined: bool = True, origin: Origin = "live"
) -> PaymentEventV1:
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
        card_bin="400000",
        card_last4="0002",
        card_ref=_card_ref(card),
    )


def _burst(
    *,
    count: int = 12,
    start: datetime = BASE,
    first_n: int = 1,
    cards: int = 4,
    declined: int = 6,
    origin: Origin = "live",
) -> list[PaymentEventV1]:
    return [
        _event(
            first_n + index,
            start + timedelta(seconds=index * 10),
            card=index % cards + 1,
            declined=index < declined,
            origin=origin,
        )
        for index in range(count)
    ]


def _deliver(
    session_factory: sessionmaker[Session], event: PaymentEventV1, clock: MutableClock | None = None
) -> bool:
    with session_factory.begin() as session:
        return apply_payment_event(session, clock or MutableClock(CLOCK_T1), event)


def _deliver_all(
    session_factory: sessionmaker[Session], events: list[PaymentEventV1], clock: MutableClock | None = None
) -> None:
    for event in events:
        _deliver(session_factory, event, clock)


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def _ops_alerts(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, "SELECT * FROM ops_ai.ops_alerts ORDER BY created_at, alert_id")


def _fraud_alerts(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, "SELECT * FROM ops_ai.fraud_alerts ORDER BY started_at, alert_id")


def _tickets(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, "SELECT * FROM ops_ai.tickets ORDER BY created_at, ticket_id")


def _deliveries(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    query = "SELECT * FROM ops_ai.ops_alert_deliveries ORDER BY created_at, delivery_id"
    return _rows(session_factory, query)


def _project_all_as_backfill(session_factory: sessionmaker[Session], events: list[PaymentEventV1]) -> None:
    _deliver_all(session_factory, [event.model_copy(update={"origin": "backfill"}) for event in events])


def _leaks(events: list[PaymentEventV1], rendered: str) -> list[str]:
    """Payment ids, card refs and BIN-plus-last4 pairs from the events that appear in `rendered`."""
    found = [str(e.payment_id) for e in events if str(e.payment_id) in rendered]
    found += [e.card_ref for e in events if e.card_ref and e.card_ref in rendered]
    found += [
        f"{e.card_bin}...{e.card_last4}"
        for e in events
        if e.card_bin and e.card_last4 and e.card_bin in rendered and e.card_last4 in rendered
    ]
    return found


# --- the burst raises one ops alert ----------------------------------------------------------------------


def test_a_card_testing_burst_raises_exactly_one_ops_alert_linked_to_the_fraud_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (fraud_alert,) = _fraud_alerts(session_factory)
    (ops_alert,) = _ops_alerts(session_factory)
    assert isinstance(ops_alert["alert_id"], uuid.UUID)
    assert {key: value for key, value in ops_alert.items() if key not in ("alert_id", "title", "body")} == {
        "source_key": fraud_alert["window_key"],
        "kind": "card_testing",
        "severity": "high",
        "merchant_id": MERCHANT,
        "fraud_alert_id": fraud_alert["alert_id"],
        "created_at": CLOCK_T1,
        "acknowledged_at": None,
        "acknowledged_by": None,
    }


def test_the_ops_alert_source_key_is_the_fraud_alert_window_key(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (ops_alert,) = _ops_alerts(session_factory)
    assert ops_alert["source_key"] == f"card_testing:{MERCHANT}:{events[0].payment_id}"


def test_the_ops_alert_title_is_card_testing_suspected_and_the_body_is_the_ticket_reason(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (ops_alert,) = _ops_alerts(session_factory)
    (ticket,) = _tickets(session_factory)
    assert ops_alert["title"] == "Card testing suspected"
    assert ops_alert["body"] == ticket["reason"]
    assert re.search(r"\b10\b", str(ops_alert["body"])) is not None


def test_the_ops_alert_has_one_pending_redis_stream_delivery(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (ops_alert,) = _ops_alerts(session_factory)
    (delivery,) = _deliveries(session_factory)
    assert (delivery["alert_id"], delivery["channel"], delivery["status"], delivery["attempts"]) == (
        ops_alert["alert_id"],
        "redis_stream",
        "pending",
        0,
    )


def test_the_ops_alert_is_raised_in_the_same_transaction_as_the_fraud_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12)

    # Act
    with session_factory() as session:
        for event in events:
            apply_payment_event(session, MutableClock(CLOCK_T1), event)
        session.rollback()

    # Assert
    assert _fraud_alerts(session_factory) == []
    assert _ops_alerts(session_factory) == []
    assert _deliveries(session_factory) == []


# --- no second ops alert ---------------------------------------------------------------------------------


def test_further_events_in_the_same_window_raise_no_second_ops_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12)
    _deliver_all(session_factory, events, MutableClock(CLOCK_T1))
    (ops_alert_before,) = _ops_alerts(session_factory)

    # Act
    later = _burst(count=3, first_n=13, start=BASE + timedelta(seconds=120))
    _deliver_all(session_factory, later, MutableClock(CLOCK_T2))

    # Assert
    assert _ops_alerts(session_factory) == [ops_alert_before]
    assert len(_deliveries(session_factory)) == 1
    (fraud_alert,) = _fraud_alerts(session_factory)
    assert fraud_alert["payment_count"] == 15


def test_a_redelivered_event_raises_no_second_ops_alert(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    events = _burst(count=12)
    _deliver_all(session_factory, events, MutableClock(CLOCK_T1))
    ops_alerts_before = _ops_alerts(session_factory)
    deliveries_before = _deliveries(session_factory)

    # Act
    applied = [_deliver(session_factory, event, MutableClock(CLOCK_T2)) for event in events]

    # Assert
    assert applied == [False] * 12
    assert _ops_alerts(session_factory) == ops_alerts_before
    assert _deliveries(session_factory) == deliveries_before
    assert len(ops_alerts_before) == 1


def test_calling_watch_payment_again_for_the_flagged_event_raises_no_second_ops_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12)
    _project_all_as_backfill(session_factory, events)
    clock = MutableClock(CLOCK_T1)
    with session_factory.begin() as session:
        first_call = watch_payment(session, clock, events[11])
    ops_alerts_after_first = _ops_alerts(session_factory)

    # Act
    with session_factory.begin() as session:
        second_call = watch_payment(session, MutableClock(CLOCK_T2), events[11])

    # Assert
    assert first_call is not None
    assert second_call == first_call
    assert len(ops_alerts_after_first) == 1
    assert _ops_alerts(session_factory) == ops_alerts_after_first
    assert len(_deliveries(session_factory)) == 1


def test_a_later_episode_of_the_same_merchant_raises_its_own_ops_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _burst(count=12, first_n=1, start=BASE)
    second = _burst(count=12, first_n=101, start=BASE + timedelta(minutes=10))

    # Act
    _deliver_all(session_factory, first + second)

    # Assert
    ops_alerts = _ops_alerts(session_factory)
    assert sorted(str(a["source_key"]) for a in ops_alerts) == sorted(
        [
            f"card_testing:{MERCHANT}:{first[0].payment_id}",
            f"card_testing:{MERCHANT}:{second[0].payment_id}",
        ]
    )
    assert len(_deliveries(session_factory)) == 2


# --- no ops alert ----------------------------------------------------------------------------------------


def test_a_burst_that_is_not_flagged_raises_no_ops_alert(session_factory: sessionmaker[Session]) -> None:
    # Arrange: twelve micro payments from one card
    events = _burst(count=12, cards=1, declined=0)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    assert _fraud_alerts(session_factory) == []
    assert _ops_alerts(session_factory) == []
    assert _deliveries(session_factory) == []


def test_a_backfilled_burst_raises_no_ops_alert(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    events = _burst(count=12, origin="backfill")

    # Act
    _deliver_all(session_factory, events)

    # Assert
    assert _ops_alerts(session_factory) == []
    assert _deliveries(session_factory) == []


# --- privacy ---------------------------------------------------------------------------------------------


def test_the_ops_alert_title_and_body_hold_no_payment_id_card_ref_or_bin_plus_last4(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (ops_alert,) = _ops_alerts(session_factory)
    assert _leaks(events, f"{ops_alert['title']} {ops_alert['body']}") == []


def test_the_streamed_ops_alert_holds_no_payment_id_card_ref_or_bin_plus_last4(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    events = _burst(count=12)
    _deliver_all(session_factory, events)

    # Act
    delivered = deliver_pending(session_factory, redis_client, MutableClock(CLOCK_T2))

    # Assert
    (ops_alert,) = _ops_alerts(session_factory)
    ((_entry_id, fields),) = cast(StreamEntries, redis_client.xrange(OPS_ALERTS_STREAM))
    assert delivered == 1
    assert fields["alert_id"] == str(ops_alert["alert_id"])
    assert (fields["kind"], fields["severity"], fields["title"]) == (
        "card_testing",
        "high",
        "Card testing suspected",
    )
    assert _leaks(events, " ".join(fields.values())) == []
