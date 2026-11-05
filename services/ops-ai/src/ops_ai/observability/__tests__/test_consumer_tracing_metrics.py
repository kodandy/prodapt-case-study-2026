"""Tests for the tracing and metrics added to consume_payments, from the NFR spec C (consumer change)."""

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from ops_ai.ingest.consumer import apply_payment_event, consume_payments
from ops_ai.observability.metrics import REGISTRY
from ops_ai.observability.tracing import TraceIdLogFilter, configure_tracing
from redis import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy.orm import Session, sessionmaker

SPAN_NAME = "consume gw.payments.v1"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"
OTHER_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
OTHER_TRACEPARENT = f"00-{OTHER_TRACE_ID}-b7ad6b7169203331-01"
BASE = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
CONTROL_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
CONSUMER = "tracing-test-consumer"


def _clock_now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def _consumed(outcome: str) -> float:
    return _sample("ops_ai_events_consumed_total", {"outcome": outcome})


def _failures(code: str) -> float:
    return _sample("ops_ai_payment_failures_total", {"code": code})


def _fraud_flags(kind: str) -> float:
    return _sample("ops_ai_fraud_flags_total", {"kind": kind})


def _event(
    *,
    failed: bool = False,
    amount_minor: int = 1500,
    merchant_id: uuid.UUID = MERCHANT,
) -> PaymentEventV1:
    """A declined (decline_code card_declined) event, or with failed=True a failed one without a code."""
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.failed" if failed else "payment.declined",
        occurred_at=BASE,
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency="USD",
        method="card",
        state="failed" if failed else "declined",
        provider="acq_a",
        decline_code=None if failed else "card_declined",
        card_bin="400000",
        card_last4="0002",
    )


def _succeeded_event() -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.succeeded",
        occurred_at=BASE,
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=MERCHANT,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="succeeded",
        provider="acq_a",
        decline_code=None,
        card_bin="424242",
        card_last4="4242",
    )


def _stream_add(redis: Redis, event: PaymentEventV1, traceparent: str | None = None) -> None:
    fields: dict[FieldT, EncodableT] = {
        "event_id": str(event.event_id),
        "payload": event.model_dump_json(),
    }
    if traceparent is not None:
        fields["traceparent"] = traceparent
    redis.xadd(PAYMENTS_TOPIC, fields)


def _consume(session_factory: sessionmaker[Session], redis: Redis) -> int:
    return consume_payments(session_factory, redis, _clock_now, CONSUMER)


def _consumer_spans(exporter: InMemorySpanExporter) -> list[tuple[str, int | None]]:
    return [
        (format(s.context.trace_id, "032x"), None if s.parent is None else s.parent.span_id)
        for s in exporter.get_finished_spans()
        if s.name == SPAN_NAME
    ]


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    configure_tracing("ops-ai").add_span_processor(SimpleSpanProcessor(memory))
    yield memory
    memory.clear()


# --- spans -----------------------------------------------------------------------------------------------


