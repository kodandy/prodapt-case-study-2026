"""Tests for watch_payment through apply_payment_event, written from docs/specs/sq3.md section 4 (Live)."""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from contracts.events import PaymentEventV1
from ops_ai.fraud_watch import watch_payment
from ops_ai.ingest.consumer import apply_payment_event
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

BASE = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
CLOCK_T1 = BASE + timedelta(hours=2)
CLOCK_T2 = BASE + timedelta(hours=3)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
Origin = Literal["live", "backfill"]


@dataclass
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def _card_ref(n: int) -> str:
    return f"{n:032x}"


def _event(
    n: int,
    at: datetime,
    *,
    card: int | None = 1,
    declined: bool = True,
    amount_minor: int = 50,
    currency: str = "USD",
    origin: Origin = "live",
    merchant_id: uuid.UUID = MERCHANT,
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=10_000 + n),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=at,
        origin=origin,
        entity_version=1,
        payment_id=uuid.UUID(int=n),
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency=currency,
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="400000",
        card_last4="0002",
        card_ref=None if card is None else _card_ref(card),
    )


def _burst(
    *,
    count: int = 12,
    start: datetime = BASE,
    step_seconds: int = 10,
    first_n: int = 1,
    cards: int = 4,
    declined: int = 6,
    origin: Origin = "live",
    merchant_id: uuid.UUID = MERCHANT,
    card_offset: int = 0,
) -> list[PaymentEventV1]:
    return [
        _event(
            first_n + index,
            start + timedelta(seconds=index * step_seconds),
            card=card_offset + index % cards + 1,
            declined=index < declined,
            origin=origin,
            merchant_id=merchant_id,
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


def _alerts(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, "SELECT * FROM ops_ai.fraud_alerts ORDER BY started_at, alert_id")


def _tickets(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, "SELECT * FROM ops_ai.tickets ORDER BY created_at, ticket_id")


def _counts(session_factory: sessionmaker[Session]) -> tuple[int, int]:
    return len(_alerts(session_factory)), len(_tickets(session_factory))


# --- the burst raises one alert and one ticket ----------------------------------------------------------


def test_a_live_micro_burst_raises_exactly_one_alert_with_the_episode_window_and_count(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (alert,) = _alerts(session_factory)
    first = events[0].payment_id
    assert {key: alert[key] for key in alert if key not in ("alert_id", "features")} == {
        "window_key": f"card_testing:{MERCHANT}:{first}",
        "merchant_id": MERCHANT,
        "kind": "card_testing",
        "first_payment_id": first,
        "started_at": BASE,
        "last_seen_at": BASE + timedelta(seconds=110),
        "payment_count": 12,
        "created_at": CLOCK_T1,
    }


def test_a_live_micro_burst_raises_one_open_high_priority_ticket_in_the_fraud_queue(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (alert,) = _alerts(session_factory)
    (ticket,) = _tickets(session_factory)
    assert ticket["queue"] == "fraud"
    assert ticket["priority"] == "high"
    assert ticket["status"] == "open"
    assert ticket["merchant_id"] == MERCHANT
    assert ticket["source_key"] == alert["window_key"]
    assert ticket["alert_id"] == alert["alert_id"]
    assert str(ticket["title"]).strip() != ""
    assert str(ticket["reason"]).strip() != ""


def test_the_alert_features_are_the_episode_counts_ratios_bins_currencies_and_reasons(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (alert,) = _alerts(session_factory)
    assert alert["features"] == {
        "micro_payments": 12,
        "card_payments": 12,
        "micro_share": 1.0,
        "distinct_cards": 4,
        "declined": 6,
        "decline_ratio": 0.5,
        "card_bins": ["400000"],
        "currencies": ["USD"],
        "reasons": ["micro_burst", "many_cards", "high_decline_ratio"],
    }


def test_the_alert_features_hold_no_payment_id_and_no_card_ref(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (alert,) = _alerts(session_factory)
    rendered = str(alert["features"])
    assert [str(event.payment_id) for event in events if str(event.payment_id) in rendered] == []
    assert [event.card_ref for event in events if event.card_ref and event.card_ref in rendered] == []


def test_the_ticket_title_and_reason_name_the_count_the_distinct_cards_and_the_span(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: eleven backfilled payments, then the twelfth arrives live, so the alert starts at 12
    events = _burst(count=12, cards=4, declined=6)
    _deliver_all(session_factory, [event.model_copy(update={"origin": "backfill"}) for event in events[:11]])

    # Act
    _deliver(session_factory, events[11])

    # Assert
    (ticket,) = _tickets(session_factory)
    reason = str(ticket["reason"])
    assert re.search(r"\b12\b", reason) is not None
    assert re.search(r"\b4\b", reason) is not None
    assert re.search(r"110|1\s*(m|min|minutes?)\D{0,3}50", reason) is not None
    assert str(ticket["title"]).strip() != ""


def test_the_raised_alert_is_logged_once_with_alert_merchant_and_count_and_nothing_else(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6)
    _deliver_all(session_factory, [event.model_copy(update={"origin": "backfill"}) for event in events[:11]])
    caplog.set_level(logging.DEBUG)

    # Act
    _deliver(session_factory, events[11])

    # Assert
    (alert,) = _alerts(session_factory)
    lines = [r.getMessage() for r in caplog.records if "fraud alert raised" in r.getMessage()]
    assert len(lines) == 1
    assert str(alert["alert_id"]) in lines[0]
    assert str(MERCHANT) in lines[0]
    assert re.search(r"\b12\b", lines[0]) is not None
    assert [e.card_ref for e in events if e.card_ref and e.card_ref in lines[0]] == []
    assert [str(e.payment_id) for e in events if str(e.payment_id) in lines[0]] == []


def test_later_payments_of_the_same_episode_do_not_log_a_second_alert_line(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    caplog.set_level(logging.DEBUG)

    # Act
    _deliver_all(session_factory, _burst(count=13, cards=4, declined=6))

    # Assert
    lines = [r.getMessage() for r in caplog.records if "fraud alert raised" in r.getMessage()]
    assert len(lines) == 1


def test_two_merchants_bursts_in_the_same_span_raise_one_alert_and_one_ticket_each(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    own = _burst(count=12, first_n=1, cards=4)
    other = _burst(
        count=12,
        first_n=1000,
        start=BASE + timedelta(seconds=3),
        cards=4,
        card_offset=50,
        merchant_id=OTHER_MERCHANT,
    )
    interleaved = [event for pair in zip(own, other, strict=True) for event in pair]

    # Act
    _deliver_all(session_factory, interleaved)

    # Assert
    alerts = _alerts(session_factory)
    tickets = _tickets(session_factory)
    assert sorted(str(a["merchant_id"]) for a in alerts) == sorted([str(MERCHANT), str(OTHER_MERCHANT)])
    assert sorted(str(t["merchant_id"]) for t in tickets) == sorted([str(MERCHANT), str(OTHER_MERCHANT)])
    assert {a["payment_count"] for a in alerts} == {12}


# --- redelivery and later payments ----------------------------------------------------------------------


def test_redelivering_the_same_events_creates_no_second_alert_and_no_second_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6)
    _deliver_all(session_factory, events)
    alerts_before = _alerts(session_factory)
    tickets_before = _tickets(session_factory)

    # Act
    applied = [_deliver(session_factory, event, MutableClock(CLOCK_T2)) for event in events]

    # Assert
    assert applied == [False] * 12
    assert _alerts(session_factory) == alerts_before
    assert _tickets(session_factory) == tickets_before
    assert len(alerts_before) == 1
    assert len(tickets_before) == 1


def test_a_thirteenth_payment_updates_the_alert_and_creates_no_second_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=13, cards=4, declined=6)
    _deliver_all(session_factory, events[:12], MutableClock(CLOCK_T1))
    (alert_before,) = _alerts(session_factory)
    (ticket_before,) = _tickets(session_factory)

    # Act
    _deliver(session_factory, events[12], MutableClock(CLOCK_T2))

    # Assert
    (alert_after,) = _alerts(session_factory)
    (ticket_after,) = _tickets(session_factory)
    assert alert_after["alert_id"] == alert_before["alert_id"]
    assert alert_after["payment_count"] == 13
    assert alert_after["last_seen_at"] == BASE + timedelta(seconds=120)
    assert alert_after["started_at"] == BASE
    assert alert_after["created_at"] == CLOCK_T1
    assert isinstance(alert_after["features"], dict)
    assert alert_after["features"]["micro_payments"] == 13
    assert ticket_after == ticket_before


def test_a_payment_in_a_later_episode_of_the_same_merchant_raises_its_own_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _burst(count=12, first_n=1, start=BASE)
    second = _burst(count=12, first_n=101, start=BASE + timedelta(minutes=10))

    # Act
    _deliver_all(session_factory, first + second)

    # Assert
    alerts = _alerts(session_factory)
    assert [a["first_payment_id"] for a in alerts] == [first[0].payment_id, second[0].payment_id]
    assert [a["payment_count"] for a in alerts] == [12, 12]
    assert len(_tickets(session_factory)) == 2


def test_an_existing_ticket_with_the_same_source_key_is_kept_and_no_second_one_is_created(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6)
    source_key = f"card_testing:{MERCHANT}:{events[0].payment_id}"
    existing_ticket = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO ops_ai.tickets (ticket_id, source_key, queue, priority, merchant_id, title, "
                "reason, created_at) VALUES (:ticket_id, :source_key, 'fraud', 'high', :merchant_id, "
                "'pre-existing', 'pre-existing', :created_at)"
            ),
            {
                "ticket_id": existing_ticket,
                "source_key": source_key,
                "merchant_id": MERCHANT,
                "created_at": BASE,
            },
        )

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (ticket,) = _tickets(session_factory)
    assert (ticket["ticket_id"], ticket["title"], ticket["alert_id"]) == (
        existing_ticket,
        "pre-existing",
        None,
    )
    assert len(_alerts(session_factory)) == 1


def test_the_alert_ticket_and_projection_roll_back_together_with_the_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6)

    # Act
    with session_factory() as session:
        for event in events:
            apply_payment_event(session, MutableClock(CLOCK_T1), event)
        session.rollback()

    # Assert
    facts = _rows(session_factory, "SELECT payment_id FROM ops_ai.payment_facts")
    assert facts == []
    assert _counts(session_factory) == (0, 0)


# --- bursts that must not alert --------------------------------------------------------------------------


def test_twelve_micro_payments_from_a_single_card_raise_no_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: a legitimate top-up series
    events = _burst(count=12, cards=1, declined=0)

    # Act
    _deliver_all(session_factory, events)

    # Assert
    assert _counts(session_factory) == (0, 0)


def test_a_three_minute_gap_after_the_sixth_payment_splits_the_burst_into_two_episodes_of_six_without_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _burst(count=6, first_n=1, start=BASE)
    second = _burst(count=6, first_n=101, start=BASE + timedelta(seconds=50 + 180))

    # Act
    _deliver_all(session_factory, first + second)

    # Assert
    assert _counts(session_factory) == (0, 0)


def test_twelve_micro_payments_interleaved_with_twelve_larger_card_payments_raise_no_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: share 0.5 inside the span
    micro = _burst(count=12, cards=4)
    larger = [
        _event(500 + index, BASE + timedelta(seconds=index * 10 + 5), card=index % 4 + 1, amount_minor=5000)
        for index in range(11)
    ]
    larger.append(_event(600, BASE + timedelta(seconds=110), card=1, amount_minor=5000))
    ordered = sorted([*micro, *larger], key=lambda e: (e.occurred_at, e.payment_id))

    # Act
    _deliver_all(session_factory, ordered)

    # Assert
    assert _counts(session_factory) == (0, 0)


def test_the_same_burst_with_origin_backfill_raises_no_alert_but_is_still_projected(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6, origin="backfill")

    # Act
    _deliver_all(session_factory, events)

    # Assert
    assert _counts(session_factory) == (0, 0)
    assert len(_rows(session_factory, "SELECT payment_id FROM ops_ai.payment_facts")) == 12


def test_a_burst_with_unknown_card_identity_raises_an_alert_with_unknown_distinct_cards(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = [
        _event(index + 1, BASE + timedelta(seconds=index * 10), card=None, declined=True)
        for index in range(12)
    ]

    # Act
    _deliver_all(session_factory, events)

    # Assert
    (alert,) = _alerts(session_factory)
    assert isinstance(alert["features"], dict)
    assert alert["features"]["distinct_cards"] is None
    assert alert["features"]["reasons"] == ["micro_burst", "card_identity_unknown", "high_decline_ratio"]
    assert len(_tickets(session_factory)) == 1


# --- watch_payment itself ---------------------------------------------------------------------------------


def _project_all_as_backfill(session_factory: sessionmaker[Session], events: list[PaymentEventV1]) -> None:
    _deliver_all(session_factory, [event.model_copy(update={"origin": "backfill"}) for event in events])


def test_watch_payment_returns_the_alert_id_for_a_flagged_live_event_and_the_same_id_on_a_repeat_call(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6)
    _project_all_as_backfill(session_factory, events)
    clock = MutableClock(CLOCK_T1)

    # Act
    with session_factory.begin() as session:
        first_call = watch_payment(session, clock, events[11])
    with session_factory.begin() as session:
        second_call = watch_payment(session, clock, events[11])

    # Assert
    (alert,) = _alerts(session_factory)
    assert first_call == alert["alert_id"]
    assert second_call == first_call
    assert len(_tickets(session_factory)) == 1


def test_watch_payment_returns_none_for_a_backfilled_event_even_inside_a_flagged_burst(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=4, declined=6, origin="backfill")
    _project_all_as_backfill(session_factory, events)

    # Act
    with session_factory.begin() as session:
        result = watch_payment(session, MutableClock(CLOCK_T1), events[11])

    # Assert
    assert result is None
    assert _counts(session_factory) == (0, 0)


def test_watch_payment_returns_none_for_a_live_payment_above_the_micro_limit(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: a flagged burst is running, then a 101-cent payment arrives
    _project_all_as_backfill(session_factory, _burst(count=12, cards=4, declined=6))
    larger = _event(500, BASE + timedelta(seconds=120), card=1, amount_minor=101)
    _project_all_as_backfill(session_factory, [larger])

    # Act
    with session_factory.begin() as session:
        result = watch_payment(session, MutableClock(CLOCK_T1), larger)

    # Assert
    assert result is None
    assert _counts(session_factory) == (0, 0)


def test_watch_payment_returns_none_when_the_episode_is_not_flagged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    events = _burst(count=12, cards=1, declined=0)
    _project_all_as_backfill(session_factory, events)

    # Act
    with session_factory.begin() as session:
        result = watch_payment(session, MutableClock(CLOCK_T1), events[11])

    # Assert
    assert result is None
    assert _counts(session_factory) == (0, 0)


def test_watch_payment_ignores_payments_that_occurred_after_the_event(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the event is the earliest of the burst, eleven later payments are already projected
    events = _burst(count=12, cards=4, declined=6)
    _project_all_as_backfill(session_factory, events[1:])
    earliest = events[0]
    _project_all_as_backfill(session_factory, [earliest])

    # Act
    with session_factory.begin() as session:
        result = watch_payment(session, MutableClock(CLOCK_T1), earliest)

    # Assert
    assert result is None
    assert _counts(session_factory) == (0, 0)


def test_watch_payment_looks_back_exactly_30_minutes_from_the_event(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 21 payments 100 s apart form one 2000 s episode; the 30 minute window starts at +200 s
    events = _burst(count=21, step_seconds=100, cards=4, declined=0)
    _project_all_as_backfill(session_factory, events[:20])

    # Act
    _deliver(session_factory, events[20])

    # Assert
    (alert,) = _alerts(session_factory)
    assert alert["first_payment_id"] == events[2].payment_id
    assert alert["started_at"] == BASE + timedelta(seconds=200)
    assert alert["payment_count"] == 19
