"""Tests for gateway.observability.outbox: the OutboxEvent before_insert hook and record_outbox_backlog."""

import json
import uuid
from datetime import datetime, timedelta

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from gateway.clock import FixedClock
from gateway.observability.metrics import REGISTRY
from gateway.observability.outbox import record_outbox_backlog, register_outbox_hooks
from gateway.observability.tracing import configure_tracing
from gateway.outbox.models import OutboxEvent
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

OTHER_TOPIC = "gw.other.v1"
PRESET_TRACEPARENT = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"


def _payment_events(method: str, state: str, decline_code: str) -> float:
    value = REGISTRY.get_sample_value(
        "gateway_payment_events_total", {"method": method, "state": state, "decline_code": decline_code}
    )
    return 0.0 if value is None else value


def _gauge(name: str) -> float | None:
    return REGISTRY.get_sample_value(name)


def _event(clock: FixedClock, state: str = "succeeded") -> PaymentEventV1:
    declined = state == "declined"
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=clock(),
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=uuid.uuid4(),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="424242",
        card_last4="4242",
    )


def _insert(
    session_factory: sessionmaker[Session],
    *,
    topic: str,
    payload: dict[str, object],
    headers: dict[str, str],
    created_at: datetime,
    published_at: datetime | None = None,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    with session_factory() as session:
        session.add(
            OutboxEvent(
                event_id=event_id,
                topic=topic,
                payload=payload,
                headers=headers,
                created_at=created_at,
                published_at=published_at,
            )
        )
        session.commit()
    return event_id


def _insert_payment_event(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    headers: dict[str, str],
    state: str = "succeeded",
) -> uuid.UUID:
    event = _event(clock, state)
    return _insert(
        session_factory,
        topic=PAYMENTS_TOPIC,
        payload=json.loads(event.model_dump_json()),
        headers=headers,
        created_at=clock(),
    )


def _stored_headers(session_factory: sessionmaker[Session], event_id: uuid.UUID) -> dict[str, str]:
    with session_factory() as session:
        row = session.execute(
            text("SELECT headers FROM gateway.outbox WHERE event_id = :event_id"), {"event_id": event_id}
        ).scalar_one()
    return dict(row)


def test_insert_inside_a_span_stores_the_traceparent_header(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    tracer = configure_tracing("gateway").get_tracer("test")

    # Act
    with tracer.start_as_current_span("request") as span:
        event_id = _insert_payment_event(session_factory, clock, headers={})

    # Assert
    context = span.get_span_context()
    assert _stored_headers(session_factory, event_id) == {
        "traceparent": f"00-{context.trace_id:032x}-{context.span_id:016x}-01"
    }


def test_insert_inside_a_span_keeps_other_headers_and_adds_the_traceparent(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    tracer = configure_tracing("gateway").get_tracer("test")

    # Act
    with tracer.start_as_current_span("request") as span:
        event_id = _insert_payment_event(session_factory, clock, headers={"x-custom": "1"})

    # Assert
    context = span.get_span_context()
    assert _stored_headers(session_factory, event_id) == {
        "x-custom": "1",
        "traceparent": f"00-{context.trace_id:032x}-{context.span_id:016x}-01",
    }


def test_insert_with_an_existing_traceparent_header_does_not_overwrite_it(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    tracer = configure_tracing("gateway").get_tracer("test")

    # Act
    with tracer.start_as_current_span("request"):
        event_id = _insert_payment_event(session_factory, clock, headers={"traceparent": PRESET_TRACEPARENT})

    # Assert
    assert _stored_headers(session_factory, event_id) == {"traceparent": PRESET_TRACEPARENT}


def test_insert_without_an_active_span_leaves_the_headers_unchanged(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    configure_tracing("gateway")

    # Act
    event_id = _insert_payment_event(session_factory, clock, headers={"x-custom": "1"})

    # Assert
    assert _stored_headers(session_factory, event_id) == {"x-custom": "1"}


def test_insert_without_an_active_span_and_empty_headers_stores_empty_headers(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    configure_tracing("gateway")

    # Act
    event_id = _insert_payment_event(session_factory, clock, headers={})

    # Assert
    assert _stored_headers(session_factory, event_id) == {}


def test_insert_on_a_non_payment_topic_still_gets_the_traceparent(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    tracer = configure_tracing("gateway").get_tracer("test")

    # Act
    with tracer.start_as_current_span("request") as span:
        event_id = _insert(
            session_factory, topic=OTHER_TOPIC, payload={"k": "v"}, headers={}, created_at=clock()
        )

    # Assert
    context = span.get_span_context()
    assert _stored_headers(session_factory, event_id) == {
        "traceparent": f"00-{context.trace_id:032x}-{context.span_id:016x}-01"
    }


def test_succeeded_payment_event_increments_the_payment_events_counter_with_decline_code_none(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    before = _payment_events("card", "succeeded", "none")

    # Act
    _insert_payment_event(session_factory, clock, headers={}, state="succeeded")

    # Assert
    assert _payment_events("card", "succeeded", "none") - before == 1.0


def test_declined_payment_event_increments_the_counter_with_its_decline_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    before = _payment_events("card", "declined", "card_declined")
    before_none = _payment_events("card", "declined", "none")

    # Act
    _insert_payment_event(session_factory, clock, headers={}, state="declined")

    # Assert
    assert _payment_events("card", "declined", "card_declined") - before == 1.0
    assert _payment_events("card", "declined", "none") - before_none == 0.0


def test_non_payment_topic_does_not_touch_the_payment_events_counter(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    before = _payment_events("card", "succeeded", "none")

    # Act
    _insert(session_factory, topic=OTHER_TOPIC, payload={"k": "v"}, headers={}, created_at=clock())

    # Assert
    assert _payment_events("card", "succeeded", "none") - before == 0.0


def test_register_outbox_hooks_is_idempotent_so_one_insert_counts_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    register_outbox_hooks()
    register_outbox_hooks()
    register_outbox_hooks()
    before = _payment_events("card", "succeeded", "none")

    # Act
    _insert_payment_event(session_factory, clock, headers={}, state="succeeded")

    # Assert
    assert _payment_events("card", "succeeded", "none") - before == 1.0


def test_record_outbox_backlog_counts_unpublished_rows_and_reports_the_oldest_age(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()
    _insert(
        session_factory,
        topic=OTHER_TOPIC,
        payload={},
        headers={},
        created_at=now - timedelta(seconds=600),
        published_at=now - timedelta(seconds=500),
    )
    _insert(
        session_factory, topic=OTHER_TOPIC, payload={}, headers={}, created_at=now - timedelta(seconds=90)
    )
    _insert(
        session_factory, topic=OTHER_TOPIC, payload={}, headers={}, created_at=now - timedelta(seconds=30)
    )

    # Act
    pending, oldest_age = record_outbox_backlog(session_factory, clock)

    # Assert
    assert (pending, oldest_age) == (2, 90.0)
    assert _gauge("gateway_outbox_pending") == 2.0
    assert _gauge("gateway_outbox_oldest_pending_age_seconds") == 90.0


def test_record_outbox_backlog_on_an_empty_outbox_reports_zero_and_sets_the_gauges(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: no outbox rows

    # Act
    pending, oldest_age = record_outbox_backlog(session_factory, clock)

    # Assert
    assert (pending, oldest_age) == (0, 0.0)
    assert _gauge("gateway_outbox_pending") == 0.0
    assert _gauge("gateway_outbox_oldest_pending_age_seconds") == 0.0


def test_record_outbox_backlog_with_only_published_rows_reports_zero(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()
    _insert(
        session_factory,
        topic=OTHER_TOPIC,
        payload={},
        headers={},
        created_at=now - timedelta(seconds=300),
        published_at=now - timedelta(seconds=299),
    )

    # Act
    pending, oldest_age = record_outbox_backlog(session_factory, clock)

    # Assert
    assert (pending, oldest_age) == (0, 0.0)


def test_record_outbox_backlog_age_follows_the_injected_clock(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert(session_factory, topic=OTHER_TOPIC, payload={}, headers={}, created_at=clock())
    clock.advance(timedelta(seconds=45))

    # Act
    pending, oldest_age = record_outbox_backlog(session_factory, clock)

    # Assert
    assert (pending, oldest_age) == (1, 45.0)