def test_consume_payments_starts_a_consumer_span_in_the_trace_of_the_entry_traceparent(
    session_factory: sessionmaker[Session], redis_client: Redis, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    _stream_add(redis_client, _event(), TRACEPARENT)

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    assert read == 1
    assert _consumer_spans(exporter) == [(TRACE_ID, int(PARENT_SPAN_ID, 16))]


def test_the_consumer_span_has_kind_consumer(
    session_factory: sessionmaker[Session], redis_client: Redis, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    _stream_add(redis_client, _event(), TRACEPARENT)

    # Act
    _consume(session_factory, redis_client)

    # Assert
    [span] = [s for s in exporter.get_finished_spans() if s.name == SPAN_NAME]
    assert span.kind == SpanKind.CONSUMER


def test_an_entry_without_traceparent_still_gets_a_root_consumer_span_in_a_new_trace(
    session_factory: sessionmaker[Session], redis_client: Redis, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    _stream_add(redis_client, _event())

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    [(trace_id, parent_span_id)] = _consumer_spans(exporter)
    assert read == 1
    assert parent_span_id is None
    assert len(trace_id) == 32
    assert trace_id not in (TRACE_ID, OTHER_TRACE_ID)


def test_each_entry_gets_its_own_span_in_the_trace_of_its_own_traceparent(
    session_factory: sessionmaker[Session], redis_client: Redis, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    _stream_add(redis_client, _event(), TRACEPARENT)
    _stream_add(redis_client, _event(), OTHER_TRACEPARENT)

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    assert read == 2
    assert [trace_id for trace_id, _ in _consumer_spans(exporter)] == [TRACE_ID, OTHER_TRACE_ID]


def test_logs_emitted_while_handling_an_entry_carry_the_entry_trace_id(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    seen: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.trace_id)  # type: ignore[attr-defined]

    handler = _Capture(level=logging.DEBUG)
    handler.addFilter(TraceIdLogFilter())
    consumer_logger = logging.getLogger("ops_ai.ingest.consumer")
    previous_level = consumer_logger.level
    consumer_logger.setLevel(logging.DEBUG)
    consumer_logger.addHandler(handler)
    redis_client.xadd(
        PAYMENTS_TOPIC, {"event_id": str(uuid.uuid4()), "payload": "{not json", "traceparent": TRACEPARENT}
    )

    # Act
    try:
        _consume(session_factory, redis_client)
    finally:
        consumer_logger.removeHandler(handler)
        consumer_logger.setLevel(previous_level)

    # Assert
    assert seen == [TRACE_ID]


# --- metrics ---------------------------------------------------------------------------------------------


def test_an_applied_event_increments_events_consumed_applied(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    applied_before = _consumed("applied")
    duplicate_before = _consumed("duplicate")
    dead_before = _consumed("dead_letter")
    _stream_add(redis_client, _succeeded_event())

    # Act
    _consume(session_factory, redis_client)

    # Assert
    assert _consumed("applied") - applied_before == 1.0
    assert _consumed("duplicate") - duplicate_before == 0.0
    assert _consumed("dead_letter") - dead_before == 0.0


def test_a_redelivered_event_is_counted_once_as_applied_and_once_as_duplicate(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    event = _succeeded_event()
    _stream_add(redis_client, event)
    _stream_add(redis_client, event)
    applied_before = _consumed("applied")
    duplicate_before = _consumed("duplicate")

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    assert read == 2
    assert _consumed("applied") - applied_before == 1.0
    assert _consumed("duplicate") - duplicate_before == 1.0


def test_a_malformed_entry_increments_events_consumed_dead_letter(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    redis_client.xadd(PAYMENTS_TOPIC, {"event_id": str(uuid.uuid4()), "payload": "{not json"})
    dead_before = _consumed("dead_letter")
    applied_before = _consumed("applied")

    # Act
    _consume(session_factory, redis_client)

    # Assert
    assert _consumed("dead_letter") - dead_before == 1.0
    assert _consumed("applied") - applied_before == 0.0


def test_a_declined_event_increments_payment_failures_under_its_decline_code(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    before = _failures("card_declined")
    none_before = _failures("none")
    _stream_add(redis_client, _event())

    # Act
    _consume(session_factory, redis_client)

    # Assert
    assert _failures("card_declined") - before == 1.0
    assert _failures("none") - none_before == 0.0


def test_a_failed_event_without_a_decline_code_is_counted_under_none(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    before = _failures("none")
    _stream_add(redis_client, _event(failed=True))

    # Act
    _consume(session_factory, redis_client)

    # Assert
    assert _failures("none") - before == 1.0


def test_a_succeeded_event_does_not_increment_payment_failures(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    declined_before = _failures("card_declined")
    none_before = _failures("none")
    _stream_add(redis_client, _succeeded_event())

    # Act
    _consume(session_factory, redis_client)

    # Assert
    assert _failures("card_declined") - declined_before == 0.0
    assert _failures("none") - none_before == 0.0


def _card_testing_event(index: int, merchant_id: uuid.UUID) -> PaymentEventV1:
    declined = index < 6
    return PaymentEventV1(
        event_id=uuid.UUID(int=10_000 + index),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=BASE + timedelta(seconds=index * 10),
        entity_version=1,
        payment_id=uuid.UUID(int=index + 1),
        merchant_id=merchant_id,
        amount_minor=50,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="400000",
        card_last4="0002",
        card_ref=f"{index % 4 + 1:032x}",
    )


def test_an_event_that_completes_a_card_testing_episode_increments_fraud_flags(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    burst = [_card_testing_event(index, MERCHANT) for index in range(12)]
    with session_factory.begin() as session:
        for event in burst[:11]:
            apply_payment_event(session, _clock_now, event)
    before = _fraud_flags("card_testing")
    _stream_add(redis_client, burst[11])

    # Act
    _consume(session_factory, redis_client)

    # Assert
    assert _fraud_flags("card_testing") - before == 1.0


def test_an_event_that_raises_no_alert_does_not_increment_fraud_flags(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    before = _fraud_flags("card_testing")
    _stream_add(redis_client, _event(merchant_id=CONTROL_MERCHANT, amount_minor=5000))

    # Act
    _consume(session_factory, redis_client)

    # Assert
    assert _fraud_flags("card_testing") - before == 0.0
