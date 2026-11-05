"""Graceful shutdown of run_consumer (P1-6): an event applied when `stop` is set is finished and acked."""

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from ops_ai.ingest import consumer as consumer_module
from ops_ai.ingest.consumer import PAYMENT_FACTS_GROUP, apply_payment_event
from ops_ai.ingest.worker import run_consumer
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

WAIT_SECONDS = 15.0
BLOCK_MS = 50
OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)


def _clock_now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _event() -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined",
        occurred_at=OCCURRED_AT,
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=uuid.uuid4(),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined",
        provider="acq_a",
        decline_code="card_declined",
        card_bin="400000",
        card_last4="0002",
    )


def _stream_add(redis: Redis, event: PaymentEventV1) -> None:
    redis.xadd(PAYMENTS_TOPIC, {"event_id": str(event.event_id), "payload": event.model_dump_json()})


def _fact_ids(session_factory: sessionmaker[Session]) -> list[uuid.UUID]:
    with session_factory() as session:
        return list(
            session.execute(text("SELECT payment_id FROM ops_ai.payment_facts ORDER BY payment_id")).scalars()
        )


def _processed_event_ids(session_factory: sessionmaker[Session]) -> list[uuid.UUID]:
    query = text("SELECT event_id FROM ops_ai.processed_events WHERE consumer = :consumer ORDER BY event_id")
    with session_factory() as session:
        return list(session.execute(query, {"consumer": PAYMENT_FACTS_GROUP}).scalars())


def _pending(redis: Redis) -> int:
    return int(redis.xpending(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)["pending"])


def _stop_on_first_apply(monkeypatch: pytest.MonkeyPatch, stop: threading.Event) -> Callable[[], int]:
    """Sets `stop` inside the first apply_payment_event call (mid-transaction), then applies normally.

    run_consumer calls clock() at the top of every loop iteration for the unknown-payment sweep, so a clock
    based trigger would depend on the sweep's call count; wrapping the apply fires at the exact moment an
    event is in flight. Returns a callable reporting how many applies ran.
    """
    applied: list[uuid.UUID] = []

    def apply_then_stop(session: Session, clock: Callable[[], datetime], event: PaymentEventV1) -> bool:
        stop.set()
        applied.append(event.event_id)
        return apply_payment_event(session, clock, event)

    monkeypatch.setattr(consumer_module, "apply_payment_event", apply_then_stop)
    return lambda: len(applied)


def _run_to_completion(session_factory: sessionmaker[Session], redis: Redis, stop: threading.Event) -> int:
    """Runs run_consumer on a daemon thread so a loop that ignores `stop` fails the test, not hangs it."""
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["total"] = run_consumer(
                session_factory, redis, _clock_now, "shutdown-consumer", stop, block_ms=BLOCK_MS
            )
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(WAIT_SECONDS)
    if thread.is_alive():
        stop.set()
        pytest.fail(f"run_consumer did not return within {WAIT_SECONDS}s after stop was set mid-apply")
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    total = outcome["total"]
    assert isinstance(total, int)
    return total


def test_run_consumer_finishes_and_acks_the_event_being_applied_when_stop_is_set(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    event = _event()
    _stream_add(redis_client, event)
    stop = threading.Event()
    applies = _stop_on_first_apply(monkeypatch, stop)

    # Act
    total = _run_to_completion(session_factory, redis_client, stop)

    # Assert
    assert applies() == 1
    assert total == 1
    assert _fact_ids(session_factory) == [event.payment_id]
    assert _pending(redis_client) == 0
    assert _processed_event_ids(session_factory) == [event.event_id]


def test_run_consumer_finishes_every_entry_of_the_batch_that_was_in_flight_when_stop_is_set(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    events = [_event(), _event(), _event()]
    for event in events:
        _stream_add(redis_client, event)
    stop = threading.Event()
    applies = _stop_on_first_apply(monkeypatch, stop)

    # Act
    total = _run_to_completion(session_factory, redis_client, stop)

    # Assert
    assert applies() == 3
    assert total == 3
    assert _fact_ids(session_factory) == sorted(event.payment_id for event in events)
    assert _pending(redis_client) == 0
    assert _processed_event_ids(session_factory) == sorted(event.event_id for event in events)


def test_run_consumer_started_again_after_the_shutdown_adds_no_duplicate_row(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    event = _event()
    _stream_add(redis_client, event)
    first_stop = threading.Event()
    with monkeypatch.context() as first_run:
        _stop_on_first_apply(first_run, first_stop)
        _run_to_completion(session_factory, redis_client, first_stop)
    second_stop = threading.Event()
    second_stop.set()

    # Act
    total = _run_to_completion(session_factory, redis_client, second_stop)

    # Assert
    assert total == 0
    assert _fact_ids(session_factory) == [event.payment_id]
    assert _processed_event_ids(session_factory) == [event.event_id]
    assert _pending(redis_client) == 0


def test_run_consumer_re_run_with_a_live_loop_does_not_reapply_the_acked_event(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    event = _event()
    _stream_add(redis_client, event)
    first_stop = threading.Event()
    with monkeypatch.context() as first_run:
        _stop_on_first_apply(first_run, first_stop)
        _run_to_completion(session_factory, redis_client, first_stop)
    reapplied: list[uuid.UUID] = []
    second_stop = threading.Event()

    def count_then_stop(session: Session, clock: Callable[[], datetime], applied: PaymentEventV1) -> bool:
        reapplied.append(applied.event_id)
        second_stop.set()
        return apply_payment_event(session, clock, applied)

    monkeypatch.setattr(consumer_module, "apply_payment_event", count_then_stop)
    new_event = _event()
    _stream_add(redis_client, new_event)

    # Act
    total = _run_to_completion(session_factory, redis_client, second_stop)

    # Assert
    assert reapplied == [new_event.event_id]
    assert total == 1
    assert _processed_event_ids(session_factory) == sorted([event.event_id, new_event.event_id])
    assert _pending(redis_client) == 0
